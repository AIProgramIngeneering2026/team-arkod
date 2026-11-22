# Контракт Tool

Операции `compare_teams` и `predict_match` используют один контракт. Dispatcher получает у сервиса футбольных данных снимок для выбранного матча и передаёт его в Tool. Tool выполняет расчёт по этому снимку и возвращает `ToolResult`. За переходы Job отвечает Dispatcher. Допустимость каждого перехода проверяет сама Job.

## Что Tool получает

| Поле | Что это |
|---|---|
| `params` | Параметры операции, проверенные по схеме `params_model` выбранного Tool |
| `snapshot` | Зафиксированный на время расчёта набор данных: выбранный матч и команды, момент отсечения, история сыгранных матчей со счётом и датами, источник и время обновления данных |
| `context.job_id` | Номер задачи, которую выполняет Tool |
| `context.correlation_id` | Метка для логов, чтобы найти все записи по одному запросу |
| `context.is_cancelled()` | Проверка, отменил ли пользователь задачу |

Вызывающая сторона проверяет входной JSON по `params_model` до создания Job. Например, если в запросе отсутствует `match_id`, она возвращает ошибку «неверные параметры» с указанием поля `match_id`. В HTTP API этой ошибке соответствует ответ 422. До вызова `run()` дело доходит только после успешной проверки и получения снимка.

## Что Tool возвращает

| Поле `ToolResult` | Что это |
|---|---|
| `status` | Итог выполнения: `ok`, `error` или `cancelled` |
| `data` | При `ok` содержит результат расчёта. При `error` или `cancelled` равно `null` |
| `error` | При `error` или `cancelled` содержит `code` и `message`. При `ok` равно `null` |
| `meta` | Сведения о расчёте: название и версия Tool, источник данных, время обновления и другие поля операции |

| `ToolResult.status` | Что значит | В какое состояние Dispatcher переводит Job |
|---|---|---|
| `ok` | Результат готов | `done` |
| `error` | Расчёт завершился ошибкой | `failed` |
| `cancelled` | Tool обнаружил отмену через `context.is_cancelled()` | `cancelled` |

Tool возвращает ошибки расчёта в `ToolResult`. Если из `run()` всё же вышло необработанное исключение, Dispatcher фиксирует `INTERNAL_ERROR` и переводит Job в `failed`. Перед сохранением итога Dispatcher проверяет признак отмены.

### Ошибки, которые возвращает Tool

| Код | Когда возникает | `status` |
|---|---|---|
| `LEAGUE_NOT_SUPPORTED` | Матч в снимке не из АПЛ | `error` |
| `PREDICTION_NOT_AVAILABLE` | Матч уже начался, отменён или время начала неизвестно (только `predict_match`) | `error` |
| `NOT_ENOUGH_DATA` | Сыгранных матчей меньше минимума, нужного для расчёта | `error` |
| `CANCELLED` | Пользователь отменил задачу во время выполнения | `cancelled` |
| `INTERNAL_ERROR` | Непредвиденная ошибка в самом Tool | `error` |

## Ошибки получения снимка

Сервис футбольных данных готовит снимок до запуска Tool. Если требуется запрос к football-data.org, его выполняет адаптер. Адаптер обрабатывает HTTP-ответы и повторы, а сервис данных сообщает Dispatcher причину, по которой снимок получить не удалось. В перечисленных случаях Dispatcher обрабатывает ошибку до вызова `Tool.run()`.

| Код | Когда возникает | Действие Dispatcher |
|---|---|---|
| `MATCH_NOT_FOUND` | Матча с указанным `match_id` нет | Переводит Job в `failed` |
| `SOURCE_UNAVAILABLE` | Внешний API не ответил или вернул 5xx после всех повторных попыток | Переводит Job в `failed` |
| `SOURCE_BAD_DATA` | Внешний API вернул повреждённый или неполный ответ | Переводит Job в `failed` |
| `SOURCE_RATE_LIMITED` | Внешний API вернул 429. Вместе с кодом сервис передаёт `retry_after_seconds` — время ожидания в секундах | Переводит Job в `waiting_quota` |

Например, при `SOURCE_RATE_LIMITED` с `retry_after_seconds = 42` Dispatcher переводит Job в `waiting_quota`. На этой попытке вызова Tool и результата `ToolResult` нет.

## Интерфейс в коде

`MatchSnapshot` обозначает доменный тип снимка, который готовит сервис футбольных данных.

```python
from __future__ import annotations

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


@dataclass
class ToolResult:
    status: Literal["ok", "error", "cancelled"]
    data: dict[str, Any] | None
    error: ToolError | None
    meta: dict[str, Any]


class Tool(Protocol):
    name: str                      # Название в Registry, например "compare_teams"
    version: str                   # Версия метода расчёта
    params_model: type[BaseModel]  # Схема для проверки входного JSON

    def run(
        self,
        params: BaseModel,
        snapshot: MatchSnapshot,
        context: ToolContext,
    ) -> ToolResult: ...
```

## Tool 1. `compare_teams` — сравнение статистики команд

Считает для обеих команд выбранного матча число матчей, победы, ничьи, поражения, забитые и пропущенные мячи. В расчёт входят только матчи, сыгранные до начала выбранного матча.

| Параметр | Тип | Обязательное | Что это |
|---|---|---|---|
| `match_id` | Целое число | Да | Номер матча во внешнем API |

**Пример запроса**

```json
{
  "match_id": 538012
}
```

**Пример успешного результата** (числа условные)

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

`cutoff` — время начала выбранного матча. В примерах список `used_match_ids` сокращён. В настоящем ответе в него входят все использованные матчи.

**Пример ошибки**

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

Считает вероятности трёх исходов: победы хозяев, ничьей и победы гостей. В первой версии используется распределение Пуассона по среднему числу голов команд. ML-модель запланирована на следующий семестр и будет подключена как ещё один Tool по этому же контракту.

| Параметр | Тип | Обязательное | Что это |
|---|---|---|---|
| `match_id` | Целое число | Да | Номер будущего матча во внешнем API |

**Пример запроса**

```json
{
  "match_id": 538012
}
```

**Пример успешного результата** (числа условные)

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

Каждая вероятность лежит в диапазоне от 0 до 1. Сумма трёх вероятностей равна 1 с допустимым отклонением не более 0,000001.

**Пример ошибки**

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
