"""共享的 Playwright 浏览器启动器。"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

from .accounts import acquire_browser_slot, release_browser_slot
from .config import account_state_path, get_valid_state_path

logger = logging.getLogger("douyin-cloud-streak")

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_COMMON_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]

_POOL_LOCK = threading.Lock()
_BROWSER_POOL: dict[str, dict] = {}
_PW = None
_PW_LOCK = threading.Lock()
POOL_IDLE_TIMEOUT = 120.0


def _ensure_playwright():
    global _PW
    if _PW is not None:
        return _PW
    with _PW_LOCK:
        if _PW is None:
            _PW = sync_playwright().start()
    return _PW


def _reclaim(entry: dict) -> None:
    browser = entry.get("browser")
    if browser:
        try:
            browser.close()
        except Exception:
            pass


def _reap_idle(now: float) -> None:
    for key in list(_BROWSER_POOL.keys()):
        e = _BROWSER_POOL.get(key)
        if not e:
            continue
        if e.get("refs", 0) == 0 and now - e.get("last_used", 0) > POOL_IDLE_TIMEOUT:
            _BROWSER_POOL.pop(key, None)
            _reclaim(e)


def _new_pool_entry() -> dict:
    pw = _ensure_playwright()
    browser = pw.chromium.launch(headless=True, args=_COMMON_ARGS)
    return {
        "browser": browser,
        "refs": 0,
        "last_used": time.time(),
    }


def shutdown_pool() -> None:
    with _POOL_LOCK:
        entries = list(_BROWSER_POOL.values())
        _BROWSER_POOL.clear()
    for e in entries:
        _reclaim(e)
    global _PW
    pw = _PW
    _PW = None
    if pw:
        try:
            pw.stop()
        except Exception:
            pass


def _apply_stealth(page) -> None:
    try:
        from playwright_stealth import Stealth
        Stealth().apply_stealth_sync(page)
    except Exception:
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except Exception:
            pass


@contextmanager
def open_browser(state_path: Path | str | None = None, headless: bool = True, **ctx_kwargs):
    valid_state = Path(state_path) if state_path else get_valid_state_path()
    state_file = str(valid_state) if valid_state and valid_state.exists() else None
    key = state_file or "__no_state__"

    acquire_browser_slot()
    entry = None
    try:
        with _POOL_LOCK:
            _reap_idle(time.time())
            entry = _BROWSER_POOL.get(key)
            if entry and entry.get("browser") and entry["browser"].is_connected():
                entry["refs"] += 1
            else:
                if entry:
                    _BROWSER_POOL.pop(key, None)
                    _reclaim(entry)
                entry = _new_pool_entry()
                entry["refs"] = 1
                _BROWSER_POOL[key] = entry
        browser = entry["browser"]
        p = _ensure_playwright()

        defaults = {
            "viewport": {"width": 1366, "height": 768},
            "user_agent": _CHROME_UA,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
        }
        defaults.update(ctx_kwargs)

        context = browser.new_context(**defaults)
        if state_file:
            try:
                import json
                _state = json.loads(Path(state_file).read_text(encoding="utf-8"))
                _cookies = _state.get("cookies") or []
                if _cookies:
                    context.add_cookies(_cookies)
            except Exception:
                pass
        page = context.new_page()
        _apply_stealth(page)

        yield p, browser, context, page
    finally:
        with _POOL_LOCK:
            if entry and entry.get("refs", 0) > 0:
                entry["refs"] -= 1
                entry["last_used"] = time.time()
        release_browser_slot()
