"""Job: единица работы с жизненным циклом. Без зависимостей от фреймворка и БД.

created -> running -> done | failed
running <-> waiting_quota (429), waiting_quota -> failed (предел ожидания)
created | running | waiting_quota -> cancelled
running -> created (перезапуск сервера: Tool пропал, Dispatcher выполнит Job заново, ADR-0002)
Конечные: done, failed, cancelled.
"""
import threading
from enum import Enum


class State(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_QUOTA = "waiting_quota"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {State.DONE, State.FAILED, State.CANCELLED}

# (команда, из состояния) -> в состояние. Всё, чего здесь нет, — отказ.
TRANSITIONS = {
    ("start", State.CREATED): State.RUNNING,
    ("wait_quota", State.RUNNING): State.WAITING_QUOTA,
    ("resume", State.WAITING_QUOTA): State.RUNNING,
    ("finish", State.RUNNING): State.DONE,
    ("fail", State.RUNNING): State.FAILED,
    ("expire_quota", State.WAITING_QUOTA): State.FAILED,
    ("cancel", State.CREATED): State.CANCELLED,
    ("cancel", State.RUNNING): State.CANCELLED,
    ("cancel", State.WAITING_QUOTA): State.CANCELLED,
    ("recover", State.RUNNING): State.CREATED,  # Tool пропал вместе с сервером
}

# Команды, после которых запускается ещё один Tool.
STARTS_TOOL = {"start", "resume"}
# Команды, после которых работающего Tool нет (итог пришёл, 429, перезапуск).
STOPS_TOOL = {"finish", "fail", "expire_quota", "wait_quota", "recover"}


class JobError(Exception):
    code = "job_error"


class InvalidTransition(JobError):
    code = "invalid_transition"

    def __init__(self, command, state):
        super().__init__(f"команда '{command}' недопустима в состоянии '{state.value}'")
        self.command, self.state = command, state


class Job:
    def __init__(self, job_id: int, operation: str, params: dict | None = None):
        self.id = job_id
        self.operation = operation
        self.params = params or {}
        self.state = State.CREATED
        self.result = None
        self.error_code = None
        self.error_message = None
        self.active_tools = 0  # сколько Tool сейчас работает (отмена их не останавливает)
        self._lock = threading.RLock()

    @property
    def is_cancelled(self) -> bool:
        """Для ToolContext.is_cancelled: Tool проверяет, не отменили ли задачу."""
        return self.state == State.CANCELLED

    def _apply(self, command, *, result=None, error=None):
        """Единственное место, где меняется состояние. Проверка и изменение атомарны."""
        with self._lock:
            target = TRANSITIONS.get((command, self.state))
            if target is None:
                raise InvalidTransition(command, self.state)
            if command in STARTS_TOOL:
                if self.active_tools:
                    raise InvalidTransition(command, self.state)  # Tool уже работает
                self.active_tools += 1
            elif command in STOPS_TOOL:
                self.active_tools = 0
            if command == "finish":
                self.result = result
            if error is not None:
                self.error_code, self.error_message = error
            self.state = target

    def start(self):
        self._apply("start")

    def wait_quota(self):
        self._apply("wait_quota")

    def resume(self):
        self._apply("resume")

    def finish(self, result):
        self._apply("finish", result=result)

    def fail(self, code: str, message: str):
        self._apply("fail", error=(code, message))

    def expire_quota(self):
        self._apply("expire_quota", error=("SOURCE_RATE_LIMITED", "Превышен лимит запросов к источнику"))

    def cancel(self):
        self._apply("cancel")

    def recover(self):
        """Перезапуск сервера: работающий Tool пропал, ответа не будет.

        Не бросает ошибку: это событие, а не команда клиента. В `waiting_quota` время
        возобновления хранится в БД, поэтому состояние не меняется.
        """
        with self._lock:
            if self.state == State.RUNNING:
                self._apply("recover")