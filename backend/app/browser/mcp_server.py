from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from app.config import ROOT_DIR, settings

# Короткие клики по типовым кнопкам cookie / модалок (RU + EN), не блокируем поток надолго
_OVERLAY_BUTTON_NAMES = (
    "Принять все",
    "Принять",
    "Согласен",
    "Согласна",
    "Понятно",
    "Закрыть",
    "OK",
    "Не сейчас",
    "Позже",
    "Отмена",
    "Allow",
    "Accept",
    "Accept all",
    "Got it",
    "Close",
    "Dismiss",
)


class BrowserMcpServer:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._launcher_page: Page | None = None
        self._agent_pages: list[Page] = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
        self._profile_dir = (ROOT_DIR / settings.browser_profile_dir).resolve()
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._closed_event = Event()

    @staticmethod
    def _startup_url_candidates(startup_url: str) -> list[str]:
        """VPN может ломать localhost-резолв/маршрутизацию, поэтому пробуем loopback-варианты."""
        normalized = startup_url.strip()
        if not normalized:
            return []
        out = [normalized]
        if "localhost" in normalized:
            out.append(normalized.replace("localhost", "127.0.0.1"))
        elif "127.0.0.1" in normalized:
            out.append(normalized.replace("127.0.0.1", "localhost"))
        # Сохраняем порядок и убираем дубликаты.
        return list(dict.fromkeys(out))

    async def _run_on_browser_thread(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    async def startup(self) -> None:
        def _startup_sync() -> tuple[Playwright, BrowserContext, Page]:
            self._closed_event.clear()
            pw = sync_playwright().start()
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()

            def on_new_page(p: Page) -> None:
                self._setup_page_handlers(p)

            def on_context_close() -> None:
                self._closed_event.set()

            context.on("page", on_new_page)
            context.on("close", lambda: on_context_close())
            self._setup_page_handlers(page)

            startup_url = (settings.browser_startup_url or "").strip()
            last_error: Exception | None = None
            for candidate in self._startup_url_candidates(startup_url):
                try:
                    page.goto(candidate, wait_until="domcontentloaded", timeout=8000)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    continue
            if startup_url and last_error is not None:
                # Не валим весь backend при проблемах локального frontend под VPN.
                # Агент сможет продолжить и сделать navigate позже.
                print(
                    "WARN: startup URL is unavailable, backend will continue without initial navigation: "
                    f"{startup_url}. Last error: {last_error}"
                )
            self._prepare_page_for_action(page)
            # Вкладка лаунчера (UI задач): не закрывать и не перезаписывать при внешней навигации агента.
            self._launcher_page = page
            return pw, context, page

        self._playwright, self._context, self._page = await self._run_on_browser_thread(_startup_sync)

    async def shutdown(self) -> None:
        def _shutdown_sync() -> None:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()

        try:
            await self._run_on_browser_thread(_shutdown_sync)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not initialized")

        # Если пользователь закрыл браузер целиком, агент должен остановиться
        # и не перезапускать вкладку автоматически.
        if self._closed_event.is_set():
            raise RuntimeError("Browser was closed by user; all active runs must be cancelled.")

        # Если закрыта только текущая вкладка, переключаемся на любую живую вкладку.
        if self._page.is_closed():
            if not self._context:
                raise RuntimeError("Browser context not initialized")
            live_pages = [p for p in self._context.pages if not p.is_closed()]
            if live_pages:
                self._page = live_pages[-1]
            else:
                raise RuntimeError("No open browser pages left; browser session is closed.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise RuntimeError("Browser context not initialized")
        return self._context

    def _setup_page_handlers(self, page: Page) -> None:
        """Диалоги window.alert/confirm не должны блокировать сценарий."""

        def on_dialog(dialog) -> None:
            try:
                dialog.dismiss()
            except Exception:
                pass

        try:
            page.on("dialog", on_dialog)
        except Exception:
            pass

    def _try_dismiss_blocking_overlays(self, page: Page) -> None:
        """Снять типовые всплывающие окна (cookie, уведомления), не ломая основной сценарий."""
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        for name in _OVERLAY_BUTTON_NAMES:
            try:
                loc = page.get_by_role("button", name=name, exact=True).first
                if loc.count() == 0:
                    continue
                loc.click(timeout=1200)
                return
            except Exception:
                continue

    def _prepare_page_for_action(self, page: Page) -> None:
        self._try_dismiss_blocking_overlays(page)

    @staticmethod
    def _selector_implies_selection(selector: str) -> bool:
        lowered = selector.lower()
        return any(token in lowered for token in ("checkbox", "radio", "switch", "[type='checkbox']"))

    @staticmethod
    def _safe_is_checked(locator: Any) -> bool | None:
        try:
            return bool(locator.is_checked(timeout=500))
        except Exception:
            return None

    @staticmethod
    def _safe_get_attribute(locator: Any, attr: str) -> str | None:
        try:
            return locator.get_attribute(attr, timeout=500)
        except Exception:
            return None

    @staticmethod
    def _safe_input_value(locator: Any) -> str | None:
        try:
            return locator.input_value(timeout=500)
        except Exception:
            return None

    def _resolve_locator(self, selector: str):
        selector = selector.strip()

        # Pure text selector from model output -> fallback to text-based locator.
        # Example: "Не спрашивать подтверждения ..."
        if selector and not any(ch in selector for ch in ("[", "]", "(", ")", ":", ">", ".", "#", "=")):
            return self.page.get_by_text(selector, exact=False).first

        # Accept a simple role-like shorthand produced by the agent:
        # textbox[placeholder='...'] -> get_by_placeholder("...")
        placeholder_match = re.fullmatch(r"""textbox\[placeholder=['"](.+?)['"]\]""", selector)
        if placeholder_match:
            return self.page.get_by_placeholder(placeholder_match.group(1)).first

        # Агент часто пишет селектор "textbox" — в HTML такого тега нет, это роль ARIA
        if selector.strip().lower() == "textbox":
            return self.page.get_by_role("textbox").first

        # Агент иногда пишет "checkbox" как роль, а не CSS-селектор.
        if selector.lower() == "checkbox":
            return self.page.get_by_role("checkbox").first

        # checkbox:has-text('...') -> role-based checkbox by accessible name.
        checkbox_has_text = re.fullmatch(r"""checkbox:has-text\(['"](.+?)['"]\)""", selector, flags=re.IGNORECASE)
        if checkbox_has_text:
            label = checkbox_has_text.group(1)
            exact = self.page.get_by_role("checkbox", name=label).first
            if exact.count() > 0:
                return exact
            return self.page.get_by_role("checkbox", name=re.compile(re.escape(label), re.IGNORECASE)).first

        # label:has-text('...') -> click label tied to input controls.
        label_has_text = re.fullmatch(r"""label:has-text\(['"](.+?)['"]\)""", selector, flags=re.IGNORECASE)
        if label_has_text:
            label = label_has_text.group(1)
            return self.page.locator("label", has_text=label).first

        # navigation[aria-label='...'] — в HTML это <nav>, роль navigation
        nav_aria = re.fullmatch(r"""navigation\[aria-label=['"](.+?)['"]\]""", selector)
        if nav_aria:
            return self.page.get_by_role("navigation", name=nav_aria.group(1)).first

        # listitem:nth-of-type(N) checkbox -> чекбокс внутри N-го listitem
        listitem_checkbox_nth = re.fullmatch(
            r"""listitem:nth-of-type\((\d+)\)\s+checkbox""", selector
        )
        if listitem_checkbox_nth:
            idx = max(int(listitem_checkbox_nth.group(1)) - 1, 0)
            return self.page.get_by_role("listitem").nth(idx).get_by_role("checkbox").first

        # listitem:nth-child(N) checkbox -> чекбокс внутри N-го listitem
        listitem_checkbox_nth_child = re.fullmatch(
            r"""listitem:nth-child\((\d+)\)\s+checkbox""", selector
        )
        if listitem_checkbox_nth_child:
            idx = max(int(listitem_checkbox_nth_child.group(1)) - 1, 0)
            return self.page.get_by_role("listitem").nth(idx).get_by_role("checkbox").first

        # listitem:nth-last-child(N) checkbox -> чекбокс внутри N-го listitem с конца
        listitem_checkbox_nth_last = re.fullmatch(
            r"""listitem:nth-last-child\((\d+)\)\s+checkbox""", selector
        )
        if listitem_checkbox_nth_last:
            idx_from_end = max(int(listitem_checkbox_nth_last.group(1)) - 1, 0)
            return self.page.get_by_role("listitem").nth(-1 - idx_from_end).get_by_role("checkbox").first

        # listitem:last-child checkbox -> чекбокс внутри последнего listitem
        if selector.lower() == "listitem:last-child checkbox":
            return self.page.get_by_role("listitem").last.get_by_role("checkbox").first

        # listitem:first-of-type checkbox / listitem:first-child checkbox
        if selector.lower() in {"listitem:first-of-type checkbox", "listitem:first-child checkbox"}:
            return self.page.get_by_role("listitem").first.get_by_role("checkbox").first

        # list listitem:first-child checkbox
        if selector.lower() == "list listitem:first-child checkbox":
            return (
                self.page.get_by_role("list")
                .first.get_by_role("listitem")
                .first.get_by_role("checkbox")
                .first
            )

        # list listitem:first-child a[href*='...'] -> ссылка внутри первого item списка
        first_link_in_first_item = re.fullmatch(
            r"""list\s+listitem:first-child\s+a\[href\*=['"](.+?)['"]\]""", selector
        )
        if first_link_in_first_item:
            href_part = first_link_in_first_item.group(1)
            return (
                self.page.get_by_role("list")
                .first.get_by_role("listitem")
                .first.locator(f"a[href*='{href_part}']")
                .first
            )

        # listitem checkbox -> первый чекбокс внутри первого listitem
        if selector.lower() == "listitem checkbox":
            return self.page.get_by_role("listitem").first.get_by_role("checkbox").first

        # link[text='...'] -> get_by_role("link", name="...")
        link_text_match = re.fullmatch(r"""link\[text=['"](.+?)['"]\]""", selector)
        if link_text_match:
            return self.page.get_by_role("link", name=link_text_match.group(1)).first

        # link:has-text('...') — агент часто пишет "link", в DOM это обычно <a>
        link_has_text = re.fullmatch(r"""link:has-text\(['"](.+?)['"]\)""", selector)
        if link_has_text:
            label = link_has_text.group(1)
            return self.page.get_by_role("link", name=re.compile(re.escape(label), re.IGNORECASE)).first

        # link:has-url('/path') -> anchor containing href fragment
        link_has_url = re.fullmatch(r"""link:has-url\(['"](.+?)['"]\)""", selector, flags=re.IGNORECASE)
        if link_has_url:
            href_part = link_has_url.group(1).replace("'", "\\'")
            return self.page.locator(f"a[href*='{href_part}']").first

        # button[text='...'] -> get_by_role("button", name="...")
        button_text_match = re.fullmatch(r"""button\[text=['"](.+?)['"]\]""", selector)
        if button_text_match:
            target = button_text_match.group(1)
            exact = self.page.get_by_role("button", name=target).first
            if exact.count() > 0:
                return exact
            words = [w for w in re.split(r"\s+", target.strip()) if w]
            if words:
                relaxed_pattern = ".*".join(re.escape(w) for w in words[:2])
                return self.page.get_by_role("button", name=re.compile(relaxed_pattern, re.IGNORECASE)).first
            return self.page.get_by_role("button").first

        # row:has-text('...') button -> кнопка внутри строки/карточки с текстом
        row_contains_button = re.fullmatch(
            r"""row:has-text\(['"](.+?)['"]\)\s+(button|link|a)""", selector, flags=re.IGNORECASE
        )
        if row_contains_button:
            row_text = row_contains_button.group(1)
            element_type = row_contains_button.group(2).lower()
            # Ищем строку по тексту, затем внутри неё элемент
            row_locator = self.page.locator("*", has_text=re.compile(re.escape(row_text), re.IGNORECASE))
            if element_type in ("link", "a"):
                return row_locator.locator("a, [role='link']").first
            return row_locator.locator("button, [role='button']").first

        # card:has-text('...') -> карточка/контейнер с текстом
        card_contains = re.fullmatch(r"""card:has-text\(['"](.+?)['"]\)""", selector, flags=re.IGNORECASE)
        if card_contains:
            card_text = card_contains.group(1)
            return self.page.locator("*", has_text=re.compile(re.escape(card_text), re.IGNORECASE)).first

        # element:has-text('...') -> любой элемент с текстом
        element_contains = re.fullmatch(r"""element:has-text\(['"](.+?)['"]\)""", selector, flags=re.IGNORECASE)
        if element_contains:
            el_text = element_contains.group(1)
            return self.page.get_by_text(el_text).first

        # button:has-text('...') -> кнопка с текстом (частый случай на hh.ru и др.)
        button_has_text = re.fullmatch(r"""button:has-text\(['"](.+?)['"]\)""", selector, flags=re.IGNORECASE)
        if button_has_text:
            btn_text = button_has_text.group(1)
            exact = self.page.get_by_role("button", name=btn_text).first
            if exact.count() > 0:
                return exact
            words = [w for w in re.split(r"\s+", btn_text.strip()) if w]
            if words:
                relaxed_pattern = ".*".join(re.escape(w) for w in words[:2])
                return self.page.get_by_role("button", name=re.compile(relaxed_pattern, re.IGNORECASE)).first
            return self.page.get_by_role("button").first

        return self.page.locator(selector).first

    @staticmethod
    def _url_has_http_origin(url: str) -> bool:
        try:
            parsed = urlparse(url.strip())
        except Exception:
            return False
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    @staticmethod
    def _same_scheme_netloc(a: str, b: str) -> bool:
        try:
            pa, pb = urlparse(a.strip()), urlparse(b.strip())
        except Exception:
            return False
        if not pa.netloc or not pb.netloc:
            return False
        return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)

    @staticmethod
    def _is_loopback_http(url: str) -> bool:
        try:
            pu = urlparse(url.strip())
        except Exception:
            return False
        if pu.scheme not in ("http", "https") or not pu.hostname:
            return False
        h = pu.hostname.lower()
        return h in {"localhost", "127.0.0.1", "::1"}

    def _should_open_new_tab_for_navigate(self, current: Page, target_url: str) -> bool:
        """Не переносим вкладку лаунчера на внешний сайт: открываем новую вкладку для работы агента."""
        lp = self._launcher_page
        if lp is None or lp.is_closed() or current is not lp:
            return False
        start = (settings.browser_startup_url or "").strip()
        tgt = (target_url or "").strip()
        if not tgt or tgt.lower().startswith("about:"):
            return False
        if self._same_scheme_netloc(start, tgt):
            return False
        try:
            ps = urlparse(start)
        except Exception:
            ps = urlparse("")
        if not ps.netloc and self._is_loopback_http(tgt):
            return False
        return self._url_has_http_origin(tgt)

    async def navigate(self, url: str) -> dict[str, Any]:
        def _navigate_sync() -> dict[str, Any]:
            page = self.page
            self._setup_page_handlers(page)

            if self._should_open_new_tab_for_navigate(page, url):
                if not self._context:
                    raise RuntimeError("Browser context not initialized")
                page = self._context.new_page()
                self._setup_page_handlers(page)
                self._agent_pages.append(page)
                self._page = page

            page.goto(url, wait_until="domcontentloaded")
            self._prepare_page_for_action(page)
            return {"url": page.url, "title": page.title()}

        return await self._run_on_browser_thread(_navigate_sync)

    async def click(self, selector: str, max_retries: int = 3) -> dict[str, Any]:
        def _click_sync() -> dict[str, Any]:
            self._prepare_page_for_action(self.page)
            loc = self._resolve_locator(selector)
            before_url = self.page.url
            before_checked = self._safe_is_checked(loc)
            before_aria_checked = self._safe_get_attribute(loc, "aria-checked")
            before_aria_pressed = self._safe_get_attribute(loc, "aria-pressed")

            last_error: Exception | None = None
            for attempt in range(max_retries):
                try:
                    # Сначала проверяем enabled
                    try:
                        enabled = loc.is_enabled(timeout=1200)
                    except Exception:
                        enabled = True
                    if not enabled:
                        raise RuntimeError(
                            f"Target element is disabled for click: {selector}. "
                            "Вероятно, нужно сначала выбрать элемент (например, чекбокс письма), "
                            "после чего действие станет доступным."
                        )

                    # Скроллим к элементу если нужно
                    try:
                        loc.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass  # Не критично, пробуем кликнуть и так

                    # Ждём стабильного состояния
                    try:
                        loc.wait_for(state="visible", timeout=3000)
                    except Exception:
                        pass

                    # Основной клик
                    loc.click(timeout=10000)
                    break  # Успех - выходим из retry loop

                except Exception as exc:
                    last_error = exc
                    message = str(exc).lower()

                    # Стратегия fallback для pointer interception
                    if "intercepts pointer events" in message or "subtree intercepts pointer events" in message:
                        try:
                            loc.click(timeout=5000, force=True)
                            break
                        except Exception:
                            try:
                                loc.dispatch_event("click")
                                break
                            except Exception:
                                pass  # Продолжим retry loop

                    # Для timeout пробуем force click на последней попытке
                    if "timeout" in message and attempt == max_retries - 1:
                        try:
                            loc.click(timeout=5000, force=True)
                            break
                        except Exception:
                            pass

                    if attempt < max_retries - 1:
                        import time
                        time.sleep(0.5 * (attempt + 1))  # Увеличивающаяся пауза
                        continue
                    else:
                        raise last_error

            after_checked = self._safe_is_checked(loc)
            after_aria_checked = self._safe_get_attribute(loc, "aria-checked")
            after_aria_pressed = self._safe_get_attribute(loc, "aria-pressed")
            after_url = self.page.url

            state_changed = any(
                (
                    before_checked is not None and after_checked is not None and before_checked != after_checked,
                    before_aria_checked is not None
                    and after_aria_checked is not None
                    and before_aria_checked != after_aria_checked,
                    before_aria_pressed is not None
                    and after_aria_pressed is not None
                    and before_aria_pressed != after_aria_pressed,
                )
            )
            url_changed = before_url != after_url
            no_effect = not state_changed and not url_changed

            # Selection-like clicks must visibly change state; otherwise the agent loops.
            if self._selector_implies_selection(selector) and no_effect:
                raise RuntimeError(
                    f"Click produced no observable selection effect for selector: {selector}. "
                    "Try another strategy/selector instead of repeating the same click."
                )

            return {
                "url": self.page.url,
                "clicked": selector,
                "effect": {
                    "state_changed": state_changed,
                    "url_changed": url_changed,
                    "no_effect": no_effect,
                    "before": {
                        "checked": before_checked,
                        "aria_checked": before_aria_checked,
                        "aria_pressed": before_aria_pressed,
                    },
                    "after": {
                        "checked": after_checked,
                        "aria_checked": after_aria_checked,
                        "aria_pressed": after_aria_pressed,
                    },
                },
            }

        return await self._run_on_browser_thread(_click_sync)

    async def type(self, selector: str, text: str, press_enter: bool = False) -> dict[str, Any]:
        def _type_sync() -> dict[str, Any]:
            self._prepare_page_for_action(self.page)
            loc = self._resolve_locator(selector)
            before_value = self._safe_input_value(loc)
            loc.click(timeout=10000)
            loc.fill(text)
            if press_enter:
                loc.press("Enter")
            after_value = self._safe_input_value(loc)
            value_changed = before_value is not None and after_value is not None and before_value != after_value
            if text and not value_changed and not press_enter:
                raise RuntimeError(
                    f"Type produced no observable input value change for selector: {selector}. "
                    "Try another input selector or focus strategy."
                )
            return {
                "typed": selector,
                "text_len": len(text),
                "effect": {
                    "value_changed": value_changed,
                    "no_effect": bool(text) and not value_changed and not press_enter,
                    "before_value_len": len(before_value) if before_value is not None else None,
                    "after_value_len": len(after_value) if after_value is not None else None,
                },
            }

        return await self._run_on_browser_thread(_type_sync)

    async def extract_text(self, selector: str = "body") -> dict[str, Any]:
        def _extract_text_sync() -> dict[str, Any]:
            self._prepare_page_for_action(self.page)
            text = self._resolve_locator(selector).inner_text(timeout=15000)
            return {"text": text[:3000], "selector": selector}

        return await self._run_on_browser_thread(_extract_text_sync)

    async def context_snapshot(self) -> dict[str, Any]:
        def _snapshot_sync() -> dict[str, Any]:
            self._prepare_page_for_action(self.page)

            # Собираем расширенную информацию о странице через JavaScript
            page_info = self.page.evaluate(
                """
                () => {
                  const short = (v, n = 200) => (v || "").trim().replace(/\\s+/g, " ").slice(0, n);

                  // Проверяем наличие ошибок на странице
                  const errors = [];
                  document.querySelectorAll('.error, .alert, .notification, [role="alert"]').forEach(el => {
                    if (el.textContent) errors.push(short(el.textContent, 150));
                  });

                  // Проверяем loading состояние
                  const isLoading = document.readyState !== 'complete' ||
                    document.querySelector('.loading, .spinner, [class*="loading"], [class*="spinner"]') !== null;

                  // Считаем количество форм и их состояние
                  const forms = Array.from(document.querySelectorAll('form')).map(f => ({
                    id: f.id || '',
                    action: f.action || '',
                    method: f.method || '',
                    fieldCount: f.querySelectorAll('input, select, textarea').length
                  }));

                  // Считаем модальные окна и оверлеи
                  const modals = Array.from(document.querySelectorAll(
                    '[role="dialog"], [role="modal"], .modal, .popup, .overlay, [class*="modal"], [class*="popup"]'
                  )).filter(el => {
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden';
                  }).length;

                  // Информация о фокусе
                  const activeElement = document.activeElement;
                  const focusedElement = activeElement && activeElement.tagName !== 'BODY'
                    ? {
                        tag: activeElement.tagName.toLowerCase(),
                        id: activeElement.id || '',
                        class: (activeElement.className || '').toString().slice(0, 50),
                        type: activeElement.type || '',
                        placeholder: activeElement.placeholder || ''
                      }
                    : null;

                  // Прокрутка страницы
                  const scrollInfo = {
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                    scrollHeight: document.documentElement.scrollHeight,
                    clientHeight: document.documentElement.clientHeight,
                    canScrollDown: (window.scrollY + document.documentElement.clientHeight) < document.documentElement.scrollHeight
                  };

                  return {
                    errors: errors.slice(0, 5),
                    isLoading,
                    formCount: forms.length,
                    forms: forms.slice(0, 3),
                    visibleModals: modals,
                    focusedElement,
                    scrollInfo,
                    url: window.location.href,
                    readyState: document.readyState
                  };
                }
                """
            )

            try:
                # Playwright modern API: ARIA snapshot is exposed via Locator.
                text = self.page.locator("body").aria_snapshot(timeout=15000)
            except Exception:
                # Fallback keeps agent running even if ARIA snapshot is unavailable.
                text = self.page.locator("body").first.inner_text(timeout=15000)

            ui_hints = self.page.evaluate(
                """
                () => {
                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const isInViewport = (el) => {
                    const rect = el.getBoundingClientRect();
                    const margin = 100; // небольшой отступ
                    return rect.top >= -margin && rect.left >= -margin &&
                           rect.bottom <= ((window.innerHeight || document.documentElement.clientHeight) + margin) &&
                           rect.right <= ((window.innerWidth || document.documentElement.clientWidth) + margin);
                  };
                  const isEnabled = (el) => {
                    if (el.disabled) return false;
                    if (el.getAttribute("aria-disabled") === "true") return false;
                    if (el.classList.contains("disabled")) return false;
                    if (el.getAttribute("readonly")) return false;
                    return true;
                  };
                  const short = (v, n = 120) => (v || "").trim().replace(/\\s+/g, " ").slice(0, n);

                  // Расширенный набор селекторов для поиска интерактивных элементов
                  const candidates = Array.from(document.querySelectorAll(
                    "a[href], button, input:not([type='hidden']), textarea, select, [role='button'], [role='link'], " +
                    "[role='checkbox'], [role='menuitem'], [role='tab'], [role='option'], [data-testid], " +
                    "[data-action], [data-click-target], .button, .btn, [class*='button'], [class*='btn'], " +
                    "[onclick], [data-href], [data-url]"
                  ));

                  const out = [];
                  for (const el of candidates) {
                    if (!isVisible(el)) continue;
                    const tag = (el.tagName || "").toLowerCase();
                    const role = el.getAttribute("role") || "";
                    const text = short(el.innerText || el.textContent || "");
                    const ariaLabel = short(el.getAttribute("aria-label") || "");
                    const testId = short(el.getAttribute("data-testid") || "");
                    const href = short(el.getAttribute("href") || el.getAttribute("data-href") || "", 180);
                    const type = short(el.getAttribute("type") || "");
                    const title = short(el.getAttribute("title") || "");
                    const cls = short(el.className && typeof el.className === "string" ? el.className : "", 80);
                    const id = short(el.id || "", 60);
                    const dataAction = short(el.getAttribute("data-action") || el.getAttribute("onclick") || "");
                    const placeholder = short(el.getAttribute("placeholder") || "");
                    const dataUrl = short(el.getAttribute("data-url") || "");

                    // Проверяем, есть ли у элемента обработчики событий
                    const hasClickHandler = el.onclick || el.getAttribute('data-click') || el.getAttribute('data-action');

                    out.push({
                      tag, role, text, ariaLabel, testId, href, type, title, className: cls, id,
                      dataAction, placeholder, dataUrl,
                      isEnabled: isEnabled(el),
                      inViewport: isInViewport(el),
                      hasClickHandler: !!hasClickHandler,
                      rect: {
                        top: Math.round(el.getBoundingClientRect().top),
                        left: Math.round(el.getBoundingClientRect().left),
                        width: Math.round(el.getBoundingClientRect().width),
                        height: Math.round(el.getBoundingClientRect().height)
                      }
                    });
                    if (out.length >= 80) break;
                  }
                  return out;
                }
                """
            )

            hints_text = json.dumps(ui_hints, ensure_ascii=False)
            if len(text) > settings.agent_max_context_chars:
                text_truncated = text[: settings.agent_max_context_chars] + "...[truncated]"
            else:
                text_truncated = text
            max_hints_chars = max(1200, settings.agent_max_context_chars // 2)
            if len(hints_text) > max_hints_chars:
                hints_text = hints_text[:max_hints_chars] + "...[truncated]"

            # Формируем компактный отчёт о состоянии страницы
            page_context = {
                "isLoading": page_info.get("isLoading", False),
                "readyState": page_info.get("readyState", "unknown"),
                "errors": page_info.get("errors", []),
                "formCount": page_info.get("formCount", 0),
                "visibleModals": page_info.get("visibleModals", 0),
                "hasFocusedElement": page_info.get("focusedElement") is not None,
                "canScrollDown": page_info.get("scrollInfo", {}).get("canScrollDown", False),
                "scrollY": page_info.get("scrollInfo", {}).get("scrollY", 0),
            }

            return {
                "url": self.page.url,
                "title": self.page.title(),
                "ax_tree": text_truncated,
                "ui_hints": hints_text,
                "page_context": page_context,
            }

        return await self._run_on_browser_thread(_snapshot_sync)

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "navigate":
            return await self.navigate(args["url"])
        if name == "click":
            return await self.click(args["selector"])
        if name == "type":
            return await self.type(
                args["selector"], args["text"], bool(args.get("press_enter", False))
            )
        if name == "screenshot":
            return {
                "skipped": True,
                "reason": "screenshot is disabled; use AXTree from context or extract_text",
            }
        if name == "extract_text":
            return await self.extract_text(args.get("selector", "body"))
        raise ValueError(f"Unknown tool: {name}")

    def is_closed(self) -> bool:
        return self._closed_event.is_set()
