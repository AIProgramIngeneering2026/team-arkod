import json
import math
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from arkod.domain.tool import (
    CompareTeams,
    InvalidParameters,
    Match,
    MatchSnapshot,
    PlayedMatch,
    PredictMatch,
    Team,
    ToolContext,
    validate_params,
)

KICKOFF = datetime(2026, 11, 21, 15, tzinfo=timezone.utc)
NOW = KICKOFF - timedelta(hours=2)
HOME = Team(1, "Arsenal FC")
AWAY = Team(2, "Chelsea FC")
TARGET = Match(538012, "PL", HOME, AWAY, KICKOFF, "scheduled")
CONTEXT = ToolContext(1, "test-request", lambda: False)


def played(match_id, home, away, home_goals, away_goals):
    return PlayedMatch(
        match_id, "PL", NOW - timedelta(days=1), home, away, home_goals, away_goals
    )


def snapshot(history, match=TARGET):
    return MatchSnapshot(match, tuple(history), "test-fixture", NOW)


def run_tool(tool, data, context=CONTEXT):
    params = validate_params(tool, {"match_id": data.match.id})
    return tool.run(params, data, context)


@pytest.fixture(
    params=[CompareTeams, lambda: PredictMatch(clock=lambda: NOW)],
    ids=["compare_teams", "predict_match"],
)
def tool(request):
    """Запускает общий для обоих Tool тест с каждым инструментом по очереди."""
    return request.param()


def test_comparison_counts_home_and_away_appearances_for_both_teams():
    history = (
        [played(i, 1, 2, 2, 1) for i in range(5)]
        + [played(i, 2, 1, 1, 1) for i in range(5, 8)]
        + [played(i, 2, 1, 3, 0) for i in range(8, 10)]
    )
    result = run_tool(CompareTeams(), snapshot(history))

    assert result.status == "ok"
    assert result.error is None
    assert result.data["match"] == {
        "id": TARGET.id, "home_team": HOME.name, "away_team": AWAY.name,
        "kickoff": "2026-11-21T15:00:00Z",
    }
    assert result.data["home"] == {
        "team": HOME.name, "matches": 10, "wins": 5, "draws": 3, "losses": 2,
        "goals_for": 13, "goals_against": 14,
    }
    assert result.data["away"] == {
        "team": AWAY.name, "matches": 10, "wins": 2, "draws": 3, "losses": 5,
        "goals_for": 14, "goals_against": 13,
    }
    assert result.meta == {
        "tool": "compare_teams", "version": "1.0", "source": "test-fixture",
        "used_match_ids": list(range(10)), "cutoff": "2026-11-21T15:00:00Z",
        "data_updated_at": "2026-11-21T13:00:00Z",
    }
    json.dumps(asdict(result), allow_nan=False)


def test_only_distinct_relevant_games_finished_before_kickoff_are_used(tool):
    eligible = [played(i, 1, 2, 1, 0) for i in range(10)]
    other = played(100, 1, 2, 9, 9)
    history = eligible + [
        eligible[0], replace(other, finished_at=KICKOFF),
        replace(other, id=101, finished_at=KICKOFF + timedelta(seconds=1)),
        replace(other, id=102, league="CL"), replace(other, id=TARGET.id),
        replace(other, id=103, home_id=3, away_id=4),
    ]
    result = run_tool(tool, snapshot(history))

    assert result == run_tool(tool, snapshot(eligible))
    assert result.meta["used_match_ids"] == list(range(10))


def test_nine_games_for_one_team_are_insufficient(tool):
    history = [played(i, 1, 2, 0, 0) for i in range(9)]
    history.append(played(9, 1, 3, 1, 0))

    result = run_tool(tool, snapshot(history))

    assert result.status == "error"
    assert result.data is None
    assert result.error.code == "NOT_ENOUGH_DATA"
    assert AWAY.name in result.error.message


def test_prediction_agrees_with_an_independent_sum_over_scorelines():
    history = [played(i, 1, 3, 2, 0) for i in range(10)]
    history += [played(i, 2, 4, 1, 4) for i in range(10, 22)]
    result = run_tool(PredictMatch(clock=lambda: NOW), snapshot(history))

    assert result.status == "ok"
    assert result.data["expected_goals"] == {"home": 3.0, "away": 0.5}
    assert result.meta["method"] == "poisson"
    probabilities = result.data["probabilities"]
    expected = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
    for home_goals in range(30):
        home_mass = math.exp(-3.0) * 3.0**home_goals / math.factorial(home_goals)
        for away_goals in range(30):
            away_mass = math.exp(-0.5) * 0.5**away_goals / math.factorial(away_goals)
            if home_goals > away_goals:
                outcome = "home_win"
            elif home_goals == away_goals:
                outcome = "draw"
            else:
                outcome = "away_win"
            expected[outcome] += home_mass * away_mass

    assert all(0.0 <= value <= 1.0 for value in probabilities.values())
    assert probabilities == pytest.approx(expected, rel=0, abs=1e-10)
    assert sum(probabilities.values()) == pytest.approx(1.0, rel=0, abs=1e-6)
    json.dumps(asdict(result), allow_nan=False)


@pytest.mark.parametrize(
    ("home_goals", "away_goals", "expected"),
    [
        (0, 0, {"home_win": 0.0, "draw": 1.0, "away_win": 0.0}),
        (2, 0, {"home_win": 1 - math.exp(-2), "draw": math.exp(-2), "away_win": 0.0}),
        (0, 2, {"home_win": 0.0, "draw": math.exp(-2), "away_win": 1 - math.exp(-2)}),
    ],
    ids=["both-zero", "away-zero", "home-zero"],
)
def test_zero_scoring_rates(home_goals, away_goals, expected):
    data = snapshot([played(i, 1, 2, home_goals, away_goals) for i in range(10)])
    result = run_tool(PredictMatch(clock=lambda: NOW), data)

    assert result.status == "ok"
    assert result.data["probabilities"] == pytest.approx(expected, abs=1e-10)


@pytest.mark.parametrize(
    "match",
    [
        replace(TARGET, kickoff=NOW),
        replace(TARGET, kickoff=NOW - timedelta(seconds=1)),
        replace(TARGET, kickoff=None),
        replace(TARGET, status="cancelled"),
        replace(TARGET, status="in_play"),
        replace(TARGET, status="finished"),
    ],
    ids=["at-kickoff", "past-kickoff", "unknown-kickoff", "cancelled", "in-play", "finished"],
)
def test_prediction_rejects_unavailable_match(match):
    result = run_tool(PredictMatch(clock=lambda: NOW), snapshot([], match))

    assert result.status == "error"
    assert result.error.code == "PREDICTION_NOT_AVAILABLE"


def test_completed_target_can_be_compared():
    history = [played(i, 1, 2, 1, 1) for i in range(10)]
    data = snapshot(history, replace(TARGET, status="finished"))

    assert run_tool(CompareTeams(), data).status == "ok"


def test_unsupported_league(tool):
    result = run_tool(tool, snapshot([], replace(TARGET, league="CL")))

    assert result.status == "error"
    assert result.error.code == "LEAGUE_NOT_SUPPORTED"


@pytest.mark.parametrize(
    "raw",
    [{}, {"match_id": "abc"}, {"match_id": 4.5}, {"match_id": True}],
    ids=["missing", "string", "float", "boolean"],
)
def test_invalid_parameters_name_the_field_before_run(tool, raw):
    with pytest.raises(InvalidParameters) as caught:
        validate_params(tool, raw)

    assert caught.value.field == "match_id"
    assert caught.value.code == "invalid_parameters"


@pytest.mark.parametrize("completed_checks", [0, 3], ids=["before-run", "during-history"])
def test_cancelled_before_calculation_and_while_processing_history(tool, completed_checks):
    data = snapshot([played(i, 1, 2, 2, 1) for i in range(10)])
    answers = iter([False] * completed_checks + [True])
    context = replace(CONTEXT, is_cancelled=lambda: next(answers))

    result = run_tool(tool, data, context)

    assert result.status == "cancelled"
    assert result.error.code == "CANCELLED"
    assert result.data is None


def test_unexpected_exception_becomes_internal_error(tool):
    def broken_callback():
        raise RuntimeError("unexpected error")

    context = replace(CONTEXT, is_cancelled=broken_callback)
    result = run_tool(tool, snapshot([]), context)

    assert result.status == "error"
    assert result.data is None
    assert result.error.code == "INTERNAL_ERROR"


def test_wrong_snapshot_is_rejected(tool):
    params = validate_params(tool, {"match_id": TARGET.id + 1})
    result = tool.run(params, snapshot([]), CONTEXT)

    assert result.status == "error"
    assert result.error.code == "INTERNAL_ERROR"
