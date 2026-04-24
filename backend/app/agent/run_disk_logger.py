from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


def _iso_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


class AgentRunDiskLogger:
    """
    Пишет в папку анализа запуска: стрим событий, снимки страницы на шаг, вызовы инструментов (MCP), ответы LLM.
    """

    def __init__(self, run_id: str, base_dir: Path | None = None) -> None:
        self.run_id = run_id
        root = base_dir if base_dir is not None else ROOT_DIR / "agent_logs"
        self.run_dir = (root / run_id).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.run_dir / "events.jsonl"
        self._mcp_path = self.run_dir / "mcp.jsonl"
        self._steps_path = self.run_dir / "steps.jsonl"
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        return self.run_dir

    async def _write_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        record = {**record, "ts": _iso_ts(), "run_id": self.run_id}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        async with self._lock:
            await asyncio.to_thread(_append_line, path, line)

    async def log_event(self, event: dict[str, Any]) -> None:
        await self._write_jsonl(self._events_path, {"kind": "stream_event", "event": event})

    async def log_context_snapshot(self, step: int, snapshot: dict[str, Any]) -> None:
        await self._write_jsonl(
            self._mcp_path,
            {"kind": "context_snapshot", "step": step, "input": {}, "output": snapshot},
        )
        await self._write_jsonl(
            self._steps_path,
            {"kind": "step_context", "step": step, "snapshot": snapshot},
        )

    async def log_mcp_tool(
        self,
        step: int,
        tool: str,
        args: dict[str, Any],
        output: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        rec: dict[str, Any] = {
            "kind": "tool_call",
            "step": step,
            "tool": tool,
            "input": args,
        }
        if error is not None:
            rec["error"] = error
        else:
            rec["output"] = output
        await self._write_jsonl(self._mcp_path, rec)
        await self._write_jsonl(
            self._steps_path,
            {
                "kind": "step_tool",
                "step": step,
                "tool": tool,
                "args": args,
                "result": output,
                "error": error,
            },
        )

    async def log_llm_turn(self, step: int, raw: str, parsed: dict[str, Any] | None, parse_error: str | None) -> None:
        await self._write_jsonl(
            self._steps_path,
            {
                "kind": "llm_turn",
                "step": step,
                "raw": raw,
                "parsed": parsed,
                "parse_error": parse_error,
            },
        )
