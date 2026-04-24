# AI Web Agent (MCP)

Проект реализует тестовое задание: автономный браузерный агент с архитектурой MCP и паттерном ReAct.

## Переменные окружения

Скопируйте шаблон и заполните значения:

```bash
cp .env.example .env
```

Минимально обязательная переменная: `OPENROUTER_API_KEY`.

## Структура

- `backend` - Python 3.11+, FastAPI, ReAct orchestrator, MCP server tools, OpenRouter integration.
- `frontend` - Vue 3 host-интерфейс для запуска задач, realtime-логов и подтверждений безопасности.
- `browser-use-mcp-server` - стартовый пример (оставлен как reference).

## Соответствие требованиям

- ReAct loop с явными событиями `thought`, `action`, `observation` в стриме.
- MCP tools: `navigate`, `click`, `type`, `screenshot`, `extract_text`.
- OpenRouter-only (`OPENROUTER_*` переменные, запросы к `/chat/completions` OpenRouter).
- Headful browser + persistent profile (`launch_persistent_context`).
- Token management: в модель отправляется сжатый `accessibility snapshot` (AX tree), а не сырой HTML.
- Security Guard Layer: подтверждение из UI перед потенциально деструктивными действиями.

## Запуск backend

```bash
cd backend
uv sync
uv run --active playwright install chromium
uv run --active uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

## Запуск frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend по умолчанию ожидает backend на `http://127.0.0.1:8001`.
При необходимости переопределите через `VITE_BACKEND_HTTP_URL` и `VITE_BACKEND_WS_URL` в `.env`.

## Пример сложной задачи

Используйте сценарий, аналогичный:

- "Найди 3 подходящие вакансии AI-инженера на hh.ru и откликнись на них с сопроводительным".

Для финальной демонстрации остановите агента перед необратимым действием и подтвердите его через Security Guard в UI.
