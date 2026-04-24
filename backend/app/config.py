from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel


ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


class Settings(BaseModel):
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    )
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    openrouter_http_referer: str = os.getenv("OPENROUTER_HTTP_REFERER", "")
    openrouter_title: str = os.getenv("OPENROUTER_TITLE", "AI Web Agent")
    agent_max_steps: int = int(os.getenv("AGENT_MAX_STEPS", "10"))
    agent_max_context_chars: int = int(os.getenv("AGENT_MAX_CONTEXT_CHARS", "12000"))
    browser_profile_dir: str = os.getenv("BROWSER_PROFILE_DIR", "./browser_profile")
    browser_startup_url: str = os.getenv("BROWSER_STARTUP_URL", "about:blank")
    allow_skip_guard_confirmations: bool = (
        os.getenv("ALLOW_SKIP_GUARD_CONFIRMATIONS", "false").strip().lower() in {"1", "true", "yes", "on"}
    )
    cors_origins: list[str] = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ]
    host: str = os.getenv("BACKEND_HOST", "127.0.0.1")
    port: int = int(os.getenv("BACKEND_PORT", "8001"))


settings = Settings()
