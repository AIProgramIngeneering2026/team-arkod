# Контракт Tool
## Что Tool получает

| Поле | Что это |
|---|---|
| `params` | Параметры операции в виде JSON. У каждого Tool свой набор, описан ниже |
| `context.job_id` | Номер задачи, которую выполняет Tool |
| `context.correlation_id` | Метка для логов, чтобы найти все записи по одному запросу |
| `context.is_cancelled()` | Проверка: не отменил ли пользователь задачу |

## Что Tool возвращает: ToolResult

Любой Tool возвращает объект одного и того же вида:

| Поле | Что это |
|---|---|
| `status` | Итог: `ok`, `error`, `rate_limited` или `cancelled` |
| `data` | Результат, если `status = ok`; иначе `null` |
| `error` | Описание ошибки: `code` и `message`; если всё хорошо — `null` |
| `meta` | Сведения о расчёте: название и версия Tool, источник данных, время обновления данных и другое |

Что делает Dispatcher с задачей по каждому статусу:

| `status` | Что значит | Статус Job |
|---|---|---|
| `ok` | Результат готов | `done` |
| `error` | Ошибка, результата не будет | `failed` |
| `rate_limited` | Внешний API временно не пускает (ответ 429) | `waiting_quota` |
| `cancelled` | Пользователь отменил задачу | `cancelled` |

Tool сам не меняет статус Job. Он только возвращает ToolResult, а статус меняет Dispatcher.

Tool не выбрасывает исключения наружу: любую ошибку он возвращает в ToolResult. Если исключение всё же вылетело, Dispatcher считает это ошибкой `INTERNAL_ERROR`, и Job переходит в `failed`.

## Перечень ошибок

| Код | Когда возникает | `status` |
|---|---|---|
| `MATCH_NOT_FOUND` | Матча с таким номером нет | `error` |
| `LEAGUE_NOT_SUPPORTED` | Матч не из АПЛ | `error` |
| `PREDICTION_NOT_AVAILABLE` | Матч уже начался, отменён или время начала неизвестно (только `predict_match`) | `error` |
| `NOT_ENOUGH_DATA` | Сыгранных матчей меньше минимума, нужного для расчёта | `error` |
| `SOURCE_UNAVAILABLE` | Внешний API не ответил или вернул 5xx после всех повторных попыток | `error` |
| `SOURCE_BAD_DATA` | Внешний API вернул битый или неполный ответ | `error` |
| `SOURCE_RATE_LIMITED` | Внешний API ответил 429; в ошибке указано, сколько секунд ждать | `rate_limited` |
| `CANCELLED` | Пользователь отменил задачу во время выполнения | `cancelled` |
| `INTERNAL_ERROR` | Непредвиденная ошибка в самом Tool | `error` |

Ошибка неверного формата параметров (422) в этот список не входит: она возникает до создания Job, и Tool её не видит.

## Интерфейс в коде

```python
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel


@dataclass
class ToolContext:
    job_id: int
    correlation_id: str
    is_cancelled: Callable[[], bool]


@dataclass
class ToolError:
    code: str
    message: str
    retry_after_seconds: int | None = None  # только для SOURCE_RATE_LIMITED


@dataclass
class ToolResult:
    status: Literal["ok", "error", "rate_limited", "cancelled"]
    data: dict[str, Any] | None
    error: ToolError | None
    meta: dict[str, Any]


class Tool(Protocol):
    name: str                      # название для Registry, например "compare_teams"
    version: str                   # версия метода расчёта
    params_model: type[BaseModel]  # схема параметров: по ней API проверяет формат

    def run(self, params: BaseModel, context: ToolContext) -> ToolResult: ...
```

## Tool 1. `compare_teams` — сравнение статистики команд

Считает для обеих команд выбранного матча: число матчей, победы, ничьи, поражения, забитые и пропущенные мячи. Берёт только матчи, сыгранные до начала выбранного матча.

**Параметры:**

| Поле | Тип | Обязательное | Что это |
|---|---|---|---|
| `match_id` | целое число | да | Номер матча во внешнем API |

**Пример запроса:**

```json
{
  "match_id": 538012
}
```

**Пример успешного результата** (числа условные):

```json
{
  "status": "ok",
  "data": {
    "match": {
      "id": 538012,
      "home_team": "Arsenal FC",
      "away_team": "Chelsea FC",
      "kickoff": "2026-11-21T15:00:00Z"
    },
    "home": {
      "team": "Arsenal FC",
      "matches": 20,
      "wins": 12,
      "draws": 5,
      "losses": 3,
      "goals_for": 38,
      "goals_against": 17
    },
    "away": {
      "team": "Chelsea FC",
      "matches": 20,
      "wins": 9,
      "draws": 6,
      "losses": 5,
      "goals_for": 31,
      "goals_against": 22
    }
  },
  "error": null,
  "meta": {
    "tool": "compare_teams",
    "version": "1.0",
    "source": "football-data.org",
    "cutoff": "2026-11-21T15:00:00Z",
    "data_updated_at": "2026-11-14T13:30:00Z",
    "used_match_ids": [537801, 537815, 537829]
  }
}
```

`cutoff` — время начала выбранного матча: в расчёт попали только матчи до него. `used_match_ids` в примере сокращён, в настоящем ответе в нём все использованные матчи.

**Пример ошибки:**

```json
{
  "status": "error",
  "data": null,
  "error": {
    "code": "NOT_ENOUGH_DATA",
    "message": "Для команды Chelsea FC найдено 3 сыгранных матча, нужно не меньше 10"
  },
  "meta": {
    "tool": "compare_teams",
    "version": "1.0",
    "source": "football-data.org"
  }
}
```

## Tool 2. `predict_match` — прогноз исхода матча

Считает вероятности трёх исходов: победа хозяев, ничья, победа гостей. В первой версии используется простой статистический метод (распределение Пуассона по среднему числу голов команд). ML-модель запланирована на следующий семестр и будет подключена как ещё один Tool по этому же контракту.

**Параметры:**

| Поле | Тип | Обязательное | Что это |
|---|---|---|---|
| `match_id` | целое число | да | Номер будущего матча во внешнем API |

**Пример запроса:**

```json
{
  "match_id": 538012
}
```

**Пример успешного результата** (числа условные):

```json
{
  "status": "ok",
  "data": {
    "match": {
      "id": 538012,
      "home_team": "Arsenal FC",
      "away_team": "Chelsea FC",
      "kickoff": "2026-11-21T15:00:00Z"
    },
    "probabilities": {
      "home_win": 0.48,
      "draw": 0.27,
      "away_win": 0.25
    },
    "expected_goals": {
      "home": 1.62,
      "away": 1.08
    }
  },
  "error": null,
  "meta": {
    "tool": "predict_match",
    "version": "1.0",
    "method": "poisson",
    "source": "football-data.org",
    "cutoff": "2026-11-21T15:00:00Z",
    "data_updated_at": "2026-11-14T13:30:00Z",
    "used_match_ids": [537801, 537815, 537829]
  }
}
```

Каждая вероятность — от 0 до 1, сумма трёх равна 1 (допустимое расхождение — не больше 0.000001).

**Пример ошибки:**

```json
{
  "status": "error",
  "data": null,
  "error": {
    "code": "PREDICTION_NOT_AVAILABLE",
    "message": "Матч уже начался, прогноз недоступен"
  },
  "meta": {
    "tool": "predict_match",
    "version": "1.0",
    "source": "football-data.org"
  }
}
```

## Пример ответа при лимите внешнего API

Одинаков для обоих Tool:

```json
{
  "status": "rate_limited",
  "data": null,
  "error": {
    "code": "SOURCE_RATE_LIMITED",
    "message": "Внешний API временно ограничил запросы",
    "retry_after_seconds": 42
  },
  "meta": {
    "tool": "compare_teams",
    "version": "1.0",
    "source": "football-data.org"
  }
}
```