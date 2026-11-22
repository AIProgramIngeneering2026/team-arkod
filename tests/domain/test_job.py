import itertools

import pytest

from arkod.domain.job import TERMINAL, TRANSITIONS, InvalidTransition, Job, State

ALL_COMMANDS = ["start", "wait_quota", "resume", "finish", "fail", "expire_quota", "cancel", "recover"]


def call(job, cmd):
    if cmd == "finish":
        return job.finish({"ok": 1})
    if cmd == "fail":
        return job.fail("INTERNAL_ERROR", "err")
    return getattr(job, cmd)()


def make(*cmds):
    j = Job(1, "compare")
    for c in cmds:
        call(j, c)
    return j


# --- обычный путь ---

def test_happy_path():
    j = make("start", "finish")
    assert j.state == State.DONE and j.result == {"ok": 1} and j.active_tools == 0


def test_quota_path():
    j = make("start", "wait_quota")
    assert j.state == State.WAITING_QUOTA and j.active_tools == 0
    j.resume()
    j.finish("r")
    assert j.state == State.DONE


def test_failed_path():
    j = make("start")
    j.fail("INTERNAL_ERROR", "boom")
    assert j.state == State.FAILED and (j.error_code, j.error_message) == ("INTERNAL_ERROR", "boom")


def test_quota_expired():
    j = make("start", "wait_quota", "expire_quota")
    assert j.state == State.FAILED and "лимит" in j.error_message


# --- все недопустимые пары отклоняются, состояние не меняется ---

# Ожидаемые переходы заданы здесь, по диаграмме из docs/requirements.md, а не берутся из
# TRANSITIONS реализации. Иначе лишний переход в Job не заметил бы ни один тест.
EXPECTED = {
    ("start", State.CREATED): State.RUNNING,
    ("wait_quota", State.RUNNING): State.WAITING_QUOTA,
    ("resume", State.WAITING_QUOTA): State.RUNNING,
    ("finish", State.RUNNING): State.DONE,
    ("fail", State.RUNNING): State.FAILED,
    ("expire_quota", State.WAITING_QUOTA): State.FAILED,
    ("cancel", State.CREATED): State.CANCELLED,
    ("cancel", State.RUNNING): State.CANCELLED,
    ("cancel", State.WAITING_QUOTA): State.CANCELLED,
    ("recover", State.RUNNING): State.CREATED,
}

INVALID = [(c, s) for c in ALL_COMMANDS for s in State
           if (c, s) not in EXPECTED and c != "recover"]

PATH_TO = {
    State.CREATED: [], State.RUNNING: ["start"], State.WAITING_QUOTA: ["start", "wait_quota"],
    State.DONE: ["start", "finish"], State.FAILED: ["start", "fail"], State.CANCELLED: ["cancel"],
}


@pytest.mark.parametrize("cmd,state", sorted(EXPECTED, key=lambda k: (k[0], k[1].value)))
def test_allowed_transition_works(cmd, state):
    j = make(*PATH_TO[state])
    call(j, cmd)
    assert j.state == EXPECTED[(cmd, state)]


def test_transition_table_matches_diagram():
    assert dict(TRANSITIONS) == EXPECTED


@pytest.mark.parametrize("cmd,state", INVALID)
def test_invalid_transition_rejected(cmd, state):
    j = make(*PATH_TO[state])
    before = (j.state, j.result, j.error_code, j.active_tools)
    with pytest.raises(InvalidTransition):
        call(j, cmd)
    assert (j.state, j.result, j.error_code, j.active_tools) == before


# --- злодейства ---

@pytest.mark.parametrize("chain", [["start", "start"], ["start", "resume"], ["start", "wait_quota", "resume", "resume"]])
def test_z1_double_work(chain):
    j = Job(1, "x")
    for c in chain[:-1]:
        call(j, c)
    with pytest.raises(InvalidTransition):
        call(j, chain[-1])
    assert j.active_tools <= 1


def test_z1_cancel_does_not_allow_second_tool():
    j = make("start", "cancel")
    with pytest.raises(InvalidTransition):
        j.start()
    with pytest.raises(InvalidTransition):
        j.resume()
    assert j.active_tools == 1  # старый Tool ещё работает, нового нет


def test_z2_restart_not_stuck_in_running():
    j = make("start")
    j.recover()
    assert j.state == State.CREATED and j.active_tools == 0
    with pytest.raises(InvalidTransition):
        j.finish("late")  # ответа от пропавшего Tool быть не должно, а если придёт — отказ
    j.start()  # Dispatcher выполняет Job заново
    assert j.state == State.RUNNING and j.active_tools == 1


def test_start_recover_start():
    j = Job(1, "x")
    j.start()
    assert j.state == State.RUNNING
    j.recover()
    assert j.state == State.CREATED and j.active_tools == 0
    j.start()  # задачу можно запустить второй раз
    assert j.state == State.RUNNING and j.active_tools == 1


def test_z2_recover_ignored_outside_running():
    for chain in ([], ["start", "wait_quota"], ["start", "finish"], ["start", "fail"], ["cancel"]):
        j = make(*chain)
        before = j.state
        j.recover()
        assert j.state == before


def test_is_cancelled_flag():
    j = make("start")
    assert not j.is_cancelled
    j.cancel()
    assert j.is_cancelled


@pytest.mark.parametrize("chain", [["start", "cancel"], ["cancel"], ["start", "wait_quota", "cancel"]])
@pytest.mark.parametrize("late", ["finish", "fail", "resume", "start", "expire_quota"])
def test_z3_cancelled_never_done(chain, late):
    j = make(*chain)
    with pytest.raises(InvalidTransition):
        call(j, late)
    assert j.state == State.CANCELLED and j.result is None


def test_z4_failed_never_done():
    j = make("start", "fail")
    with pytest.raises(InvalidTransition):
        j.finish("x")
    assert j.state == State.FAILED and j.result is None


@pytest.mark.parametrize("cmd", ["finish", "fail"])
def test_z5_no_done_without_running(cmd):
    j = Job(1, "x")
    with pytest.raises(InvalidTransition):
        call(j, cmd)
    assert j.state == State.CREATED


def test_z5_not_from_waiting_quota():
    j = make("start", "wait_quota")
    with pytest.raises(InvalidTransition):
        j.finish("x")


@pytest.mark.parametrize("cmd", ["start", "resume", "wait_quota", "cancel"])
def test_z6_done_is_final(cmd):
    j = make("start", "finish")
    with pytest.raises(InvalidTransition):
        call(j, cmd)
    assert j.state == State.DONE


# --- «атакующий»: перебор всех цепочек команд до глубины 6 ---

def test_bruteforce_invariants():
    for chain in itertools.product(ALL_COMMANDS, repeat=6):
        j = Job(1, "x")
        was_running = False
        final = None
        for c in chain:
            try:
                call(j, c)
            except InvalidTransition:
                pass
            was_running |= j.state == State.RUNNING
            assert j.active_tools <= 1                          # З1
            if final is not None:
                assert j.state == final                         # З3, З4, З6
            if j.state in TERMINAL:
                final = j.state
            if j.state == State.DONE:
                assert was_running                              # З5
            if j.state == State.RUNNING:
                assert j.active_tools == 1                      # З2: «работа» без Tool невозможна
        if final == State.DONE:
            assert j.result is not None