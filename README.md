# AI Web Agent (MCP)

Проект реализует автономного браузерного агента с архитектурой MCP и циклом ReAct (`thought -> action -> observation`).

## Что внутри

- `backend` - FastAPI-сервис (Python 3.11+), оркестратор агента, интеграция с OpenRouter и browser tools.
- `frontend` - Vue 3 + Vite интерфейс для запуска задач, просмотра realtime-логов и подтверждения risky-действий.
- `browser-use-mcp-server` - референсный пример MCP-сервера (оставлен для сравнения/исследования).
- `Show_examples` - примеры выполненных сценариев и логов.

## Требования

- Python `3.11+` (рекомендуется `3.11` или `3.12`)
- [uv](https://docs.astral.sh/uv/)
- Node.js `18+`
- npm `9+`

## Быстрый старт

### 1) Настройка переменных окружения

Скопируйте шаблон:

```bash
cp .env.example .env
```

Для Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Минимально обязательное значение: `OPENROUTER_API_KEY`.

### 2) Запуск backend

```bash
cd backend
uv sync
uv run --active playwright install chromium
uv run --active uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Проверка доступности:

- `GET http://127.0.0.1:8001/api/health`

### 3) Запуск frontend

```bash
cd frontend
npm install
npm run dev
```

По умолчанию frontend работает с backend по адресу `http://127.0.0.1:8001`.
При необходимости переопределите `VITE_BACKEND_HTTP_URL` и `VITE_BACKEND_WS_URL` в `.env`.

## Основные API backend

- `POST /api/task/start` - запуск нового прогона агента.
- `POST /api/task/{run_id}/approve` - подтверждение guarded-действий.
- `GET /api/health` - healthcheck.
- `WS /ws/{run_id}` - стрим событий выполнения.

## Как устроены guardrails

Guardrails реализованы как серверный слой подтверждений для потенциально опасных действий агента.

1. Во время выполнения backend анализирует шаги агента и, при необходимости, поднимает событие `guard_request` в `WS /ws/{run_id}`.
2. Frontend показывает пользователю причину (`reason`), инструмент (`tool`) и аргументы действия (`args`).
3. До получения решения выполнение ставится на паузу.
4. UI отправляет решение в `POST /api/task/{run_id}/approve` с `approved: true/false`.
5. При `approved: true` агент продолжает выполнение; при `approved: false` запуск завершается ошибкой `Action rejected`.

Когда guard может сработать:

- Для потенциально деструктивных сценариев (например, submit/delete/pay/order) при действиях `click`/`type`.
- Когда модель явно запрашивает подтверждение через `finish` со статусом `await_guard`.

Отключение подтверждений:

- В запросе `POST /api/task/start` можно передать `skip_guard_confirmations`.
- Флаг применяется только если на сервере включено `ALLOW_SKIP_GUARD_CONFIRMATIONS=true`.
- Если серверная политика не разрешает отключение, backend игнорирует запрос и пишет системное событие в лог/стрим.

## Реализованные требования

- ReAct loop с явными событиями в стриме.
- MCP tools: `navigate`, `click`, `type`, `screenshot`, `extract_text`.
- Работа только через OpenRouter (`OPENROUTER_*`).
- Headful browser + persistent profile (`launch_persistent_context`).
- Экономия токенов: в модель передается accessibility snapshot (AX tree), а не сырой HTML.
- Security Guard Layer: UI-подтверждение перед потенциально деструктивными действиями.

## Примеры

Готовые примеры выполнения находятся в папке `Show_examples`.
