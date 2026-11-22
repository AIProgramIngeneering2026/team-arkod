"""Порты Dispatcher. Реализации хранения и загрузки данных подключаются извне."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

from pydantic import BaseModel

from arkod.domain.job import Job
from arkod.domain.tool import MatchSnapshot, Tool, ToolContext


class DispatcherError(Exception):
    code = "INTERNAL_ERROR"


class JobNotFound(DispatcherError):
    code = "NOT_FOUND"


class IdempotencyConflict(DispatcherError):
    code = "IDEMPOTENCY_KEY_REUSED"


class StorageError(DispatcherError):
    """Сбой хранилища; нельзя выдавать его за ошибку расчёта Tool."""

    code = "SERVICE_UNAVAILABLE"


class SnapshotError(Exception):
    """Ошибка сервиса данных до вызова Tool, включая ожидание квоты."""

    def __init__(
        self, code: str, message: str, retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


@dataclass
class JobRecord:
    job: Job
    owner_id: str
    correlation_id: str
    retry_at: datetime | None = None
    quota_started_at: datetime | None = None
    quota_wait_seconds: float = 0
    execution_id: str | None = None  # Маркер текущей попытки захвата Job.


class JobRepository(Protocol):
    """Все поля JobRecord, включая результат и расписание, сохраняются вместе.

    Чтение возвращает отдельный снимок записи. transaction блокирует запись,
    выдаёт изменяемую копию и атомарно сохраняет её при успешном выходе;
    исключение в теле транзакции откатывает изменения. Блокировка общая для
    всех исполнителей. StorageError при завершении транзакции может означать
    потерю подтверждения уже сохранённой записи: состояние нужно перечитать.
    execution_id сохраняется вместе с переходом в running и отличает попытки.
    Сбои доступа поднимают StorageError. Нельзя держать транзакцию во время
    загрузки снимка или расчёта Tool.
    """

    def create_or_get(
        self, *, owner_id: str, idempotency_key: str, operation: str,
        params: dict[str, Any], correlation_id: str,
    ) -> JobRecord:
        """Атомарно создаёт Job и ключ в области owner_id.

        Повтор ключа с теми же operation и нормализованными params возвращает
        прежнюю запись, с другими — IdempotencyConflict. В будущей реализации
        БД нужны уникальное ограничение (owner_id, key) и одна транзакция.
        """
        ...

    def get(self, job_id: int) -> JobRecord:
        """Возвращает запись либо поднимает JobNotFound."""
        ...

    def transaction(self, job_id: int) -> AbstractContextManager[JobRecord]: ...

    def pending_ids(self) -> Iterable[int]:
        """Кандидаты в created/waiting_quota; актуальность проверит Dispatcher."""
        ...

    def running_ids(self) -> Iterable[int]: ...


class Registry(Protocol):
    def get(self, name: str) -> Tool:
        """Возвращает Tool либо поднимает KeyError для неизвестного имени."""
        ...


class FootballDataService(Protocol):
    def get_snapshot(
        self, operation: str, params: BaseModel, context: ToolContext,
    ) -> MatchSnapshot:
        """Готовит снимок или поднимает SnapshotError.

        operation — имя Tool. Признак отмены проверяется перед каждым
        обращением к источнику, в том числе при повторах адаптера.
        """
        ...
