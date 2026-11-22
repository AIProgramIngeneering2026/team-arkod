"""Координация Job без HTTP, SQL и правил футбольных расчётов."""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, RLock
from typing import Any, Callable
from uuid import uuid4

from arkod.application.ports import (
    DispatcherError,
    FootballDataService,
    JobNotFound,
    JobRecord,
    JobRepository,
    Registry,
    SnapshotError,
    StorageError,
)
from arkod.domain.job import Job, State
from arkod.domain.tool.models import Tool, ToolContext, ToolError, ToolResult

logger = logging.getLogger(__name__)


class UnknownOperation(DispatcherError):
    code = "VALIDATION_ERROR"


class JobNotCancellable(DispatcherError):
    code = "JOB_NOT_CANCELLABLE"


class ResultNotReady(DispatcherError):
    code = "RESULT_NOT_READY"


class ResultUnavailable(DispatcherError):
    code = "RESULT_UNAVAILABLE"


@dataclass(frozen=True)
class _PendingTransition:
    job_id: int
    execution_id: str
    result: ToolResult | None = None
    quota_started_at: datetime | None = None
    retry_at: datetime | None = None


class Dispatcher:
    def __init__(
        self,
        repository: JobRepository,
        registry: Registry,
        data_service: FootballDataService,
        *,
        max_quota_wait_seconds: float = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if not math.isfinite(max_quota_wait_seconds) or max_quota_wait_seconds <= 0:
            raise ValueError("Предел ожидания квоты должен быть положительным")
        self._repository = repository
        self._registry = registry
        self._data_service = data_service
        self._max_quota_wait = max_quota_wait_seconds
        self._clock = clock
        self._pending: dict[tuple[int, str], _PendingTransition] = {}
        self._pending_lock = RLock()

    def _tool(self, operation: str) -> Tool:
        try:
            return self._registry.get(operation)
        except KeyError as exc:
            raise UnknownOperation(f"Неизвестная операция: {operation}") from exc

    def create(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        owner_id: str,
        idempotency_key: str,
        correlation_id: str,
    ) -> Job:
        """Проверяет параметры и сохраняет created; расчёт здесь не запускается."""
        tool = self._tool(operation)
        validated = tool.params_model.model_validate(params)
        if not owner_id or not idempotency_key:
            raise ValueError("Требуются владелец и ключ идемпотентности")
        return self._repository.create_or_get(
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            operation=operation,
            params=validated.model_dump(mode="json"),
            correlation_id=correlation_id,
        ).job

    @staticmethod
    def _check_owner(record: JobRecord, owner_id: str) -> None:
        if record.owner_id != owner_id:
            raise JobNotFound("Задача не найдена")

    def get(self, job_id: int, *, owner_id: str) -> Job:
        record = self._repository.get(job_id)
        self._check_owner(record, owner_id)
        return record.job

    def get_result(self, job_id: int, *, owner_id: str) -> ToolResult:
        job = self.get(job_id, owner_id=owner_id)
        if job.state in {State.CREATED, State.RUNNING, State.WAITING_QUOTA}:
            raise ResultNotReady("Задача ещё выполняется")
        if job.state != State.DONE:
            raise ResultUnavailable("Для этой задачи результат не сохранён")
        return job.result

    def cancel(self, job_id: int, *, owner_id: str) -> Job:
        with self._repository.transaction(job_id) as record:
            self._check_owner(record, owner_id)
            if record.job.state in {State.DONE, State.FAILED}:
                raise JobNotCancellable("Нельзя отменить завершённую задачу")
            if record.job.state != State.CANCELLED:
                record.job.cancel()
                record.retry_at = None
                record.quota_started_at = None
            return record.job

    def _claim(self, job_id: int, execution_id: str) -> JobRecord | None:
        with self._repository.transaction(job_id) as record:
            job = record.job
            now = self._clock()
            if job.state == State.WAITING_QUOTA:
                if record.retry_at is None or record.quota_started_at is None:
                    raise ValueError("У waiting_quota отсутствует расписание ожидания")
                elapsed = (now - record.quota_started_at).total_seconds()
                if record.quota_wait_seconds + elapsed >= self._max_quota_wait:
                    job.expire_quota()
                    record.retry_at = None
                    record.quota_started_at = None
                    return record
                if now < record.retry_at:
                    return None
                record.quota_wait_seconds += elapsed
                record.quota_started_at = None
                record.retry_at = None
                job.resume()
            elif job.state == State.CREATED:
                job.start()
            else:
                return None
            record.execution_id = execution_id
            return record

    def execute(self, job_id: int) -> bool:
        """Берёт одну готовую Job атомарно и выполняет вне транзакции.

        На StorageError сохраняет намерение перехода для следующего run_once
        и передаёт ошибку вызывающему коду. Tool повторно не запускается.
        False означает, что задача не готова или занята другим исполнителем.
        """
        execution_id = uuid4().hex
        try:
            record = self._claim(job_id, execution_id)
        except StorageError:
            # COMMIT мог пройти, хотя подтверждение записи потерялось.
            self._defer(self._storage_failure(job_id, execution_id))
            raise
        if record is None:
            return False
        if record.job.state != State.RUNNING:
            return True
        operation, params = record.job.operation, dict(record.job.params)
        correlation_id = record.correlation_id

        context = ToolContext(
            job_id,
            correlation_id,
            lambda: self._repository.get(job_id).job.is_cancelled,
        )
        try:
            if context.is_cancelled():
                return True
            tool = self._tool(operation)
            validated = tool.params_model.model_validate(params)
            snapshot = self._data_service.get_snapshot(tool.name, validated, context)
            if context.is_cancelled():
                return True
        except SnapshotError as exc:
            self._publish(self._snapshot_error(record, exc))
            return True
        except StorageError:
            self._defer(self._storage_failure(job_id, execution_id))
            raise
        except Exception:
            result = self._internal_error(context)
        else:
            try:
                result = tool.run(validated, snapshot, context)
                self._validate_result(result)
            except StorageError:
                self._defer(self._storage_failure(job_id, execution_id))
                raise
            except Exception:
                result = self._internal_error(context)

        self._publish(_PendingTransition(job_id, execution_id, result))
        return True

    @staticmethod
    def _storage_failure(job_id: int, execution_id: str) -> _PendingTransition:
        return _PendingTransition(
            job_id,
            execution_id,
            ToolResult(
                "error",
                None,
                ToolError(
                    "SERVICE_UNAVAILABLE",
                    "Выполнение прервано сбоем хранилища",
                ),
                {},
            ),
        )

    def _defer(self, transition: _PendingTransition) -> None:
        with self._pending_lock:
            self._pending[transition.job_id, transition.execution_id] = transition

    def _publish(self, transition: _PendingTransition) -> None:
        # Намерение остаётся в памяти до подтверждённого выхода из транзакции.
        self._defer(transition)
        self._save_transition(transition)

    def _save_transition(self, transition: _PendingTransition) -> None:
        with self._repository.transaction(transition.job_id) as record:
            # Проверка отмены и публикация итога защищены одной транзакцией.
            if (
                record.job.state == State.RUNNING
                and record.execution_id == transition.execution_id
            ):
                result = transition.result
                if result is None:
                    record.job.wait_quota()
                    record.quota_started_at = transition.quota_started_at
                    record.retry_at = transition.retry_at
                elif result.status == "ok":
                    record.job.finish(result)
                elif result.status == "error":
                    record.job.fail(result.error.code, result.error.message)
                else:
                    record.job.cancel()
        with self._pending_lock:
            self._pending.pop((transition.job_id, transition.execution_id), None)

    @staticmethod
    def _internal_error(context: ToolContext) -> ToolResult:
        logger.exception(
            "Ошибка выполнения Job %s, correlation_id=%s",
            context.job_id,
            context.correlation_id,
        )
        return ToolResult(
            "error",
            None,
            ToolError("INTERNAL_ERROR", "Внутренняя ошибка выполнения"),
            {},
        )

    @staticmethod
    def _validate_result(result: ToolResult) -> None:
        if not isinstance(result, ToolResult):
            raise ValueError("Tool вернул результат другого типа")
        if not isinstance(result.meta, dict):
            raise ValueError("Tool вернул некорректные метаданные")
        if result.status == "ok":
            if not isinstance(result.data, dict) or result.error is not None:
                raise ValueError("Tool вернул некорректный успешный результат")
        elif result.status in {"error", "cancelled"}:
            if result.data is not None or not isinstance(result.error, ToolError):
                raise ValueError("Tool вернул некорректную ошибку")
        else:
            raise ValueError("Tool вернул неизвестный статус")

    def _snapshot_error(
        self, record: JobRecord, error: SnapshotError
    ) -> _PendingTransition:
        if error.code != "SOURCE_RATE_LIMITED":
            result = ToolResult("error", None, ToolError(error.code, error.message), {})
            return _PendingTransition(record.job.id, record.execution_id, result)
        delay = 60 if error.retry_after_seconds is None else error.retry_after_seconds
        if not math.isfinite(delay) or delay < 0:
            result = ToolResult(
                "error",
                None,
                ToolError(
                    "SOURCE_BAD_DATA",
                    "Некорректное время ожидания квоты",
                ),
                {},
            )
            return _PendingTransition(record.job.id, record.execution_id, result)
        now = self._clock()
        remaining = self._max_quota_wait - record.quota_wait_seconds
        return _PendingTransition(
            record.job.id,
            record.execution_id,
            quota_started_at=now,
            retry_at=now + timedelta(seconds=min(delay, remaining)),
        )

    def run_once(self) -> int:
        """Сохраняет отложенные переходы и обрабатывает готовые Job.

        Сбой одной записи не блокирует остальные; возвращает число задач,
        для которых обработана попытка выполнения или отложенный переход.
        """
        processed = set()
        with self._pending_lock:
            pending = list(self._pending.values())
        for transition in pending:
            try:
                self._save_transition(transition)
                processed.add(transition.job_id)
            except StorageError:
                logger.exception(
                    "Не удалось сохранить отложенный переход Job %s", transition.job_id
                )
        for job_id in self._repository.pending_ids():
            try:
                if self.execute(job_id):
                    processed.add(job_id)
            except StorageError:
                logger.exception("Сбой хранилища при выполнении Job %s", job_id)
        return len(processed)

    def run(self, stop: Event, *, poll_interval: float = 1) -> None:
        """Фоновый цикл. Приложение запускает его в своём рабочем потоке.

        Ошибки хранилища не маскируются под ошибки Tool. Отложенные переходы
        повторяются после интервала опроса; running восстанавливается при старте.
        """
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Интервал опроса должен быть положительным")
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Ошибка фонового цикла Dispatcher")
            stop.wait(poll_interval)

    def recover(self) -> int:
        """Вызывать при старте до запуска исполнителей, после их остановки.

        Прерванные running завершаются ошибкой по API-контракту. created
        и waiting_quota подхватываются обычным циклом без сброса расписания.
        """
        count = 0
        for job_id in self._repository.running_ids():
            with self._repository.transaction(job_id) as record:
                if record.job.state == State.RUNNING:
                    record.job.fail(
                        "WORKER_INTERRUPTED", "Выполнение прервано перезапуском сервера"
                    )
                    count += 1
        return count
