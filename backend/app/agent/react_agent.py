from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse
from typing import Any, Awaitable, Callable

from app.agent.run_disk_logger import AgentRunDiskLogger
from app.browser.mcp_server import BrowserMcpServer
from app.config import settings
from app.llm.openrouter import OpenRouterClient

EventCb = Callable[[dict[str, Any]], Awaitable[None]]
ApprovalCb = Callable[[dict[str, Any]], Awaitable[None]]


SYSTEM_PROMPT = """
You are a browser automation ReAct agent.
You must output strict JSON:
{
  "thought": "short reasoning",
  "action": {"tool": "navigate|click|type|extract_text|finish", "args": {}}
}
Rules:
- ReAct order is mandatory on every step:
  1) Observe current URL + AXTree + UiHints + page_context (this is your Observation).
  2) Think using user goal + current Observation.
  3) Act by choosing one tool call.
- Your thought must explicitly reference current page context (what element/state you observed) and why the chosen action is next.
- Use only provided tools.
- The task text is already passed by backend; never type/retype the task into any launcher/control UI textbox.
- Do not use screenshot: it is disabled. Rely on AXTree in each user turn; use extract_text with a selector when you need visible text from the page.
- Never request raw full HTML.
- Use UiHints to choose stable selectors first (data-testid, aria-label, href, role).
- Prefer selectors by text, role, aria-label, placeholder, data-testid.
- For checkbox by visible label, prefer selector formats like:
  - checkbox:has-text('Label text')
  - label:has-text('Label text')
- If a tool call fails, you must adjust the next action arguments (selector/url/text) using the error details. Do not repeat the same failing call unchanged.
- If click fails with "intercepts pointer events", switch strategy: try clicking checkbox in the same row/listitem or click a different stable target in that row.
- Never treat a tool call as successful without checking observable postconditions in the next step (state change, URL change, enabled/disabled changes).
- If observation indicates no_effect=true, do not repeat the same action args; switch selector and interaction strategy.
- If current URL is local host app/control plane, first navigate to the target external site from the user task instead of interacting with local launcher controls.
- If the user task is destructive (delete/order/pay/submit), ask guard confirmation by using tool finish with args {"status":"await_guard","reason":"..."}.
- When task is done, return tool finish with args {"status":"done","result":"..."}.

PAGE CONTEXT CHECK (page_context field in observation):
- ALWAYS check page_context before clicking:
  - If isLoading=true: WAIT! Page is still loading, do not click yet. Use extract_text or wait.
  - If readyState != "complete": Page not ready, wait or retry.
  - If errors array not empty: Page has error messages, consider them in planning.
  - If visibleModals > 0: Modal/popup is open, interact with it first or dismiss it.
  - If canScrollDown=true AND target element inViewport=false: Scroll down first using click on scroll area or extract_text.
  - If hasFocusedElement=true: Some input is focused, might need to blur it before clicking buttons.
- Page scroll position (scrollY): Use to understand if element might be below fold.

CLICK SUCCESS CHECKLIST:
Before clicking, verify in UiHints:
1. Element has isEnabled=true (not disabled)
2. Element has inViewport=true (visible on screen) OR canScrollDown=false
3. Element has hasClickHandler=true OR tag is button/a (confirm it's clickable)
4. If any check fails, scroll first or find alternative selector

LIST AND CARD INTERACTION STRATEGIES (for job sites, email, e-commerce):
- When interacting with lists/cards (like job vacancies on hh.ru), use this priority:
  1) First try clicking the MAIN LINK/TITLE of the item to open detail page, where buttons become more stable
  2) If inline button needed, use "row:has-text('Item text') button" pattern to find button within specific row
  3) If that fails, use "card:has-text('Item text')" to find the card, then click link inside it
- Prefer semantic selectors over text matching:
  - Use data-testid="..." if present in UiHints
  - Use [role='button'][aria-label='...'] for buttons
  - Use a[href*='...'] for links (partial href match is more stable)
- Check UiHints isEnabled and inViewport flags before clicking:
  - If isEnabled=false, element might need prior action (form fill, scroll, another click)
  - If inViewport=false, element may need scroll or might be in collapsed section
- For "Apply" buttons on job sites: if "Откликнуться" button times out, try:
  1) Click job title link first to open detail page, then apply
  2) Use "button:has-text('Откликнуться')" as broader match
  3) Look for alternative apply buttons like "Подробнее", "Вакансия", or icon buttons
- For navigation timeouts: if direct navigate to URL fails with ERR_ABORTED, retry with full URL or try going through intermediate page

WHY CLICKS FAIL (common reasons):
- Page still loading (isLoading=true) - wait for readyState=complete
- Element not in viewport - scroll first
- Element disabled - need prior action (form fill, login, etc.)
- Modal/popup blocking - dismiss or interact with modal first
- JavaScript not initialized - wait and retry
- Element is not actually clickable - try parent element or alternative selector
""".strip()


DESTRUCTIVE_HINTS = ("delete", "remove", "pay", "order", "submit", "checkout", "отправ", "удал", "заказ")
ACTION_RESTART_RETRIES = 2
STRATEGY_RETRIES = 3


def _destructive_guard_reason(task: str, tool: str, args: dict[str, Any]) -> str:
    hits = [h for h in DESTRUCTIVE_HINTS if h in task.lower()]
    hints = ", ".join(repr(h) for h in hits) if hits else "не указано явно"
    try:
        args_preview = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        args_preview = str(args)
    if len(args_preview) > 500:
        args_preview = args_preview[:500] + "…"
    return (
        f"В задаче есть ключевые слова, указывающие на необратимое действие: {hints}. "
        f"Планируется шаг: {tool} с параметрами: {args_preview}. "
        f"Подтвердите выполнение этого шага."
    )


class ReactAgent:
    def __init__(self, browser_server: BrowserMcpServer) -> None:
        self.browser_server = browser_server
        self.llm = OpenRouterClient()

    def _parse_json_payload(self, raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Some models still wrap JSON into markdown code fences.
        fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        if fenced_match:
            return json.loads(fenced_match.group(1))

        # Fallback: extract the first JSON object from text.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])

        raise ValueError("LLM returned non-JSON payload")

    @staticmethod
    def _sanitize_action_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        thought = payload.get("thought")
        if not isinstance(thought, str) or not thought.strip():
            thought = ""

        action = payload.get("action")
        if not isinstance(action, dict):
            # Если action отсутствует, но есть thought - используем finish с thought как результат
            if thought:
                return thought, {"tool": "finish", "args": {"status": "done", "result": thought}}
            return "No action provided", {"tool": "finish", "args": {"status": "done", "result": "No result"}}

        tool = action.get("tool")
        if not isinstance(tool, str):
            tool = "finish"
        tool = tool.strip().lower()
        if tool not in {"navigate", "click", "type", "extract_text", "finish"}:
            tool = "finish"

        args = action.get("args")
        if not isinstance(args, dict):
            args = {}

        # Если thought пустой, но есть action - генерируем thought на основе action
        if not thought:
            if tool == "navigate":
                url = args.get("url", "unknown")
                thought = f"Planning to navigate to {url}"
            elif tool == "click":
                selector = args.get("selector", "unknown element")
                thought = f"Planning to click on {selector}"
            elif tool == "type":
                selector = args.get("selector", "unknown input")
                text_preview = args.get("text", "")[:30]
                thought = f"Planning to type '{text_preview}...' into {selector}"
            elif tool == "extract_text":
                selector = args.get("selector", "body")
                thought = f"Planning to extract text from {selector}"
            elif tool == "finish":
                thought = "Task completed or requires user decision"
            else:
                thought = f"Planning to execute {tool}"

        return thought, {"tool": tool, "args": args}

    async def _complete_action_payload(
        self,
        messages: list[dict[str, str]],
        emit: EventCb,
        step: int,
        disk_logger: AgentRunDiskLogger | None = None,
    ) -> tuple[dict[str, Any], str]:
        parse_retries = 2
        completion = await self.llm.complete_json(messages)
        raw = completion["raw"]

        # Логируем сырой ответ от LLM для отладки
        await emit(
            {
                "type": "system",
                "step": step,
                "text": f"LLM raw response received ({len(raw)} chars)",
            }
        )

        for attempt in range(parse_retries + 1):
            try:
                payload = self._parse_json_payload(raw)
                thought = payload.get("thought", "")
                action = payload.get("action", {})
                tool = action.get("tool", "unknown") if isinstance(action, dict) else "unknown"
                await emit(
                    {
                        "type": "system",
                        "step": step,
                        "text": f"Parsed JSON successfully: thought='{thought[:60]}...' tool={tool}",
                    }
                )
                if disk_logger:
                    await disk_logger.log_llm_turn(step, raw, payload, None)
                return payload, raw
            except ValueError as exc:
                if attempt >= parse_retries:
                    if disk_logger:
                        await disk_logger.log_llm_turn(step, raw, None, str(exc))
                    raise
                await emit(
                    {
                        "type": "error",
                        "step": step,
                        "text": (
                            f"[json-repair {attempt + 1}/{parse_retries}] {exc}. "
                            "Requesting strict JSON reformat from model."
                        ),
                    }
                )
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was not valid JSON. "
                            "Return STRICT JSON only in this exact schema and nothing else:\n"
                            '{\n  "thought": "detailed reasoning about current state and next action",\n'
                            '  "action": {"tool": "navigate|click|type|extract_text|finish", "args": {}}\n}\n'
                            "Do not use markdown/code fences."
                        ),
                    }
                )
                completion = await self.llm.complete_json(messages)
                raw = completion["raw"]

    @staticmethod
    def _is_local_control_plane_url(current_url: str) -> bool:
        try:
            parsed = urlparse(current_url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _action_targets_control_plane(tool: str, args: dict[str, Any]) -> bool:
        if tool not in {"click", "type"}:
            return False
        selector = str(args.get("selector", "")).lower()
        text = str(args.get("text", "")).lower()
        joined = f"{selector} {text}"
        control_plane_tokens = (
            "опишите задачу",
            "выполняется",
            "запустить",
            "run id",
            "не спрашивать подтверждения",
            "approve",
            "разрешить",
            "отклонить",
            "task",
        )
        return any(token in joined for token in control_plane_tokens)

    async def _execute_action(
        self,
        task: str,
        step: int,
        current_url: str,
        tool: str,
        args: dict[str, Any],
        emit: EventCb,
        wait_for_approval: ApprovalCb,
        guard_already_confirmed: bool,
        skip_guard_confirmations: bool,
        disk_logger: AgentRunDiskLogger | None = None,
    ) -> dict[str, Any]:
        if self._is_local_control_plane_url(current_url) and self._action_targets_control_plane(tool, args):
            raise RuntimeError(
                "Control-plane interaction is blocked: task is already provided by backend API. "
                "Navigate to the target website and continue there."
            )

        if tool == "finish":
            status = args.get("status", "done")
            if status == "await_guard":
                if disk_logger:
                    await disk_logger.log_mcp_tool(
                        step,
                        "finish",
                        args,
                        {"phase": "await_guard", "skipped": bool(skip_guard_confirmations)},
                        None,
                    )
                if skip_guard_confirmations:
                    return {"kind": "approval"}
                if guard_already_confirmed:
                    raise RuntimeError("Agent requested guard confirmation again after approval")
                reason = args.get("reason", "Sensitive action")
                await emit(
                    {
                        "type": "guard_request",
                        "step": step,
                        "reason": reason,
                        "tool": "finish",
                        "args": args,
                    }
                )
                await wait_for_approval({"reason": reason})
                return {"kind": "approval"}
            result = {"status": "done", "result": args.get("result", "Completed")}
            if disk_logger:
                await disk_logger.log_mcp_tool(step, "finish", args, {"phase": "done", **result}, None)
            await emit({"type": "observation", "step": step, "text": result["result"]})
            return {"kind": "finished", "result": result}

        try:
            observation = await self.browser_server.call_tool(tool, args)
        except Exception as exc:
            if disk_logger:
                await disk_logger.log_mcp_tool(step, tool, args, None, str(exc))
            raise
        if disk_logger:
            obs_out: dict[str, Any] = observation if isinstance(observation, dict) else {"value": observation}
            await disk_logger.log_mcp_tool(step, tool, args, obs_out, None)
        if isinstance(observation, dict):
            effect = observation.get("effect")
            if isinstance(effect, dict) and effect.get("no_effect") is True:
                raise RuntimeError(
                    f"Tool call produced no observable effect and must be replanned: "
                    f"tool={tool}, args={json.dumps(args, ensure_ascii=False)}, effect={json.dumps(effect, ensure_ascii=False)}"
                )
        await emit({"type": "observation", "step": step, "text": json.dumps(observation, ensure_ascii=False)[:1200]})
        return {"kind": "observation", "observation": observation}

    @staticmethod
    def _action_fingerprint(tool: str, args: dict[str, Any]) -> str:
        """Создаёт fingerprint действия для отслеживания повторов."""
        try:
            selector = str(args.get("selector", ""))
            text = str(args.get("text", ""))
            url = str(args.get("url", ""))
            return f"{tool}:{selector}:{text}:{url}"
        except Exception:
            return f"{tool}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"

    async def run(
        self,
        task: str,
        emit: EventCb,
        wait_for_approval: ApprovalCb,
        *,
        skip_guard_confirmations: bool = False,
        disk_logger: AgentRunDiskLogger | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        guard_confirmed = False
        last_call_error: str | None = None

        # Отслеживание fingerprint для предотвращения циклов
        fingerprint_counts: dict[str, int] = {}
        fingerprint_history: list[str] = []

        for step in range(1, settings.agent_max_steps + 1):
            snapshot = await self.browser_server.context_snapshot()
            if disk_logger:
                await disk_logger.log_context_snapshot(step, snapshot)
            extra_error_context = (
                f"\nPrevious tool error: {last_call_error}\n"
                "You must fix this by changing selector/url/text in the next action."
                if last_call_error
                else ""
            )
            # Формируем контекст страницы для LLM
            page_ctx = snapshot.get("page_context", {})
            page_ctx_str = json.dumps(page_ctx, ensure_ascii=False) if page_ctx else "{}"

            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Task: {task}\nStep: {step}\n"
                        f"URL: {snapshot['url']}\nTitle: {snapshot['title']}\n"
                        f"AXTree: {snapshot['ax_tree']}\n"
                        f"UiHints: {snapshot.get('ui_hints', '[]')}\n"
                        f"PageContext: {page_ctx_str}"
                        f"{extra_error_context}"
                    ),
                }
            )
            last_call_error = None
            payload, current_raw = await self._complete_action_payload(
                messages, emit, step, disk_logger=disk_logger
            )
            current_thought, action = self._sanitize_action_payload(payload)
            current_tool = action.get("tool", "finish")
            current_args = action.get("args", {})

            for strategy_attempt in range(STRATEGY_RETRIES + 1):
                await emit({"type": "thought", "step": step, "text": current_thought})
                await emit({"type": "action", "step": step, "tool": current_tool, "args": current_args})

                # Отслеживание fingerprint для предотвращения циклов
                fingerprint = self._action_fingerprint(current_tool, current_args)
                fingerprint_counts[fingerprint] = fingerprint_counts.get(fingerprint, 0) + 1
                fingerprint_history.append(fingerprint)

                # Если один и тот же селектор уже не сработал 2+ раза, требуем смену стратегии
                if fingerprint_counts.get(fingerprint, 0) >= 2:
                    await emit(
                        {
                            "type": "error",
                            "step": step,
                            "text": f"WARNING: Same action '{fingerprint[:80]}...' already tried {fingerprint_counts[fingerprint]} times. You MUST use a different selector or strategy.",
                        }
                    )

                requires_destructive_guard = (
                    not skip_guard_confirmations
                    and any(h in task.lower() for h in DESTRUCTIVE_HINTS)
                    and current_tool in {"click", "type"}
                )
                if requires_destructive_guard:
                    reason = _destructive_guard_reason(task, current_tool, current_args)
                    await emit(
                        {
                            "type": "guard_request",
                            "step": step,
                            "reason": reason,
                            "tool": current_tool,
                            "args": current_args,
                        }
                    )
                    await wait_for_approval({"reason": reason})
                    guard_confirmed = True

                action_error: Exception | None = None
                for restart_attempt in range(ACTION_RESTART_RETRIES + 1):
                    try:
                        outcome = await self._execute_action(
                            task=task,
                            step=step,
                            current_url=snapshot["url"],
                            tool=current_tool,
                            args=current_args,
                            emit=emit,
                            wait_for_approval=wait_for_approval,
                            guard_already_confirmed=guard_confirmed,
                            skip_guard_confirmations=skip_guard_confirmations,
                            disk_logger=disk_logger,
                        )
                        if outcome["kind"] == "approval":
                            guard_confirmed = True
                            messages.append({"role": "assistant", "content": current_raw})
                            if skip_guard_confirmations:
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": "User disabled confirmation prompts for this run. Continue execution.",
                                    }
                                )
                            else:
                                messages.append({"role": "user", "content": "User approved. Continue execution."})
                            break
                        if outcome["kind"] == "finished":
                            return outcome["result"]

                        observation = outcome["observation"]
                        messages.append({"role": "assistant", "content": current_raw})
                        messages.append(
                            {
                                "role": "user",
                                "content": f"Observation: {json.dumps(observation, ensure_ascii=False)}",
                            }
                        )
                        action_error = None
                        break
                    except Exception as exc:
                        action_error = exc
                        await emit(
                            {
                                "type": "error",
                                "step": step,
                                "text": f"[retry {restart_attempt + 1}/{ACTION_RESTART_RETRIES + 1}] {exc}",
                            }
                        )
                        if restart_attempt < ACTION_RESTART_RETRIES:
                            await asyncio.sleep(0.5 * (restart_attempt + 1))  # Увеличивающаяся пауза
                            continue

                if action_error is None:
                    break

                if strategy_attempt >= STRATEGY_RETRIES:
                    last_call_error = (
                        f"Action failed after {ACTION_RESTART_RETRIES + 1} restarts and "
                        f"{STRATEGY_RETRIES} strategy changes. "
                        f"tool={current_tool}, args={json.dumps(current_args, ensure_ascii=False)}, "
                        f"error={action_error}"
                    )
                    await emit({"type": "observation", "step": step, "text": last_call_error})
                    messages.append({"role": "assistant", "content": current_raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Tool call failed and must be corrected on the next step.\n"
                                f"{last_call_error}\n"
                                "Choose a different/fixed action with updated arguments."
                            ),
                        }
                    )
                    break

                recovery_snapshot = await self.browser_server.context_snapshot()
                messages.append({"role": "assistant", "content": current_raw})

                # Собираем информацию о неудачных попытках для контекста
                recent_failures = fingerprint_history[-5:] if len(fingerprint_history) >= 5 else fingerprint_history
                failed_patterns = "\n".join([f"  - {fp[:100]}" for fp in recent_failures])

                # Контекст страницы для recovery
                recovery_page_ctx = recovery_snapshot.get("page_context", {})
                recovery_page_ctx_str = json.dumps(recovery_page_ctx, ensure_ascii=False) if recovery_page_ctx else "{}"

                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Previous action failed with error:\n"
                            f"{action_error}\n\n"
                            "CRITICAL: You MUST choose a completely different strategy. Do NOT repeat selectors that already failed.\n"
                            f"Recent failed fingerprints:\n{failed_patterns}\n\n"
                            "Strategy options to consider:\n"
                            "  1) If button click in list failed, try clicking the item's title/link to open detail page first\n"
                            "  2) Use 'row:has-text(\"...\") button' pattern for scoped selection\n"
                            "  3) Look for data-testid attributes in UiHints\n"
                            "  4) Try partial href match: a[href*='partial-url']\n"
                            "  5) Check PageContext: if isLoading=true, WAIT. If visibleModals>0, handle modal first.\n"
                            "  6) Check isEnabled/inViewport flags in UiHints - disabled elements need prior actions\n"
                            "Choose a different strategy and return strict JSON only.\n"
                            f"Current URL: {recovery_snapshot['url']}\n"
                            f"Current Title: {recovery_snapshot['title']}\n"
                            f"AXTree: {recovery_snapshot['ax_tree']}\n"
                            f"UiHints: {recovery_snapshot.get('ui_hints', '[]')}\n"
                            f"PageContext: {recovery_page_ctx_str}"
                        ),
                    }
                )
                recovery_payload, current_raw = await self._complete_action_payload(
                    messages, emit, step, disk_logger=disk_logger
                )
                current_thought, recovery_action = self._sanitize_action_payload(recovery_payload)
                if not current_thought or current_thought == "No thought":
                    current_thought = f"Strategy change #{strategy_attempt + 1} after action failure."
                current_tool = recovery_action.get("tool", "finish")
                current_args = recovery_action.get("args", {})

            if last_call_error:
                continue

        return {"status": "max_steps", "result": "Stopped by AGENT_MAX_STEPS"}
