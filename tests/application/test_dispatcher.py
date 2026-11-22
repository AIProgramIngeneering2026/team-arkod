"""Проверки координации с репозиторием в памяти и заглушками зависимостей."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arkod.application.dispatcher import (
    Dispatcher,
    JobNotCancellable,
    ResultNotReady,
    ResultUnavailable,
    UnknownOperation,
)
from arkod.application.ports import (
    IdempotencyConflict,
    JobNotFound,
    SnapshotError,
    StorageError,
)
from arkod.domain.job import State
from arkod.domain.tools.models import ToolError, ToolResult
from arkod.infrastructure.memory_job_repository import MemoryJobRepository


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    match_id: int = Field(gt=0)


class FakeTool:
    name = "compare_teams"
    version = "1.0"
    params_model = Params

    def __init__(self):
        self.calls = []
        self.action = lambda params, snapshot, context: ToolResult(
            "ok",
            {"matches": 20},
            None,
            {"tool": self.name, "version": self.version},
        )

    def run(self, params, snapshot, context):
        self.calls.append((params, snapshot, context))
        return self.action(params, snapshot, context)


class FakeDataService:
    def __init__(self):
        self.calls = []
        self.snapshot = object()
        self.action = lambda operation, params, context: self.snapshot

    def get_snapshot(self, operation, params, context):
        self.calls.append((operation, params, context))
        return self.action(operation, params, context)


class StubRegistry:
    """Только выдача заранее заданных Tool по контракту Registry.get."""

    def __init__(self, tools):
        self.tools = tools

    def get(self, name):
        return self.tools[name]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 21, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def env():
    repository, tool, data, clock = (
        MemoryJobRepository(),
        FakeTool(),
        FakeDataService(),
        Clock(),
    )
    registry = StubRegistry({tool.name: tool})
    dispatcher = Dispatcher(
        repository, registry, data, clock=clock, max_quota_wait_seconds=120
    )
    return dispatcher, repository, tool, data, clock, registry


def create(
    dispatcher,
    *,
    match_id=1,
    key="request-00000001",
    owner="user-a",
    operation="compare_teams",
):
    return dispatcher.create(
        operation,
        {"match_id": match_id},
        owner_id=owner,
        idempotency_key=key,
        correlation_id="request-log-id",
    )


def throw(error):
    def action(*args):
        raise error

    return action


def fail_writes(repository, state, *, failures=1, after_commit=False):
    """Сбой записи с откатом или потеря подтверждения успешной записи."""
    original_transaction = repository.transaction
    remaining = {"count": failures}

    @contextmanager
    def transaction(job_id):
        with original_transaction(job_id) as record:
            yield record
            fail = record.job.state == state and remaining["count"] > 0
            if fail:
                remaining["count"] -= 1
                if not after_commit:
                    raise StorageError("Запись временно недоступна")
        if fail:
            raise StorageError("Подтверждение записи потерялось")

    repository.transaction = transaction
    return remaining


def test_create_is_separate_from_execution_and_passes_contract(env):
    dispatcher, repository, tool, data, *_ = env
    transaction_open = False
    original_transaction = repository.transaction

    @contextmanager
    def observe_transaction(job_id):
        nonlocal transaction_open
        with original_transaction(job_id) as record:
            transaction_open = True
            try:
                yield record
            finally:
                transaction_open = False

    repository.transaction = observe_transaction
    job = create(dispatcher)
    assert job.state == State.CREATED
    assert tool.calls == data.calls == []

    def calculate(params, snapshot, context):
        assert not transaction_open
        assert snapshot is data.snapshot
        assert params == Params(match_id=1)
        assert context.job_id == job.id
        assert context.correlation_id == "request-log-id"
        assert not context.is_cancelled()
        return ToolResult("ok", {"matches": 10}, None, {"version": "1.0"})

    def prepare(*args):
        assert not transaction_open
        return data.snapshot

    data.action, tool.action = prepare, calculate
    assert dispatcher.execute(job.id)
    result = dispatcher.get_result(job.id, owner_id="user-a")
    assert result.data == {"matches": 10}
    assert result.meta == {"version": "1.0"}
    assert repository.get(job.id).job.state == State.DONE
    assert not dispatcher.execute(job.id)
    assert len(tool.calls) == 1


@pytest.mark.parametrize(
    "params", [{}, {"match_id": 0}, {"match_id": "1"}, {"match_id": 1, "extra": 1}]
)
def test_invalid_params_do_not_create_job(env, params):
    dispatcher, repository, *_ = env
    with pytest.raises(ValidationError):
        dispatcher.create(
            "compare_teams",
            params,
            owner_id="u",
            idempotency_key="k",
            correlation_id="c",
        )
    assert repository.pending_ids() == []


def test_unknown_operation_does_not_create_job(env):
    dispatcher, repository, *_ = env
    with pytest.raises(UnknownOperation):
        create(dispatcher, operation="unknown")
    assert repository.pending_ids() == []


def test_dispatcher_uses_tool_returned_for_operation(env):
    dispatcher, repository, tool, data, _, registry = env
    tool.name = "predict_match"
    registry.tools["predict_outcome"] = tool
    job = create(dispatcher, operation="predict_outcome")
    dispatcher.execute(job.id)
    assert data.calls[0][0] == "predict_match"
    assert repository.get(job.id).job.operation == "predict_outcome"
    assert len(tool.calls) == 1


def test_new_tool_requires_only_registration(env):
    dispatcher, repository, _, data, _, registry = env

    class NewParams(BaseModel):
        team: str
        count: int = 5

    class NewTool:
        name = "unseen_operation"
        version = "2.0"
        params_model = NewParams

        def run(self, params, snapshot, context):
            assert snapshot is data.snapshot
            return ToolResult(
                "ok", {"team": params.team, "count": params.count}, None, {}
            )

    tool = NewTool()
    registry.tools[tool.name] = tool
    job = dispatcher.create(
        "unseen_operation",
        {"team": "Arsenal"},
        owner_id="user-a",
        idempotency_key="request-00000001",
        correlation_id="new-tool",
    )
    assert repository.get(job.id).job.params == {"team": "Arsenal", "count": 5}
    dispatcher.execute(job.id)
    assert dispatcher.get_result(job.id, owner_id="user-a").data == {
        "team": "Arsenal",
        "count": 5,
    }


def test_idempotency_owner_scope_conflict_and_new_key(env):
    dispatcher, repository, *_ = env
    first = create(dispatcher)
    assert create(dispatcher).id == first.id
    with pytest.raises(IdempotencyConflict):
        create(dispatcher, match_id=2)
    second = create(dispatcher, owner="user-b")
    third = create(dispatcher, key="request-00000002")
    assert len({first.id, second.id, third.id}) == 3
    dispatcher.execute(first.id)
    assert create(dispatcher).state == State.DONE
    assert repository.pending_ids() == [second.id, third.id]


def test_concurrent_creation_with_same_key(env):
    dispatcher, repository, *_ = env
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: create(dispatcher).id, range(24)))
    assert len(set(ids)) == len(repository.pending_ids()) == 1


def test_reading_job_does_not_expose_mutable_storage(env):
    dispatcher, repository, *_ = env
    job = create(dispatcher)
    job.params["match_id"] = 999
    job.cancel()
    assert repository.get(job.id).job.params == {"match_id": 1}
    assert repository.get(job.id).job.state == State.CREATED
    dispatcher.execute(job.id)
    result = dispatcher.get_result(job.id, owner_id="user-a")
    result.data["matches"] = 999
    assert dispatcher.get_result(job.id, owner_id="user-a").data["matches"] == 20


@pytest.mark.parametrize("method", ["get", "get_result", "cancel"])
def test_foreign_and_missing_jobs_are_not_found(env, method):
    dispatcher, *_ = env
    job = create(dispatcher)
    for job_id, owner in ((job.id, "user-b"), (999, "user-a")):
        with pytest.raises(JobNotFound, match="Задача не найдена"):
            getattr(dispatcher, method)(job_id, owner_id=owner)
    assert dispatcher.get(job.id, owner_id="user-a").state == State.CREATED


def test_result_availability_and_repeat_cancel(env):
    dispatcher, *_ = env
    job = create(dispatcher)
    with pytest.raises(ResultNotReady):
        dispatcher.get_result(job.id, owner_id="user-a")
    for _ in range(2):
        assert dispatcher.cancel(job.id, owner_id="user-a").state == State.CANCELLED
    with pytest.raises(ResultUnavailable):
        dispatcher.get_result(job.id, owner_id="user-a")
    assert not dispatcher.execute(job.id)


@pytest.mark.parametrize(
    "status,expected",
    [("ok", State.DONE), ("error", State.FAILED), ("cancelled", State.CANCELLED)],
)
def test_tool_results(env, status, expected):
    dispatcher, repository, tool, *_ = env
    tool.action = lambda *args: ToolResult(
        status,
        {"ok": True} if status == "ok" else None,
        None
        if status == "ok"
        else ToolError(
            "CANCELLED" if status == "cancelled" else "NOT_ENOUGH_DATA", "Причина"
        ),
        {},
    )
    job = create(dispatcher)
    dispatcher.execute(job.id)
    saved = repository.get(job.id).job
    assert saved.state == expected
    if status != "ok":
        assert saved.result is None
    if status == "error":
        assert saved.error_code == "NOT_ENOUGH_DATA"
    if status != "cancelled":
        with pytest.raises(JobNotCancellable):
            dispatcher.cancel(job.id, owner_id="user-a")


@pytest.mark.parametrize("stage", ["snapshot", "tool"])
def test_unhandled_errors_become_internal_error(env, stage):
    dispatcher, repository, tool, data, *_ = env
    target = data if stage == "snapshot" else tool
    target.action = throw(RuntimeError("Секретные подробности не должны попасть в Job"))
    job = create(dispatcher)
    dispatcher.execute(job.id)
    saved = repository.get(job.id).job
    assert saved.state == State.FAILED
    assert saved.error_code == "INTERNAL_ERROR"
    assert "Секретные" not in saved.error_message


@pytest.mark.parametrize(
    "result",
    [
        None,
        ToolResult("ok", None, None, {}),
        ToolResult("error", {}, None, {}),
        ToolResult("rate_limited", None, None, {}),
    ],
)
def test_invalid_tool_result_is_failed(env, result):
    dispatcher, repository, tool, *_ = env
    tool.action = lambda *args: result
    job = create(dispatcher)
    dispatcher.execute(job.id)
    assert repository.get(job.id).job.error_code == "INTERNAL_ERROR"


@pytest.mark.parametrize(
    "code", ["MATCH_NOT_FOUND", "SOURCE_UNAVAILABLE", "SOURCE_BAD_DATA"]
)
def test_snapshot_error_does_not_call_tool(env, code):
    dispatcher, repository, tool, data, *_ = env
    data.action = throw(SnapshotError(code, "Данные недоступны"))
    job = create(dispatcher)
    dispatcher.execute(job.id)
    saved = repository.get(job.id).job
    assert saved.state == State.FAILED and saved.error_code == code
    assert not tool.calls


def test_snapshot_error_raised_by_tool_is_internal_error(env):
    dispatcher, repository, tool, *_ = env
    tool.action = throw(
        SnapshotError("SOURCE_RATE_LIMITED", "Это исключение не от сервиса данных", 42)
    )
    job = create(dispatcher)
    dispatcher.execute(job.id)
    assert repository.get(job.id).job.error_code == "INTERNAL_ERROR"


@pytest.mark.parametrize("delay,seconds", [(42, 42), (None, 60)])
def test_quota_resume_not_early_and_keeps_same_job(env, delay, seconds):
    dispatcher, repository, tool, data, clock, _ = env
    job = create(dispatcher)
    data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", delay))
    assert dispatcher.run_once() == 1
    record = repository.get(job.id)
    assert record.job.state == State.WAITING_QUOTA and record.job.active_tools == 0
    assert record.retry_at == clock.now + timedelta(seconds=seconds)
    assert not tool.calls
    clock.advance(seconds - 1)
    assert dispatcher.run_once() == 0
    assert len(data.calls) == 1
    clock.advance(1)
    data.action = lambda *args: data.snapshot
    assert dispatcher.run_once() == 1
    assert repository.get(job.id).job.state == State.DONE
    assert repository.get(job.id).job.id == job.id


def test_waiting_quota_does_not_block_other_jobs(env):
    dispatcher, repository, tool, data, *_ = env
    first, second = (
        create(dispatcher),
        create(dispatcher, key="request-00000002", match_id=2),
    )

    def prepare(operation, params, context):
        if params.match_id == 1:
            raise SnapshotError("SOURCE_RATE_LIMITED", "Квота", 60)
        return data.snapshot

    data.action = prepare
    assert dispatcher.run_once() == 2
    assert repository.get(first.id).job.state == State.WAITING_QUOTA
    assert repository.get(second.id).job.state == State.DONE
    assert len(tool.calls) == 1


def test_cumulative_wait_limit_and_restart_preserve_schedule(env):
    dispatcher, repository, tool, data, clock, registry = env
    job = create(dispatcher)
    data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", 42))
    dispatcher.execute(job.id)
    # Новый экземпляр использует только сохранённые поля, без таймеров в памяти.
    dispatcher = Dispatcher(
        repository, registry, data, clock=clock, max_quota_wait_seconds=120
    )
    assert dispatcher.recover() == 0
    assert not dispatcher.execute(job.id)
    clock.advance(42)
    dispatcher.execute(job.id)
    clock.advance(42)
    dispatcher.execute(job.id)
    clock.advance(36)
    dispatcher.execute(job.id)
    saved = repository.get(job.id).job
    assert saved.state == State.FAILED and saved.error_code == "SOURCE_RATE_LIMITED"
    assert len(data.calls) == 3 and not tool.calls


@pytest.mark.parametrize("delay", [-1, float("inf"), float("nan")])
def test_invalid_quota_delay_is_bad_data(env, delay):
    dispatcher, repository, _, data, *_ = env
    data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", delay))
    job = create(dispatcher)
    dispatcher.execute(job.id)
    assert repository.get(job.id).job.error_code == "SOURCE_BAD_DATA"


def test_cancel_during_snapshot_skips_tool(env):
    dispatcher, repository, tool, data, *_ = env
    job = create(dispatcher)

    def prepare(operation, params, context):
        dispatcher.cancel(job.id, owner_id="user-a")
        assert context.is_cancelled()
        return data.snapshot

    data.action = prepare
    dispatcher.execute(job.id)
    assert repository.get(job.id).job.state == State.CANCELLED
    assert not tool.calls


@pytest.mark.parametrize("late", ["result", "exception", "quota"])
def test_cancel_during_execution_never_publishes_late_result(env, late):
    dispatcher, repository, tool, data, *_ = env
    job = create(dispatcher)

    def cancel_then_return(*args):
        dispatcher.cancel(job.id, owner_id="user-a")
        if late == "exception":
            raise RuntimeError("Поздняя ошибка")
        if late == "quota":
            raise SnapshotError("SOURCE_RATE_LIMITED", "Поздняя квота", 60)
        return ToolResult("ok", {"late": True}, None, {})

    if late == "quota":
        data.action = cancel_then_return
    else:
        tool.action = cancel_then_return
    dispatcher.execute(job.id)
    saved = repository.get(job.id).job
    assert saved.state == State.CANCELLED and saved.result is None


def test_cancel_while_waiting_quota_never_resumes(env):
    dispatcher, repository, _, data, clock, _ = env
    data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", 60))
    job = create(dispatcher)
    dispatcher.execute(job.id)
    dispatcher.cancel(job.id, owner_id="user-a")
    clock.advance(60)
    assert dispatcher.run_once() == 0
    assert repository.get(job.id).retry_at is None
    assert len(data.calls) == 1


@pytest.mark.parametrize("resume", [False, True])
def test_two_dispatchers_cannot_execute_one_job_simultaneously(env, resume):
    dispatcher, repository, tool, data, clock, registry = env
    other = Dispatcher(repository, registry, data, clock=clock)
    job = create(dispatcher)
    if resume:
        data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", 42))
        dispatcher.execute(job.id)
        clock.advance(42)
        data.action = lambda *args: data.snapshot
    entered, release = Event(), Event()

    def calculate(*args):
        entered.set()
        assert release.wait(5)
        return ToolResult("ok", {"ok": True}, None, {})

    tool.action = calculate
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(dispatcher.execute, job.id)
        try:
            assert entered.wait(5)
            assert not other.execute(job.id)
            dispatcher.cancel(job.id, owner_id="user-a")
        finally:
            release.set()
        assert running.result(timeout=5)
    assert len(tool.calls) == 1
    assert repository.get(job.id).job.state == State.CANCELLED
    assert repository.get(job.id).job.result is None


def test_recovery_fails_running_without_rerun(env):
    dispatcher, repository, tool, *_ = env
    job = create(dispatcher)
    with repository.transaction(job.id) as record:
        record.job.start()
    assert dispatcher.recover() == 1
    assert dispatcher.recover() == 0
    saved = repository.get(job.id).job
    assert saved.state == State.FAILED and saved.error_code == "WORKER_INTERRUPTED"
    assert not dispatcher.execute(job.id) and not tool.calls


def test_storage_failure_is_not_reported_as_tool_failure(env):
    dispatcher, repository, tool, data, *_ = env
    data.action = throw(StorageError("БД недоступна"))
    job = create(dispatcher)
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    assert repository.get(job.id).job.state == State.RUNNING
    assert not tool.calls
    assert dispatcher.run_once() == 1
    saved = repository.get(job.id).job
    assert saved.state == State.FAILED
    assert saved.error_code == "SERVICE_UNAVAILABLE"
    assert len(data.calls) == 1 and not tool.calls
    assert dispatcher.run_once() == 0


def test_failed_publication_does_not_persist_done_without_result(env):
    dispatcher, repository, tool, data, *_ = env
    job = create(dispatcher)
    fail_writes(repository, State.DONE, failures=2)
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    saved = repository.get(job.id).job
    assert saved.state == State.RUNNING
    assert saved.result is None
    assert dispatcher.run_once() == 0
    assert repository.get(job.id).job.state == State.RUNNING
    assert dispatcher.run_once() == 1
    result = dispatcher.get_result(job.id, owner_id="user-a")
    assert result.data == {"matches": 20}
    assert result.meta == {"tool": tool.name, "version": tool.version}
    assert len(tool.calls) == len(data.calls) == 1
    assert dispatcher.run_once() == 0


@pytest.mark.parametrize(
    "status,state", [("error", State.FAILED), ("cancelled", State.CANCELLED)]
)
def test_failed_and_cancelled_tool_results_are_retried_without_execution(
    env, status, state
):
    dispatcher, repository, tool, data, *_ = env
    tool.action = lambda *args: ToolResult(
        status, None, ToolError("REASON", "Причина"), {}
    )
    job = create(dispatcher)
    fail_writes(repository, state)
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    assert dispatcher.run_once() == 1
    assert repository.get(job.id).job.state == state
    if state == State.FAILED:
        assert repository.get(job.id).job.error_code == "REASON"
    assert len(tool.calls) == len(data.calls) == 1


@pytest.mark.parametrize(
    "kind,state",
    [
        ("ok", State.DONE),
        ("error", State.FAILED),
        ("quota", State.WAITING_QUOTA),
    ],
)
def test_cancel_discards_pending_transition(env, kind, state):
    dispatcher, repository, tool, data, *_ = env
    job = create(dispatcher)
    if kind == "error":
        tool.action = lambda *args: ToolResult(
            "error", None, ToolError("REASON", "Причина"), {}
        )
    elif kind == "quota":
        data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", 42))
    fail_writes(repository, state)
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    dispatcher.cancel(job.id, owner_id="user-a")
    assert dispatcher.run_once() == 1
    saved = repository.get(job.id).job
    assert saved.state == State.CANCELLED and saved.result is None
    assert len(tool.calls) == (0 if kind == "quota" else 1)
    assert dispatcher.run_once() == 0


@pytest.mark.parametrize("elapsed", [20, 120])
def test_deferred_quota_preserves_original_schedule_and_deadline(env, elapsed):
    dispatcher, repository, tool, data, clock, _ = env
    job = create(dispatcher)
    data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", 42))
    fail_writes(repository, State.WAITING_QUOTA)
    started_at = clock.now
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    clock.advance(elapsed)
    assert dispatcher.run_once() == 1
    record = repository.get(job.id)
    if elapsed == 120:
        assert record.job.state == State.FAILED
        assert record.job.error_code == "SOURCE_RATE_LIMITED"
        assert not tool.calls and len(data.calls) == 1
        return
    assert record.job.state == State.WAITING_QUOTA
    assert record.quota_started_at == started_at
    assert record.retry_at == started_at + timedelta(seconds=42)
    assert not tool.calls and len(data.calls) == 1
    data.action = lambda *args: data.snapshot
    clock.advance(42 - elapsed)
    dispatcher.run_once()
    assert repository.get(job.id).job.state == State.DONE
    assert len(data.calls) == 2 and len(tool.calls) == 1


def test_snapshot_failure_is_saved_after_storage_recovery(env):
    dispatcher, repository, tool, data, *_ = env
    job = create(dispatcher)
    data.action = throw(SnapshotError("MATCH_NOT_FOUND", "Матч не найден"))
    fail_writes(repository, State.FAILED)
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    dispatcher.run_once()
    assert repository.get(job.id).job.error_code == "MATCH_NOT_FOUND"
    assert len(data.calls) == 1 and not tool.calls


def test_one_storage_failure_does_not_block_other_jobs(env):
    dispatcher, repository, tool, data, *_ = env
    first = create(dispatcher)
    second = create(dispatcher, match_id=2, key="request-00000002")

    def prepare(operation, params, context):
        if params.match_id == 1:
            raise StorageError("Сбой чтения данных первой задачи")
        return data.snapshot

    data.action = prepare
    assert dispatcher.run_once() == 1
    assert repository.get(second.id).job.state == State.DONE
    assert repository.get(first.id).job.state == State.RUNNING
    dispatcher.run_once()
    assert repository.get(first.id).job.error_code == "SERVICE_UNAVAILABLE"
    assert len(tool.calls) == 1


@pytest.mark.parametrize("resume", [False, True])
def test_rolled_back_claim_can_be_taken_again_without_duplicate_calculation(
    env, resume
):
    dispatcher, repository, tool, data, clock, _ = env
    job = create(dispatcher)
    if resume:
        data.action = throw(SnapshotError("SOURCE_RATE_LIMITED", "Квота", 42))
        dispatcher.execute(job.id)
        clock.advance(42)
        data.action = lambda *args: data.snapshot
    fail_writes(repository, State.RUNNING)
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    assert repository.get(job.id).job.state == (
        State.WAITING_QUOTA if resume else State.CREATED
    )
    assert not tool.calls
    dispatcher.run_once()
    assert repository.get(job.id).job.state == State.DONE
    assert len(tool.calls) == 1


@pytest.mark.parametrize("state", [State.RUNNING, State.DONE])
def test_lost_commit_acknowledgement_does_not_repeat_tool(env, state):
    dispatcher, repository, tool, data, *_ = env
    job = create(dispatcher)
    fail_writes(repository, state, after_commit=True)
    with pytest.raises(StorageError):
        dispatcher.execute(job.id)
    dispatcher.run_once()
    saved = repository.get(job.id).job
    if state == State.RUNNING:
        assert saved.state == State.FAILED and saved.error_code == "SERVICE_UNAVAILABLE"
        assert not tool.calls and not data.calls
    else:
        assert saved.state == State.DONE and len(tool.calls) == 1
    assert dispatcher.run_once() == 0


def test_failed_claim_does_not_finalize_another_dispatchers_active_attempt(env):
    dispatcher, repository, tool, data, clock, registry = env
    other = Dispatcher(repository, registry, data, clock=clock)
    job = create(dispatcher)
    entered, release = Event(), Event()

    def calculate(*args):
        entered.set()
        assert release.wait(5)
        return ToolResult("ok", {"ok": True}, None, {})

    tool.action = calculate
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(dispatcher.execute, job.id)
        try:
            assert entered.wait(5)
            original_transaction = repository.transaction
            fail = True

            @contextmanager
            def fail_once(job_id):
                nonlocal fail
                if fail:
                    fail = False
                    raise StorageError("Не удалось прочитать запись при захвате")
                with original_transaction(job_id) as record:
                    yield record

            repository.transaction = fail_once
            with pytest.raises(StorageError):
                other.execute(job.id)
            other.run_once()
            assert repository.get(job.id).job.state == State.RUNNING
        finally:
            release.set()
        assert running.result(timeout=5)
    assert repository.get(job.id).job.state == State.DONE
    assert len(tool.calls) == 1


def test_background_loop_accepts_stop_event(env):
    dispatcher, repository, tool, *_ = env
    stop = Event()
    job = create(dispatcher)

    def calculate(*args):
        stop.set()
        return ToolResult("ok", {}, None, {})

    tool.action = calculate
    dispatcher.run(stop, poll_interval=0.01)
    assert repository.get(job.id).job.state == State.DONE
