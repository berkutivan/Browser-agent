from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.config import settings


def _message_text_from_choice(first_choice: dict[str, Any]) -> str | None:
    """OpenRouter / some models put the assistant reply in `reasoning` when `content` is null."""
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content

    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    if isinstance(reasoning, list):
        parts = [str(x) for x in reasoning if x]
        if parts:
            return "\n".join(parts)

    details = message.get("reasoning_details")
    if isinstance(details, list):
        chunks: list[str] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning":
                t = item.get("text") or item.get("content") or item.get("reasoning")
                if isinstance(t, str) and t.strip():
                    chunks.append(t)
        if chunks:
            return "\n".join(chunks)

    return None


class OpenRouterClient:
    def __init__(self) -> None:
        if not settings.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required")

    async def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if settings.openrouter_http_referer:
            headers["HTTP-Referer"] = settings.openrouter_http_referer
        if settings.openrouter_title:
            headers["X-Title"] = settings.openrouter_title

        payload = {
            "model": settings.openrouter_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        retries = 2
        backoff = 1.2
        data: dict[str, Any] | None = None

        async with httpx.AsyncClient(timeout=90.0) as client:
            for attempt in range(retries + 1):
                try:
                    response = await client.post(
                        f"{settings.openrouter_base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    break
                except httpx.ConnectError as exc:
                    if attempt >= retries:
                        base = settings.openrouter_base_url
                        raise RuntimeError(
                            "Не удалось подключиться к OpenRouter. "
                            f"Проверьте интернет/прокси/SSL и доступность {base}. "
                            f"Исходная ошибка: {type(exc).__name__}: {exc!r}"
                        ) from exc
                except httpx.TimeoutException as exc:
                    if attempt >= retries:
                        raise RuntimeError(
                            "Таймаут запроса к OpenRouter. "
                            "Проверьте сеть или увеличьте timeout. "
                            f"Исходная ошибка: {type(exc).__name__}: {exc!r}"
                        ) from exc
                except httpx.HTTPStatusError as exc:
                    # Ошибки 4xx обычно не transient, повторять смысла нет.
                    if 400 <= exc.response.status_code < 500:
                        body = exc.response.text[:800]
                        raise RuntimeError(
                            f"OpenRouter вернул {exc.response.status_code}: {body}"
                        ) from exc
                    if attempt >= retries:
                        body = exc.response.text[:800]
                        raise RuntimeError(
                            f"OpenRouter вернул {exc.response.status_code} после повторов: {body}"
                        ) from exc
                if attempt < retries:
                    await asyncio.sleep(backoff * (attempt + 1))

        if data is None:
            raise RuntimeError("OpenRouter request failed without response payload")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            snippet = json.dumps(data, ensure_ascii=False)[:600]
            raise RuntimeError(f"OpenRouter response missing choices. Payload: {snippet}")

        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        if not isinstance(first_choice, dict):
            raise RuntimeError("OpenRouter choice is not an object")

        content: Any = _message_text_from_choice(first_choice)

        if content is None:
            content = first_choice.get("text")

        if content is None:
            snippet = json.dumps(first_choice, ensure_ascii=False)[:600]
            raise RuntimeError(f"OpenRouter response missing message content. Choice: {snippet}")

        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        return {"raw": content, "usage": data.get("usage", {})}
