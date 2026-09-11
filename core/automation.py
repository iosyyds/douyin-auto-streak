"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from .browser import open_browser
from . import ledger
from .config import DEFAULT_ACCOUNT_ID, account_state_path, load_config
from .guard import detect_rate_limit
from .msg_builder import build_message
from .runtime import load_runtime, update_runtime
from .sender import creator_channel

logger = logging.getLogger("douyin-cloud-streak")

CHAT_URL = "https://www.douyin.com/chat"
LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _norm_name(s) -> str:
    return str(s or "").replace("\u00a0", " ").replace("\u200b", "").strip()


_STREAK_KEY_HINTS = ("streak", "keep_fire", "keepfire", "spark", "fire", "interact", "continuous")
_streak_logged_keys: set[str] = set()


def _probe_streak(item) -> int:
    if not isinstance(item, dict):
        return 0
    for k, v in item.items():
        kl = str(k).lower()
        if not any(h in kl for h in _STREAK_KEY_HINTS):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and 0 <= int(v) <= 9999:
            if k not in _streak_logged_keys:
                _streak_logged_keys.add(k)
                logger.info("接口探测到火花字段 %s=%s", k, v)
            return int(v)
        if isinstance(v, str):
            m = re.search(r"\d{1,4}", v)
            if m and 0 <= int(m.group()) <= 9999:
                if k not in _streak_logged_keys:
                    _streak_logged_keys.add(k)
                return int(m.group())
    return 0


_STREAK_PAGE_JS = """
() => {
    const t = (document.body ? document.body.innerText || "" : "").replace(/\\s+/g, " ");
    const pats = [
        /已连续\\s*(\\d{1,4})\\s*天/,
        /连续\\s*(\\d{1,4})\\s*天/,
        /持续\\s*(\\d{1,4})\\s*天/,
    ];
    for (const p of pats) {
        const m = t.match(p);
        if (m) return m[1];
    }
    return "";
}
"""


def _probe_streak_from_page(page) -> int:
    try:
        res = page.evaluate(_STREAK_PAGE_JS)
        if res and re.fullmatch(r"\d{1,4}", str(res)):
            days = int(res)
            if 1 <= days <= 9999:
                return days
    except Exception:
        pass
    return 0


def _screenshot(page, account_id: str | None = None) -> None:
    try:
        path = account_state_path(account_id).with_name("last_error.png")
        page.screenshot(path=str(path), timeout=5000)
    except Exception:
        pass


def check_login(page) -> tuple[bool, str]:
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"
    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception:
        pass
    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception:
            continue
    try:
        cookies = page.context.cookies()
        if not any(str(c.get("name", "")).startswith("sessionid") for c in cookies):
            return False, "未检测到 sessionid Cookie"
    except Exception as e:
        return False, f"读取登录 Cookie 失败: {e}"
    return True, "ok"


_TITLE_SELECTOR = '.conversationConversationItemtitle, [class*="Itemtitle"] .conversationConversationItemtitle'


def _list_title_matches(page, name: str) -> int:
    target = _norm_name(name)
    count = 0
    try:
        titles = page.locator(_TITLE_SELECTOR)
        n = titles.count()
        for i in range(min(n, 200)):
            el = titles.nth(i)
            try:
                if _norm_name(el.inner_text()) != target:
                    continue
                box = el.bounding_box()
            except Exception:
                continue
            if box and box.get("x", 9999) < 400:
                count += 1
    except Exception:
        pass
    return count


def _find_contact(page, name: str):
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    return page.locator(".conversationConversationItemtitle").filter(has_text=name).first


def _verify_in_conversation(page, name: str) -> bool:
    target = _norm_name(name)
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    el = loc.nth(i)
                    box = el.bounding_box()
                    text = _norm_name(el.inner_text())
                except Exception:
                    continue
                if (
                    box and box.get("x", 0) > 300 and box.get("y", 0) < 100
                    and text == target
                ):
                    return True
        except Exception:
            continue
    return False


def _search_and_open(page, name: str) -> bool:
    try:
        cands = page.get_by_placeholder("搜索", exact=False)
        box = None
        for i in range(min(cands.count(), 5)):
            el = cands.nth(i)
            b = el.bounding_box()
            if b and b.get("x", 9999) < 400:
                box = el
                break
        if box is None:
            return False
        box.click()
        box.fill(name)
        time.sleep(4)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(4)
            return True
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            cands = page.get_by_text(name, exact=False)
            candidate = None
            for i in range(cands.count()):
                el = cands.nth(i)
                text = el.inner_text() or ""
                if "搜" not in text:
                    candidate = el
                    break
            if candidate is None:
                candidate = cands.first
        if candidate.count() == 0:
            return False
        candidate.click(force=True)
        time.sleep(3)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(3)
        return True
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


def _quick_logged_out(page) -> bool:
    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return True
    except Exception:
        pass
    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return True
        except Exception:
            continue
    return False


def _locate_contact(page, name: str) -> tuple[bool, str]:
    dup = _list_title_matches(page, name)
    if dup > 1:
        return False, f"聊天列表存在 {dup} 个同名会话「{name}」，为避免错发已跳过。"
    try:
        reset_js = _SCROLL_LIST_JS.replace("el.scrollTop = before + step;", "el.scrollTop = 0;")
        page.evaluate(reset_js, 0)
        time.sleep(0.5)
    except Exception:
        pass
    for attempt in range(80):
        if attempt % 5 == 0 and _quick_logged_out(page):
            return False, "登录态已过期，请重新扫码后再发送"
        try:
            target = _find_contact(page, name)
            if target.count():
                target.click(force=True, timeout=10000)
                time.sleep(random.uniform(2, 4))
                if _verify_in_conversation(page, name):
                    return True, "ok"
            else:
                moved = False
                try:
                    r = page.evaluate(_SCROLL_LIST_JS, 450) or {}
                    moved = bool(r.get("moved"))
                except Exception:
                    pass
                if not moved:
                    try:
                        page.mouse.move(200, 350)
                        page.mouse.wheel(0, 450)
                    except Exception:
                        pass
                time.sleep(0.5)
        except Exception as e:
            logger.info("点击联系人 %s 异常: %s", name, str(e)[:100])
        time.sleep(random.uniform(0.5, 1))
    if _search_and_open(page, name):
        time.sleep(random.uniform(1, 3))
        if _verify_in_conversation(page, name):
            return True, "ok"
    return False, "未能切换到该好友会话"


def _type_and_send(page, input_box, msg_text: str) -> bool:
    try:
        input_box.click()
        time.sleep(0.4)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.3)
        page.keyboard.type(msg_text, delay=100)
        time.sleep(0.8)
        cur = input_box.inner_text() or ""
        if msg_text not in cur:
            return False
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("输入/发送异常: %s", str(e)[:100])
        return False


def _wait_input_cleared(input_box, msg_text: str, wait: float = 8) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        try:
            cur = input_box.inner_text() or ""
            if msg_text not in cur:
                return True
        except Exception:
            pass
    return False


class MessageSendTracker:
    _SEND_PATH_RE = re.compile(r"(?:v1/message/send|imapi\d*/(?:message/send|send_message))/?$")

    def __init__(self, page):
        self.page = page
        self.last_receipt: dict | None = None
        self._armed_at = 0.0
        self._handler = self._on_response
        try:
            self.page.on("response", self._handler)
        except Exception:
            pass

    def _on_response(self, resp):
        try:
            path = urlparse(resp.url or "").path
            if not self._SEND_PATH_RE.search(path):
                return
            if resp.status != 200:
                return
            data = resp.json()
            if not isinstance(data, dict):
                return
            status_code = data.get("status_code")
            if status_code is None:
                return
            nested = data.get("data") or {}
            if not isinstance(nested, dict):
                nested = {}
            msg_id = nested.get("server_message_id") or nested.get("message_id")
            status_msg = str(data.get("status_msg") or data.get("message") or "ok")
            self.last_receipt = {
                "status_code": status_code,
                "server_message_id": str(msg_id) if msg_id else "",
                "status_msg": status_msg,
                "time": time.time(),
            }
        except Exception:
            pass

    def reset(self):
        self.last_receipt = None
        self._armed_at = time.time()

    def pop_recent(self, within_seconds: float = 8.0) -> dict | None:
        rec = self.last_receipt
        if (
            rec
            and rec["time"] >= self._armed_at
            and (time.time() - rec["time"]) <= within_seconds
        ):
            self.last_receipt = None
            return rec
        return None

    def close(self):
        try:
            self.page.remove_listener("response", self._handler)
        except Exception:
            pass


def _receipt_verdict(receipt: dict | None) -> tuple[bool, str] | None:
    if receipt is None:
        return None
    code = receipt.get("status_code", 0)
    if code == 0:
        msg_id = receipt.get("server_message_id")
        return True, f"ok (msg_id: {msg_id})" if msg_id else "ok"
    if code == 1:
        return False, "发送失败：对方不在单聊会话中或已被解散"
    if code == 3:
        return False, f"发送失败：文案触发抖音安全审核拦截"
    if code == 5:
        return False, "发送失败：已被对方拉黑或对方已注销"
    return False, f"发送失败：服务端返回错误 {code}"


def _count_msg_bubble(page, msg_text: str, input_top: float) -> int:
    n = 0
    try:
        loc = page.get_by_text(msg_text, exact=True)
        for i in range(min(loc.count(), 10)):
            try:
                box = loc.nth(i).bounding_box()
            except Exception:
                continue
            if box and box.get("x", 0) > 300 and 60 < box.get("y", 0) < input_top - 6:
                n += 1
    except Exception:
        pass
    return n


def _settle_send(input_box, tracker: MessageSendTracker | None, msg_text: str,
                 bubble_before: int = 0, wait: float = 8, grace: float = 3.0,
                 page=None) -> tuple[bool, str]:
    cleared = _wait_input_cleared(input_box, msg_text, wait=wait)
    deadline = time.time() + (grace if cleared else 0)
    while True:
        if tracker:
            v = _receipt_verdict(tracker.pop_recent(within_seconds=15))
            if v is not None:
                return v
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    if cleared:
        if page is None:
            return True, "ok(弱判定:无服务端回执)"
        input_top = 9999.0
        try:
            b = input_box.bounding_box()
            if b:
                input_top = float(b.get("y", 9999))
        except Exception:
            pass
        bubble_after = _count_msg_bubble(page, msg_text, input_top)
        if bubble_after > bubble_before:
            return True, "ok(无回执,已核实新气泡)"
        return False, "无服务端回执且会话中未核实到新消息，判定未发出"
    return False, "发送后输入框未清空，消息可能未发出"


def _send_message(page, msg_text: str, dry_run: bool, tracker: MessageSendTracker | None = None) -> tuple[bool, str]:
    if detect_rate_limit(page):
        return False, "发送前检测到验证提示"
    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=8000)
    except Exception:
        return False, "找不到聊天输入框"
    if dry_run:
        return True, "dry-run"
    last_why = "未知原因"
    for attempt in (1, 2):
        if tracker:
            tracker.reset()
        input_top = 9999.0
        try:
            b = input_box.bounding_box()
            if b:
                input_top = float(b.get("y", 9999))
        except Exception:
            pass
        bubble_before = _count_msg_bubble(page, msg_text, input_top)
        if not _type_and_send(page, input_box, msg_text):
            last_why = "文字未能输入到输入框"
            if attempt == 1:
                continue
            return False, last_why
        ok, why = _settle_send(input_box, tracker, msg_text, bubble_before=bubble_before, page=page)
        if ok:
            return True, why
        last_why = why
        if detect_rate_limit(page):
            return False, "重试时检测到验证提示"
        time.sleep(random.uniform(1.5, 3))
    return False, last_why


def send_to_contact(page, name: str, msg_text: str, dry_run: bool, tracker: MessageSendTracker | None = None) -> tuple[bool, str]:
    _dismiss_dialogs(page)
    ok, why = _locate_contact(page, name)
    if not ok:
        return False, why
    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"
    return _send_message(page, msg_text, dry_run, tracker=tracker)


_DISMISS_TEXTS = ["我知道了", "知道了", "稍后再说", "不再提示"]
_DISMISS_SELECTORS = [
    ".semi-modal-close",
    'button[aria-label="Close"]',
    'button[aria-label="关闭"]',
    '[class*="close-icon"]',
    '[class*="modalClose"]',
    '[class*="dialog-close"]',
]


def _dismiss_dialogs(page) -> bool:
    dismissed = False
    for text in _DISMISS_TEXTS:
        try:
            loc = page.get_by_text(text, exact=True)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1500)
                dismissed = True
                page.wait_for_timeout(500)
        except Exception:
            pass
    for sel in _DISMISS_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1500)
                dismissed = True
                page.wait_for_timeout(500)
        except Exception:
            pass
    return dismissed


def _open_chat_page(page) -> bool:
    for attempt in range(3):
        try:
            page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
            _dismiss_dialogs(page)
            try:
                page.wait_for_selector(
                    ".conversationConversationListwrapper, [class*='conversationList'], "
                    "[class*='chatList'], [class*='conversationItem']",
                    timeout=12000,
                )
            except Exception:
                pass
            return True
        except Exception as e:
            logger.info("打开页面失败（第 %s 次）: %s", attempt + 1, str(e)[:80])
            if attempt < 2:
                time.sleep(3)
    return False


_SCROLL_LIST_JS = """
    (step) => {
        let el = null;
        const cand = document.querySelector(
            '.conversationConversationListwrapper, [class*="conversationList"], [class*="chatList"], [class*="ContactList"], [class*="contactList"]'
        );
        if (cand && cand.scrollHeight > cand.clientHeight) {
            el = cand;
        } else {
            const all = [...document.querySelectorAll('div')].filter(
                x => x.scrollHeight > x.clientHeight + 100 && x.clientHeight > 200 &&
                     x.getBoundingClientRect().left < 400
            );
            if (all.length) el = all[0];
        }
        if (!el) return { moved: false, atBottom: true };
        const before = el.scrollTop;
        el.scrollTop = before + step;
        const moved = el.scrollTop > before;
        return {
            moved: moved,
            atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 8,
        };
    }
"""


def _wait_real_list(page, timeout_ms: int = 40000) -> bool:
    try:
        page.wait_for_selector(
            ".conversationConversationItemtitle, [class*='conversationItem']",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _scroll_and_extract(page, collected: list[dict], max_rounds: int = 80) -> None:
    _dismiss_dialogs(page)
    seen = {c.get("name") for c in collected}
    stable = 0
    for _ in range(max_rounds):
        res = page.evaluate(_EXTRACT_JS) or {}
        data = res.get("items") or []
        new_items = []
        for x in data:
            name = x.get("name")
            if name and name not in seen:
                seen.add(name)
                new_items.append(x)
        if new_items:
            collected.extend(new_items)
            stable = 0
        else:
            stable += 1
        at_bottom = bool(res.get("atBottom"))
        if at_bottom or stable >= 3:
            break
        moved = False
        try:
            r = page.evaluate(_SCROLL_LIST_JS, 700) or {}
            moved = bool(r.get("moved"))
        except Exception:
            moved = False
        if not moved:
            try:
                page.mouse.move(200, 350)
                page.mouse.wheel(0, 700)
            except Exception:
                pass
        page.wait_for_timeout(250)


_EXTRACT_JS = r"""
    () => {
        const out = [];
        const seen = new Set();
        const rows = document.querySelectorAll('[class*="conversationConversationItemwrapper"]');
        const cleanName = (el) => {
            let direct = "";
            el.childNodes.forEach(n => { if (n.nodeType === 3) direct += n.textContent; });
            let name = direct.trim();
            if (!name) {
                const clone = el.cloneNode(true);
                clone.querySelectorAll(
                    '[class*="TagNextToTitle"], [class*="timeStr"], [class*="streak"], [class*="Streak"], [class*="badge"]'
                ).forEach(x => x.remove());
                name = (clone.textContent || "").trim();
            }
            return name.replace(/\\s+/g, " ").trim();
        };
        rows.forEach(row => {
            const rect = row.getBoundingClientRect();
            if (rect.height < 30 || rect.width < 100) return;
            let finalName = "";
            let titleEl = row.querySelector('.conversationConversationItemtitle');
            const wrap = titleEl ? null : row.querySelector('[class*="Itemtitle"]');
            if (!titleEl && wrap) titleEl = wrap.querySelector('.conversationConversationItemtitle');
            if (titleEl) {
                finalName = cleanName(titleEl);
            } else if (wrap) {
                const c2 = wrap.cloneNode(true);
                c2.querySelectorAll(
                    '[class*="TagNextToTitle"], [class*="timeStr"], [class*="streak"], [class*="Streak"], [class*="badge"]'
                ).forEach(x => x.remove());
                finalName = (c2.textContent || "").replace(/\\s+/g, " ").trim();
            }
            if (!finalName) return;
            if (/^\\d+$/.test(finalName)) return;
            if (/^\\d{1,2}:\\d{2}$/.test(finalName)) return;
            if (finalName === '消息' || finalName === '私信' || finalName === '朋友私信' || finalName === '通知') return;
            if (finalName.length > 40) return;
            if (seen.has(finalName)) return;
            seen.add(finalName);
            let streak = "";
            const pickDigits = (el) => {
                if (!el) return "";
                const m = (el.textContent || "").match(/(\\d{1,4})\\s*天/);
                return m ? m[1] : "";
            };
            let st = row.querySelector('[class*="commonStreaknormalText"]');
            if (!st) st = row.querySelector('[class*="treak"]');
            if (!st) {
                for (const el of row.querySelectorAll('[class]')) {
                    const cn = typeof el.className === 'string' ? el.className : '';
                    if (/streak/i.test(cn)) { st = el; break; }
                }
            }
            streak = pickDigits(st);
            if (!streak) {
                const m = (row.textContent || "").match(/(\\d{1,4})\\s*天(?!前)/);
                if (m) streak = m[1];
            }
            if (!streak) {
                const m = (row.textContent || "").match(/[🔥⚡]\\s*(\\d{1,4})(?:\\s*天)?(?!\\d)/);
                if (m) streak = m[1];
            }
            let avatar = "";
            const avImg = row.querySelector('img');
            if (avImg) {
                let asrc = avImg.getAttribute('src') || avImg.src || "";
                if (asrc.startsWith('//')) asrc = 'https:' + asrc;
                if (!asrc.includes('flame_icon')) avatar = asrc;
            }
            out.push({ name: finalName, streak: streak, avatar: avatar });
        });
        let atBottom = false;
        try {
            const scroller = document.querySelector(
                '.conversationConversationListwrapper, [class*="conversationList"], [class*="chatList"], [class*="ContactList"], [class*="contactList"]'
            );
            const el = scroller && scroller.scrollHeight > scroller.clientHeight ? scroller : document.scrollingElement;
            if (el) {
                atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
            } else {
                atBottom = true;
            }
        } catch (e) {}
        return { items: out, atBottom: atBottom };
    }
"""


def fetch_chat_contacts(account_id: str | None = None) -> dict:
    aid = account_id or DEFAULT_ACCOUNT_ID
    result = {"at": _now(), "names": [], "error": None}
    state = account_state_path(aid)
    if not state.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result
    api_names: list[dict] = []
    api_seen: set[str] = set()
    def _on_api_user_info(resp):
        try:
            if "aweme/v1/web/im/user/info" not in resp.url:
                return
            if resp.status != 200:
                return
            data = resp.json()
            items = data.get("data", []) or []
            if isinstance(items, dict):
                items = items.get("user_list") or items.get("list") or items.get("users") or []
            if not isinstance(items, list):
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                nick = _norm_name(item.get("remark_name") or item.get("nickname") or "")
                if not nick or nick in api_seen:
                    continue
                api_seen.add(nick)
                api_names.append({"name": nick, "streak": _probe_streak(item), "avatar": ""})
        except Exception:
            pass
    try:
        with open_browser(state_path=state) as (p, browser, context, page):
            page.on("response", _on_api_user_info)
            if not _open_chat_page(page):
                result["error"] = "无法打开抖音私信页面"
                return result
            real_rendered = _wait_real_list(page, 40000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result
            collected: list[dict] = []
            _scroll_and_extract(page, collected)
            if not collected and api_names:
                collected = list(api_names)
            if not collected:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=45000)
                    _wait_real_list(page, 25000)
                    _scroll_and_extract(page, collected)
                except Exception:
                    pass
            if collected and api_names:
                by_name = {c.get("name"): c for c in collected}
                for n in api_names:
                    cur = by_name.get(n.get("name"))
                    if cur is None:
                        collected.append(n)
                    elif not cur.get("streak") and n.get("streak"):
                        cur["streak"] = n.get("streak")
            result["names"] = collected
            logger.info("已读取聊天列表联系人 %s 个", len(result["names"]))
    except Exception as e:
        logger.error("获取联系人异常: %s", e)
        result["error"] = f"获取联系人异常: {e}"
    return result


def _b_channel_daily(account_id: str | None = None) -> tuple[str, int]:
    today = datetime.now().astimezone().date().isoformat()
    rec = load_runtime(account_id).get("b_channel_daily") or {}
    if rec.get("date") != today:
        return today, 0
    return today, int(rec.get("count", 0) or 0)


def compute_pending(cfg: dict | None = None, account_id: str | None = None) -> list[dict]:
    cfg = cfg or load_config(account_id)
    entries = ledger.get_selected(account_id)
    daily_limit = max(1, int(cfg.get("first_message_daily_limit", 1) or 1))
    _, creator_sent_today = _b_channel_daily(account_id)
    allow_first = bool(cfg.get("allow_first_message"))
    pending: list[dict] = []
    for e in entries:
        if e.get("has_conversation"):
            pending.append({**e, "send_channel": "consumer"})
        elif allow_first and creator_sent_today < daily_limit:
            pending.append({**e, "send_channel": "creator"})
    return pending


def _send_consumer(page, entry: dict, msg: str, dry_run: bool, result: dict, account_id: str | None = None, tracker: MessageSendTracker | None = None) -> None:
    name = entry["display_name"]
    ok, why = send_to_contact(page, name, msg, dry_run, tracker=tracker)
    if ok:
        result["ok"].append(name)
        if not dry_run:
            ledger.confirm_join(name, account_id)
            days = _probe_streak_from_page(page)
            if days:
                ledger.set_streak(name, days, account_id)
    else:
        if entry.get("channel") == "creator":
            result["skipped"].append({
                "name": name,
                "reason": "consumer 定位失败（该好友为 creator-only），已降级跳过",
            })
            ledger.mark_no_consumer_conversation(name, account_id)
        else:
            result["failed"].append({"name": name, "reason": why})
            if detect_rate_limit(page):
                result["rate_limited"] = True
    if not dry_run:
        ledger.update_send_result(name, ok, _now(), account_id=account_id, msg_text=msg)


def _send_creator(entry: dict, msg: str, dry_run: bool, result: dict, p, account_id: str | None = None) -> None:
    name = entry["display_name"]
    cfg = load_config(account_id)
    allow_first = bool(cfg.get("allow_first_message"))
    daily_limit = max(1, int(cfg.get("first_message_daily_limit", 1) or 1))
    today, count = _b_channel_daily(account_id)
    if not allow_first:
        result["skipped"].append({"name": name, "reason": "无会话且未开启「允许首条消息」"})
        return
    if count >= daily_limit:
        result["skipped"].append({
            "name": name,
            "reason": f"今日已发送首条消息 {count}/{daily_limit}",
        })
        return
    ok, why = creator_channel.send_first_message(entry, msg, dry_run, p, account_id)
    if ok:
        result["ok"].append(name)
        if not dry_run:
            ledger.update_send_result(name, True, _now(), via_creator=True, account_id=account_id, msg_text=msg)
            update_runtime(account_id, b_channel_daily={"date": today, "count": count + 1})
    else:
        result["failed"].append({"name": name, "reason": f"通道B: {why}"})
        if "限流" in why or "停止" in why:
            result["rate_limited"] = True


def run_send(dry_run: bool = False, only_names: list[str] | None = None, account_id: str | None = None) -> dict:
    aid = account_id or DEFAULT_ACCOUNT_ID
    cfg = load_config(aid)
    messages = cfg.get("messages") or ["🔥"]
    max_n = int(cfg.get("max_friends_per_run", 0) or 0)
    gap_min = max(1, int(cfg.get("send_gap_min", 6) or 6))
    gap_max = max(gap_min, int(cfg.get("send_gap_max", 12) or 12))
    result = {
        "at": _now(), "dry_run": bool(dry_run),
        "ok": [], "failed": [], "skipped": [],
        "logged_out": False, "rate_limited": False,
        "account_id": aid,
    }
    state = account_state_path(aid)
    if not state.exists():
        result["failed"].append({"name": "_system", "reason": "该账号尚未上传登录态 state.json"})
        return result
    targets = ledger.get_selected(aid)
    if not targets and cfg.get("friends"):
        stats = ledger.import_config_friends(cfg["friends"], aid)
        targets = ledger.get_selected(aid)
    if only_names is not None:
        targets = [t for t in targets if t.get("display_name") in only_names]
    targets = targets[:max_n] if max_n > 0 else targets
    if not targets:
        logger.info("[%s] 未配置任何好友，跳过发送", aid)
        return result
    try:
        with open_browser(state_path=state) as (p, browser, context, page):
            if not _open_chat_page(page):
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                return result
            tracker = MessageSendTracker(page)
            try:
                time.sleep(5)
                _dismiss_dialogs(page)
                logged, why = check_login(page)
                if not logged:
                    result["logged_out"] = True
                    result["failed"].append({"name": "_system", "reason": why})
                    return result
                logger.info("[%s] 待发送好友 %s 人，dry_run=%s", aid, len(targets), dry_run)
                for entry in targets:
                    name = entry.get("display_name", "?")
                    try:
                        if "/chat" not in (page.url or ""):
                            _open_chat_page(page)
                        logged, why = check_login(page)
                        if not logged:
                            result["logged_out"] = True
                            result["failed"].append({"name": name, "reason": f"登录态失效，未发送: {why}"})
                            break
                        custom_msg = str(entry.get("custom_message") or "").strip()
                        if custom_msg:
                            msg = custom_msg
                        else:
                            msg = build_message(messages, last_sent_msg=str(entry.get("last_msg", "")))
                        if entry.get("has_conversation"):
                            _send_consumer(page, entry, msg, dry_run, result, aid, tracker=tracker)
                        else:
                            _send_creator(entry, msg, dry_run, result, p, aid)
                    except Exception as e:
                        logger.error("[%s] 处理好友 %s 异常: %s", aid, name, e)
                        result["failed"].append({"name": name, "reason": f"处理异常: {e}"})
                    if result["rate_limited"]:
                        break
                    time.sleep(random.uniform(gap_min, gap_max))
            finally:
                tracker.close()
    except Exception as e:
        logger.error("[%s] 运行异常: %s", aid, e)
        result["failed"].append({"name": "_system", "reason": f"运行异常: {e}"})
    return result


sync_contacts = fetch_chat_contacts
