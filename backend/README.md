# Backend

## Run

```bash
cd backend
uv sync
uv run --active playwright install chromium
uv run --active uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Backend exposes:

- `POST /api/task/start`
- `POST /api/task/{run_id}/approve`
- `GET /api/health`
- `WS /ws/{run_id}`
