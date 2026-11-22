from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, StrictInt, ValidationError


@dataclass(frozen=True)
class Team:
    id: int
    name: str


@dataclass(frozen=True)
class Match:
    id: int
    league: str
    home: Team
    away: Team
    kickoff: datetime | None
    status: Literal["scheduled", "in_play", "finished", "cancelled"]


@dataclass(frozen=True)
class PlayedMatch:
    id: int
    league: str
    finished_at: datetime
    home_id: int
    away_id: int
    home_goals: int
    away_goals: int


@dataclass(frozen=True)
class MatchSnapshot:
    match: Match
    history: tuple[PlayedMatch, ...]
    source: str
    data_updated_at: datetime


@dataclass(frozen=True)
class ToolContext:
    job_id: int
    correlation_id: str
    is_cancelled: Callable[[], bool]


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str


@dataclass(frozen=True)
class ToolResult:
    status: Literal["ok", "error", "cancelled"]
    data: dict[str, Any] | None
    error: ToolError | None
    meta: dict[str, Any]


class MatchParams(BaseModel):
    match_id: StrictInt


class Tool(Protocol):
    name: str
    version: str
    params_model: type[BaseModel]

    def run(
        self, params: BaseModel, snapshot: MatchSnapshot, context: ToolContext
    ) -> ToolResult: ...


class InvalidParameters(ValueError):
    code = "invalid_parameters"

    def __init__(self, field: str):
        self.field = field
        super().__init__(f"Неверные параметры: {field}")


def validate_params(tool: Tool, raw: object) -> BaseModel:
    """Вызывается до создания Job, когда Registry уже выбрал Tool."""
    try:
        return tool.params_model.model_validate(raw)
    except ValidationError as exc:
        location = exc.errors()[0]["loc"]
        field = ".".join(map(str, location)) or "params"
        raise InvalidParameters(field) from exc
