"""网页端扫码登录会话管理。"""

from __future__ import annotations

import base64
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from .accounts import acquire_browser_slot, release_browser_slot
from .config import DEFAULT_ACCOUNT_ID, ROOT_STATE_PATH, account_state_path

logger = logging.getLogger("douyin-cloud-streak")

CHAT_URL = "https://www.douyin.com/chat?isPopup=1"

SESSION_TIMEOUT = 300
QR_REFRESH_LIMIT = 5

_LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt"}

_slot_guard = threading.Lock()
_slot_holders: set[str] = set()


def _acquire_slot_tracked(aid: str) -> None:
    acquire_browser_slot()
    with _slot_guard:
        _slot_holders.add(aid)


def _release_slot_once(aid: str) -> None:
    with _slot_guard:
        if aid not in _slot_holders:
            return
        _slot_holders.discard(aid)
    release_browser_slot()


def _hard_expire(aid: str) -> None:
    flag = _stop_flags.get(aid)
    if flag:
        flag.set()
    with _guard:
        st = _sessions.get(aid)
        if st and st["status"] in ("queuing", "starting", "waiting_scan"):
            st.update(status="expired", message="扫码会话超时，请重新发起扫码", qrcode="")
    _release_slot_once(aid)

_QR_SELECTORS = [
    "#animate_qrcode_container img",
    '[data-e2e="login-qrcode"] img',
    'div[class*="qrcode"] img',
]

_QR_EXPIRED_TEXTS = ["二维码已过期", "已失效", "已过期", "点击刷新", "刷新"]

_guard = threading.Lock()
_sessions: dict[str, dict] = {}
_stop_flags: dict[str, threading.Event] = {}


def _new_state(aid: str, **fields) -> dict:
    st = {
        "status": "starting",
        "message": "正在启动扫码环境…",
        "qrcode": "",
        "deep_link": "",
        "started_at": time.time(),
        "last_active": time.time(),
        "error": "",
    }
    st.update(fields)
    return st


def start(account_id: str) -> dict:
    with _guard:
        old = _sessions.get(account_id)
        if old and old["status"] in ("queuing", "starting", "waiting_scan"):
            return {"ok": True, "resumed": True, **_public(old)}
        flag = threading.Event()
        _stop_flags[account_id] = flag
        st = _new_state(
            account_id,
            status="queuing",
            message="正在排队获取浏览器名额…",
        )
        _sessions[account_id] = st

    t = threading.Thread(target=_session_worker, args=(account_id, flag), daemon=True)
    t.start()
    watchdog = threading.Timer(SESSION_TIMEOUT + 90, lambda: _hard_expire(account_id))
    watchdog.daemon = True
    watchdog.start()
    logger.info("[%s] 网页扫码会话已启动", account_id)
    return {"ok": True, "resumed": False, **_public(st)}


def status(account_id: str) -> dict:
    with _guard:
        st = _sessions.get(account_id)
        if not st:
            return {"status": "idle", "message": "", "qrcode": "", "deep_link": ""}
        if st["status"] == "waiting_scan":
            st["last_active"] = time.time()
        return _public(st)


def cancel(account_id: str) -> dict:
    with _guard:
        st = _sessions.get(account_id)
        if not st or st["status"] in ("success", "failed", "expired", "cancelled"):
            _sessions.pop(account_id, None)
            return {"ok": True, "message": "无进行中的扫码会话"}
        flag = _stop_flags.get(account_id)
    if flag:
        flag.set()
    for _ in range(30):
        time.sleep(0.1)
        with _guard:
            cur = _sessions.get(account_id)
            if not cur or cur["status"] not in ("queuing", "starting", "waiting_scan"):
                break
    else:
        with _guard:
            cur = _sessions.get(account_id)
            if cur and cur["status"] in ("waiting_scan",):
                cur["status"] = "cancelled"
                cur["message"] = "已取消"
    return {"ok": True, "message": "已取消"}


def _public(st: dict) -> dict:
    return {
        "status": st["status"],
        "message": st["message"],
        "qrcode": st["qrcode"] if st["status"] == "waiting_scan" else "",
        "deep_link": st.get("deep_link", "") if st["status"] == "waiting_scan" else "",
        "error": st["error"],
    }


def _qr_looks_valid(img) -> bool:
    try:
        g = img.convert("L")
        hist = g.histogram()
        total = sum(hist)
        if not total:
            return False
        dark = sum(hist[:64]) / total
        light = sum(hist[192:]) / total
        return dark > 0.04 and light > 0.04
    except Exception:
        return False


def _enhance_qr_image(data_url: str) -> tuple[str, str, bool]:
    deep_link = ""
    if not data_url or not data_url.startswith("data:image"):
        return data_url, deep_link, False
    try:
        from io import BytesIO
        from PIL import Image

        raw_b64 = data_url.split("base64,")[1] if "base64," in data_url else data_url
        img_data = base64.b64decode(raw_b64)
        original_rgba = Image.open(BytesIO(img_data)).convert("RGBA")

        bg = Image.new("RGBA", original_rgba.size, (255, 255, 255, 255))
        composited = Image.alpha_composite(bg, original_rgba).convert("RGB")

        valid = _qr_looks_valid(composited)

        pad = 32
        padded_img = Image.new(
            "RGB", (composited.size[0] + pad * 2, composited.size[1] + pad * 2), "WHITE"
        )
        padded_img.paste(composited, (pad, pad))

        try:
            import zxingcpp
            results = zxingcpp.read_barcodes(padded_img)
            if results:
                qr_text = results[0].text
                if any(d in qr_text for d in ("douyin.com", "snssdk.com", "iesdouyin.com")):
                    deep_link = qr_text
        except Exception:
            pass

        buf = BytesIO()
        padded_img.save(buf, format="PNG")
        enhanced_b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{enhanced_b64}", deep_link, valid
    except Exception:
        return data_url, deep_link, True


def _extract_valid_qrcode(page, timeout_ms: int = 45000) -> tuple[str, str]:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        qr = _wait_and_extract_qrcode(page, timeout_ms=12000)
        if qr:
            enhanced, deep_link, valid = _enhance_qr_image(qr)
            if valid:
                return enhanced, deep_link
        try:
            _click_qr_refresh(page)
        except Exception:
            pass
        page.wait_for_timeout(2500)
    return "", ""


def _set(aid: str, **fields) -> None:
    with _guard:
        st = _sessions.get(aid)
        if st is None:
            return
        st.update(fields)
        st["last_active"] = time.time()


def _is_stopped(aid: str) -> bool:
    flag = _stop_flags.get(aid)
    return bool(flag and flag.is_set())


def _launch_browser(pw):
    common = dict(
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-software-rasterizer",
            "--renderer-process-limit=2",
            "--no-zygote",
            "--mute-audio",
        ],
        ignore_default_args=["--enable-automation"],
    )
    return pw.chromium.launch(headless=True, **common), None


class CancelledError(Exception):
    pass


def _session_worker(aid: str, stop_flag: threading.Event) -> None:
    pw = None
    browser = None
    xvfb_proc = None
    try:
        _acquire_slot_tracked(aid)
        if _is_stopped(aid):
            raise CancelledError()

        _set(aid, status="starting", message="正在打开抖音登录页…")
        pw = sync_playwright().start()
        browser, xvfb_proc = _launch_browser(pw)
        chrome_major = (browser.version or "").split(".")[0] or "124"
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
        )
        context.add_init_script(
            "const _spoof=(proto)=>{const g=proto.getParameter;"
            "proto.getParameter=function(p){if(p===37445)return 'Intel Inc.';"
            "if(p===37446)return 'Intel Iris OpenGL Engine';return g.apply(this,[p]);};};"
            "if(window.WebGLRenderingContext)_spoof(WebGLRenderingContext.prototype);"
            "if(window.WebGL2RenderingContext)_spoof(WebGL2RenderingContext.prototype);"
        )
        page = context.new_page()
        try:
            from .browser import _apply_stealth
            _apply_stealth(page)
        except Exception:
            pass

        for _goto_attempt in range(2):
            try:
                page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                break
            except Exception:
                if _goto_attempt == 0:
                    page.wait_for_timeout(1500)
                    continue

        try:
            page.wait_for_selector("#animate_qrcode_container", timeout=12000)
        except Exception:
            pass

        try:
            page.get_by_text("扫码登录").first.click(timeout=500)
        except Exception:
            pass
        try:
            page.wait_for_selector("#animate_qrcode_container", timeout=5000)
        except Exception:
            pass
        try:
            page.locator("#animate_qrcode_container").first.click(timeout=1500)
        except Exception:
            pass

        enhanced_qr, deep_link = _extract_valid_qrcode(page)
        if not enhanced_qr:
            if _slider_captcha_present(page):
                raise RuntimeError("抖音触发滑动验证码（风控拦截），无法获取登录二维码。请稍后重新发起扫码。")
            raise RuntimeError("未能从页面提取到有效的登录二维码，请稍后重试")
        _set(aid, status="waiting_scan", message="请使用抖音 App 扫码登录", qrcode=enhanced_qr, deep_link=deep_link)

        deadline = time.time() + SESSION_TIMEOUT
        refresh_count = 0
        face_clicked = False
        slider_polls = 0
        polls = 0
        while time.time() < deadline:
            if _is_stopped(aid):
                raise CancelledError()

            cookies = context.cookies("https://www.douyin.com")
            if any(c.get("name") in _LOGIN_COOKIE_NAMES and c.get("value") for c in cookies):
                _save_state(context, aid)
                _set(aid, status="success",
                     message=f"登录成功！已保存该账号的登录态（{len(cookies)} 条 Cookie）")
                return

            polls += 1

            if _qr_expired(page):
                refresh_count += 1
                if refresh_count > QR_REFRESH_LIMIT:
                    raise RuntimeError("二维码刷新次数过多，请重新发起扫码")
                _click_qr_refresh(page)
                page.wait_for_timeout(2500)
                enhanced_qr, deep_link = _extract_valid_qrcode(page, timeout_ms=30000)
                if enhanced_qr:
                    _set(aid, qrcode=enhanced_qr, deep_link=deep_link,
                         message=f"二维码已自动刷新（第 {refresh_count} 次），请重新扫码")

            if _slider_captcha_present(page):
                slider_polls += 1
                if slider_polls >= 3:
                    _set(aid, status="failed",
                         message="抖音触发滑动验证码（风控拦截），网页端无法代你完成人工滑动。建议稍等几分钟重新发起扫码。",
                         error="slider_captcha", qrcode="")
                    return
            else:
                slider_polls = 0

            if not face_clicked:
                if _js_click_first(page, ["手机刷脸验证", "刷脸验证"]):
                    face_clicked = True
                    _set(aid, message="触发安全验证：请用抖音 App 扫描下方新二维码并按提示完成验证")
                    page.wait_for_timeout(3000)
            else:
                _js_click_first(page, ["已完成", "验证成功"])
                qr_face = _extract_face_qr(page)
                if qr_face:
                    enhanced_face, deep_face, valid = _enhance_qr_image(qr_face)
                    if valid:
                        _set(aid, qrcode=enhanced_face, deep_link=deep_face)

            page.wait_for_timeout(1500)

        _set(aid, status="expired", message="扫码超时，请重新发起扫码", qrcode="")

    except CancelledError:
        _set(aid, status="cancelled", message="已取消", qrcode="")
    except Exception as e:
        msg = str(e)[:200]
        _set(aid, status="failed", message="扫码会话异常", error=msg, qrcode="")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
        if xvfb_proc:
            try:
                xvfb_proc.terminate()
            except Exception:
                pass
        _release_slot_once(aid)
        _stop_flags.pop(aid, None)
        threading.Timer(120, lambda: _sessions.pop(aid, None)).start()


def _js_click_first(page, texts: list[str]) -> bool:
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=False)
            if loc.count():
                loc.first.evaluate("el => el.click()")
                return True
        except Exception:
            continue
    return False


_FACE_QR_JS = """
() => {
    const pick = (el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width < 100 || rect.width > 350 || Math.abs(rect.width - rect.height) > 15) return null;
        const src = el.src || "";
        if (src.includes("base64,")) return src;
        try {
            const c = document.createElement("canvas");
            c.width = el.naturalWidth || rect.width;
            c.height = el.naturalHeight || rect.height;
            c.getContext("2d").drawImage(el, 0, 0, c.width, c.height);
            return c.toDataURL("image/png");
        } catch (e) { return null; }
    };
    const imgs = document.querySelectorAll("img");
    for (let i = imgs.length - 1; i >= 0; i--) {
        const r = pick(imgs[i]);
        if (r) return r;
    }
    return null;
}
"""


def _extract_face_qr(page) -> str | None:
    try:
        data = page.evaluate(_FACE_QR_JS)
        return data if data and data.startswith("data:image") else None
    except Exception:
        return None


def _wait_and_extract_qrcode(page, timeout_ms: int = 45000) -> str | None:
    deadline = time.time() + timeout_ms / 1000
    src = ""
    while time.time() < deadline:
        for sel in _QR_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count():
                    first = loc.first
                    if first.is_visible():
                        candidate = first.get_attribute("src") or ""
                        if len(candidate) > 50:
                            src = candidate
                            break
            except Exception:
                continue
        if src:
            break
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            for sel in _QR_SELECTORS:
                try:
                    loc = frame.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        candidate = loc.first.get_attribute("src") or ""
                        if len(candidate) > 50:
                            src = candidate
                            break
                except Exception:
                    continue
            if src:
                break
        if src:
            break
        page.wait_for_timeout(400)

    if src.startswith("data:image"):
        return src
    if src.startswith("http"):
        try:
            resp = requests.get(src, timeout=8)
            b64 = base64.b64encode(resp.content).decode()
            return f"data:image/png;base64,{b64}"
        except Exception:
            pass
    if src:
        return f"data:image/png;base64,{src}"
    for _attempt in range(2):
        try:
            shot = page.screenshot(timeout=8000)
            return "data:image/png;base64," + base64.b64encode(shot).decode()
        except Exception:
            try:
                page.wait_for_timeout(1500)
            except Exception:
                break
    return None


_SLIDER_TEXTS = ("拖动滑块", "拖动下方滑块", "向右拖动", "滑块填充拼图", "完成拼图")


def _slider_captcha_present(page) -> bool:
    try:
        for sel in (
            "#captcha_container",
            "#captcha-verify-image",
            "[class*='captcha_verify']",
            "iframe[src*='captcha']",
            "iframe[src*='secsdk']",
        ):
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return True
        for t in _SLIDER_TEXTS:
            loc = page.get_by_text(t, exact=False)
            if loc.count() and loc.first.is_visible():
                return True
    except Exception:
        pass
    return False


def _qr_expired(page) -> bool:
    for text in _QR_EXPIRED_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            if loc.count():
                for i in range(min(loc.count(), 3)):
                    if loc.nth(i).is_visible():
                        return True
        except Exception:
            continue
    return False


def _click_qr_refresh(page) -> None:
    candidates = [
        "#animate_qrcode_container",
        'div[class*="qrcode"]',
        'div[class*="refresh"]',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3000)
                return
        except Exception:
            continue
    try:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    except Exception:
        pass


def _save_state(context, account_id: str) -> None:
    state = context.storage_state()
    raw = _ensure_origins(state)
    path: Path = account_state_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    if account_id == DEFAULT_ACCOUNT_ID:
        try:
            ROOT_STATE_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _ensure_origins(state) -> dict:
    if isinstance(state, dict):
        state.setdefault("cookies", [])
        state.setdefault("origins", [])
        return state
    return {"cookies": [], "origins": []}
