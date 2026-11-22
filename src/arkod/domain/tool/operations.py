from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import exp, expm1
from typing import Any

from pydantic import BaseModel
from scipy.stats import skellam

from .models import Match, MatchParams, MatchSnapshot, ToolContext, ToolError, ToolResult


MIN_MATCHES = 10


class _CalculationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _check_cancellation(context: ToolContext) -> None:
    if context.is_cancelled():
        raise _CalculationError("CANCELLED", "Пользователь отменил задачу")


def _utc_string(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("В снимке требуется дата с часовым поясом")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _TeamStats:
    team: str
    matches: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0

    def record(self, scored: int, conceded: int) -> None:
        self.matches += 1
        self.goals_for += scored
        self.goals_against += conceded
        if scored > conceded:
            self.wins += 1
        elif scored == conceded:
            self.draws += 1
        else:
            self.losses += 1


def _summarize(
    snapshot: MatchSnapshot, cutoff: datetime, context: ToolContext
) -> tuple[_TeamStats, _TeamStats, list[int]]:
    target = snapshot.match
    home = _TeamStats(target.home.name)
    away = _TeamStats(target.away.name)
    teams = {target.home.id: home, target.away.id: away}
    used_ids: set[int] = set()
    for game in snapshot.history:
        _check_cancellation(context)
        if (
            game.league != target.league
            or game.finished_at >= cutoff
            or game.id == target.id
            or game.id in used_ids
        ):
            continue
        if game.home_id not in teams and game.away_id not in teams:
            continue
        if game.home_goals < 0 or game.away_goals < 0:
            raise ValueError("В снимке указан отрицательный счёт")
        if game.home_id in teams:
            teams[game.home_id].record(game.home_goals, game.away_goals)
        if game.away_id in teams:
            teams[game.away_id].record(game.away_goals, game.home_goals)
        used_ids.add(game.id)

    for team in (home, away):
        if team.matches < MIN_MATCHES:
            raise _CalculationError(
                "NOT_ENOUGH_DATA",
                f"Для команды {team.team} найдено {team.matches} сыгранных матчей, "
                f"нужно не меньше {MIN_MATCHES}",
            )
    return home, away, sorted(used_ids)


class _FootballTool:
    name: str
    version = "1.0"
    params_model: type[BaseModel] = MatchParams
    method: str | None = None

    def _check_match(self, match: Match) -> None:
        if match.league != "PL":
            raise _CalculationError("LEAGUE_NOT_SUPPORTED", "Матч не из АПЛ")

    def _calculate(self, home: _TeamStats, away: _TeamStats) -> dict[str, Any]:
        raise NotImplementedError

    def run(
        self, params: BaseModel, snapshot: MatchSnapshot, context: ToolContext
    ) -> ToolResult:
        meta = {"tool": self.name, "version": self.version, "source": snapshot.source}
        try:
            _check_cancellation(context)
            match = snapshot.match
            if not isinstance(params, MatchParams) or params.match_id != match.id:
                raise ValueError("Параметры и снимок должны относиться к одному матчу")
            self._check_match(match)
            if match.kickoff is None:
                raise ValueError("В снимке для сравнения отсутствует время отсечения")
            cutoff = _utc_string(match.kickoff)
            home, away, used_ids = _summarize(snapshot, match.kickoff, context)
            data = {
                "match": {
                    "id": match.id,
                    "home_team": match.home.name,
                    "away_team": match.away.name,
                    "kickoff": cutoff,
                },
                **self._calculate(home, away),
            }
            _check_cancellation(context)
            result_meta = {
                **meta,
                "cutoff": cutoff,
                "data_updated_at": _utc_string(snapshot.data_updated_at),
                "used_match_ids": used_ids,
            }
            if self.method is not None:
                result_meta["method"] = self.method
            return ToolResult("ok", data, None, result_meta)
        except _CalculationError as exc:
            status = "cancelled" if exc.code == "CANCELLED" else "error"
            return ToolResult(status, None, ToolError(exc.code, str(exc)), meta)
        except Exception:
            error = ToolError("INTERNAL_ERROR", "Внутренняя ошибка расчёта")
            return ToolResult("error", None, error, meta)


class CompareTeams(_FootballTool):
    name = "compare_teams"

    def _calculate(self, home: _TeamStats, away: _TeamStats) -> dict[str, Any]:
        return {"home": asdict(home), "away": asdict(away)}


def _current_time() -> datetime:
    return datetime.now(timezone.utc)


def _outcome_probabilities(home_mean: float, away_mean: float) -> dict[str, float]:
    # Разность двух независимых пуассоновских величин имеет распределение Скеллама.
    # При нулевом среднем соответствующая команда всегда забивает ноль голов.
    if home_mean == 0:
        return {"home_win": 0.0, "draw": exp(-away_mean), "away_win": -expm1(-away_mean)}
    if away_mean == 0:
        return {"home_win": -expm1(-home_mean), "draw": exp(-home_mean), "away_win": 0.0}
    difference = skellam(home_mean, away_mean)
    return {
        "home_win": float(difference.sf(0)),
        "draw": float(difference.pmf(0)),
        "away_win": float(difference.cdf(-1)),
    }


class PredictMatch(_FootballTool):
    name = "predict_match"
    method = "poisson"

    def __init__(self, clock: Callable[[], datetime] = _current_time):
        self._clock = clock

    def _check_match(self, match: Match) -> None:
        super()._check_match(match)
        if match.status == "cancelled":
            reason = "Матч отменён"
        elif match.kickoff is None:
            reason = "Время начала матча неизвестно"
        elif match.status != "scheduled" or match.kickoff <= self._clock():
            reason = "Матч уже начался"
        else:
            return
        raise _CalculationError("PREDICTION_NOT_AVAILABLE", f"{reason}, прогноз недоступен")

    def _calculate(self, home: _TeamStats, away: _TeamStats) -> dict[str, Any]:
        home_mean = (home.goals_for / home.matches + away.goals_against / away.matches) / 2
        away_mean = (away.goals_for / away.matches + home.goals_against / home.matches) / 2
        return {
            "probabilities": _outcome_probabilities(home_mean, away_mean),
            "expected_goals": {"home": home_mean, "away": away_mean},
        }
