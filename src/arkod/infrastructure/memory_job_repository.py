"""Репозиторий Job в памяти. Данные живут до завершения процесса."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from threading import RLock
from typing import Any, Iterator

from arkod.application.ports import IdempotencyConflict, JobNotFound, JobRecord
from arkod.domain.job import Job, State


def _copy_record(record: JobRecord) -> JobRecord:
    # Данные копируются отдельно от RLock доменной Job.
    job = Job(record.job.id, record.job.operation, deepcopy(record.job.params))
    for field in ("state", "result", "error_code", "error_message", "active_tools"):
        setattr(job, field, deepcopy(getattr(record.job, field)))
    return replace(record, job=job)


class MemoryJobRepository:
    """Короткие транзакции сериализованы общей блокировкой.

    Пользователь получает отдельные снимки. Исключение внутри transaction
    не меняет сохранённые данные; состояние и итог сохраняются вместе.
    """

    def __init__(self):
        self._records: dict[int, JobRecord] = {}
        self._keys: dict[tuple[str, str], int] = {}
        self._lock = RLock()

    def create_or_get(
        self, *, owner_id: str, idempotency_key: str, operation: str,
        params: dict[str, Any], correlation_id: str,
    ) -> JobRecord:
        with self._lock:
            key = owner_id, idempotency_key
            if key in self._keys:
                record = self._records[self._keys[key]]
                if (record.job.operation, record.job.params) != (operation, params):
                    raise IdempotencyConflict("Ключ уже использован с другими параметрами")
            else:
                job_id = len(self._records) + 1
                record = JobRecord(
                    Job(job_id, operation, deepcopy(params)), owner_id, correlation_id,
                )
                self._records[job_id] = record
                self._keys[key] = job_id
            return _copy_record(record)

    def get(self, job_id: int) -> JobRecord:
        with self._lock:
            if job_id not in self._records:
                raise JobNotFound("Задача не найдена")
            return _copy_record(self._records[job_id])

    @contextmanager
    def transaction(self, job_id: int) -> Iterator[JobRecord]:
        with self._lock:
            record = self.get(job_id)
            yield record
            self._records[job_id] = _copy_record(record)

    def pending_ids(self) -> list[int]:
        with self._lock:
            return [job_id for job_id, record in self._records.items()
                    if record.job.state in {State.CREATED, State.WAITING_QUOTA}]

    def running_ids(self) -> list[int]:
        with self._lock:
            return [job_id for job_id, record in self._records.items()
                    if record.job.state == State.RUNNING]
