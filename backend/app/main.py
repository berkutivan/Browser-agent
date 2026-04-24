from __future__ import annotations

import asyncio
import json
import sys
import traceback
import uuid
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.agent.react_agent import ReactAgent
from app.agent.run_disk_logger import AgentRunDiskLogger
from app.browser.mcp_server import BrowserMcpServer
from app.config import settings

# Playwright on Windows requires subprocess-capable event loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


class TaskRequest(BaseModel):
    task: str
    skip_guard_confirmations: bool = False


class ApprovalRequest(BaseModel):
    approved: bool


class Runtime:
    def __init__(self) -> None:
        self.browser = BrowserMcpServer()
        self.agent = ReactAgent(self.browser)
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self.waiters: dict[str, asyncio.Future[bool]] = {}
        self.run_tasks: dict[str, asyncio.Task[None]] = {}
        self.browser_watchdog_task: asyncio.Task[None] | None = None
        self.active_run_id: str | None = None
        self.start_lock = asyncio.Lock()

    async def emit(self, run_id: str, event: dict[str, Any]) -> None:
        self.events[run_id].append(event)
        dead: list[WebSocket] = []
        for ws in self.sockets[run_id]:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets[run_id].discard(ws)

    async def cancel_all_runs(self, reason: str) -> None:
        for run_id, task in list(self.run_tasks.items()):
            if task.done():
                continue
            await self.emit(
                run_id,
                {
                    "type": "system",
                    "text": f"Run cancelled: {reason}",
                    "run_id": run_id,
                },
            )
            task.cancel()
            waiter = self.waiters.pop(run_id, None)
            if waiter and not waiter.done():
                waiter.set_result(False)
            if self.active_run_id == run_id:
                self.active_run_id = None

    async def cancel_run(self, run_id: str, reason: str, *, purge: bool = False) -> None:
        task = self.run_tasks.get(run_id)
        if task and not task.done():
            await self.emit(
                run_id,
                {
                    "type": "system",
                    "text": f"Run cancelled: {reason}",
                    "run_id": run_id,
                },
            )
            task.cancel()
        waiter = self.waiters.pop(run_id, None)
        if waiter and not waiter.done():
            waiter.set_result(False)
        if self.active_run_id == run_id:
            self.active_run_id = None
        if purge:
            self.events.pop(run_id, None)
            self.sockets.pop(run_id, None)

    async def browser_watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.7)
            if self.browser.is_closed():
                await self.cancel_all_runs("browser was closed by user")
                return


runtime = Runtime()
app = FastAPI(title="AI Web Agent Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    await runtime.browser.startup()
    runtime.browser_watchdog_task = asyncio.create_task(runtime.browser_watchdog())


@app.on_event("shutdown")
async def shutdown() -> None:
    if runtime.browser_watchdog_task and not runtime.browser_watchdog_task.done():
        runtime.browser_watchdog_task.cancel()
    await runtime.browser.shutdown()


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/task/start")
async def start_task(request: TaskRequest) -> dict[str, str]:
    async with runtime.start_lock:
        previous_run_id = runtime.active_run_id
        if previous_run_id:
            await runtime.cancel_run(
                previous_run_id,
                "superseded by a new run request",
                purge=True,
            )

        run_id = str(uuid.uuid4())
        runtime.active_run_id = run_id

    effective_skip_guard = bool(request.skip_guard_confirmations and settings.allow_skip_guard_confirmations)

    async def wait_for_approval(payload: dict[str, Any]) -> None:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        runtime.waiters[run_id] = fut
        approved = await fut
        if not approved:
            raise RuntimeError(f"Action rejected: {payload.get('reason', 'guard rejected')}")

    disk_logger = AgentRunDiskLogger(run_id)
    meta_path = disk_logger.run_dir / "meta.json"

    def _write_run_meta() -> None:
        meta_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "task": request.task,
                    "skip_guard_confirmations": bool(request.skip_guard_confirmations),
                    "effective_skip_guard": effective_skip_guard,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    await asyncio.to_thread(_write_run_meta)

    async def emit_with_disk(event: dict[str, Any]) -> None:
        payload = {**event, "run_id": run_id}
        await disk_logger.log_event(payload)
        await runtime.emit(run_id, payload)

    async def runner() -> None:
        await emit_with_disk({"type": "system", "text": "Run started", "run_id": run_id})
        if request.skip_guard_confirmations and not settings.allow_skip_guard_confirmations:
            await emit_with_disk(
                {
                    "type": "system",
                    "text": "Запрос на отключение подтверждений отклонен политикой сервера",
                    "run_id": run_id,
                },
            )
        elif effective_skip_guard:
            await emit_with_disk(
                {
                    "type": "system",
                    "text": "Подтверждения опасных действий отключены для этого запуска",
                    "run_id": run_id,
                },
            )
        try:
            await emit_with_disk(
                {
                    "type": "system",
                    "text": f"Логи запуска на диске: {disk_logger.directory.as_posix()}",
                    "run_id": run_id,
                    "log_dir": disk_logger.directory.as_posix(),
                },
            )
            result = await runtime.agent.run(
                task=request.task,
                emit=emit_with_disk,
                wait_for_approval=wait_for_approval,
                skip_guard_confirmations=effective_skip_guard,
                disk_logger=disk_logger,
            )
            await emit_with_disk({"type": "result", "result": result})
        except Exception as exc:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            await emit_with_disk(
                {
                    "type": "error",
                    "text": str(exc) or "Ошибка без сообщения",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc) or "",
                    "error_repr": repr(exc),
                    "traceback": tb[-6000:],
                },
            )
        finally:
            runtime.run_tasks.pop(run_id, None)
            if runtime.active_run_id == run_id:
                runtime.active_run_id = None
            await emit_with_disk({"type": "system", "text": "Run finished", "run_id": run_id})

    runtime.run_tasks[run_id] = asyncio.create_task(runner())
    return {"run_id": run_id}


@app.post("/api/task/{run_id}/approve")
async def approve_action(run_id: str, request: ApprovalRequest) -> dict[str, bool]:
    fut = runtime.waiters.get(run_id)
    if not fut:
        raise HTTPException(status_code=404, detail="No approval required now")
    if not fut.done():
        fut.set_result(request.approved)
    runtime.waiters.pop(run_id, None)
    return {"ok": True}


@app.websocket("/ws/{run_id}")
async def ws_events(websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()
    runtime.sockets[run_id].add(websocket)
    for event in runtime.events.get(run_id, []):
        await websocket.send_json(event)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        runtime.sockets[run_id].discard(websocket)
