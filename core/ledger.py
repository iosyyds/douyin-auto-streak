"""好友台账：以会话显示名为键的本地持久化好友库。"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .config import DEFAULT_ACCOUNT_ID, account_dir
from .avatar import fetch_and_save_avatar


def ledger_path(account_id: str | None = None) -> Path:
    return account_dir(account_id) / "ledger.json"


_lock = threading.RLock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_entry(display_name: str) -> dict:
    return {
        "display_name": display_name,
        "nickname": "",
        "short_id": "",
        "user_id": "",
        "avatar": "",
        "custom_message": "",
        "streak_days": 0,
        "has_conversation": False,
        "selected": False,
        "selected_order": None,
        "last_sent_at": None,
        "channel": "none",
        "join_confidence": "low",
        "source": {"creator": False, "consumer": False},
    }


def _parse_streak(text) -> int:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else 0


def _norm_ws(s) -> str:
    return str(s or "").replace("\u00a0", " ").strip()


def load_ledger(account_id: str | None = None) -> list[dict]:
    with _lock:
        entries: list[dict] = []
        lp = ledger_path(account_id)
        if lp.exists():
            try:
                data = json.loads(lp.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [dict(e) for e in data if isinstance(e, dict) and e.get("display_name")]
                    for e in entries:
                        e["no_consumer_conversation"] = not e.get("has_conversation")
                        e["creator_only"] = e.get("channel") == "creator" and e["no_consumer_conversation"]
            except Exception:
                pass
        return entries


def _save(entries: list[dict], account_id: str | None = None) -> None:
    with _lock:
        d = account_dir(account_id)
        d.mkdir(parents=True, exist_ok=True)
        lp = ledger_path(account_id)
        temp_lp = lp.with_suffix('.tmp')
        temp_lp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_lp.replace(lp)


def _upsert(entries: list[dict], entry: dict) -> dict:
    for e in entries:
        if e.get("display_name") == entry["display_name"]:
            selected = e.get("selected", False)
            last_sent = e.get("last_sent_at")
            e.update(entry)
            for k, v in _default_entry(entry["display_name"]).items():
                e.setdefault(k, v)
            e["selected"] = selected
            e["last_sent_at"] = last_sent
            return e
    base = _default_entry(entry["display_name"])
    base.update(entry)
    entries.append(base)
    return base


def merge_consumer_contacts(contacts: list[dict], account_id: str | None = None) -> dict:
    with _lock:
        entries_snapshot = load_ledger(account_id)
    by_name_snapshot = {e.get("display_name"): e for e in entries_snapshot}
    contacts = contacts or []

    def _resolve_avatar(c: dict) -> str:
        avatar_url = c.get("avatar") or ""
        if not avatar_url:
            return ""
        streak = _parse_streak(c.get("streak"))
        if streak == 0:
            old = by_name_snapshot.get(str(c.get("name", "")).strip())
            return (old.get("avatar") or avatar_url) if old else avatar_url
        new_avatar = fetch_and_save_avatar(avatar_url, account_id)
        if new_avatar:
            return new_avatar
        old = by_name_snapshot.get(str(c.get("name", "")).strip())
        return (old.get("avatar") or avatar_url) if old else avatar_url

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        avatar_paths = list(pool.map(_resolve_avatar, contacts))

    with _lock:
        entries = load_ledger(account_id)
        by_name = {e.get("display_name"): e for e in entries}
        added = 0
        updated = 0
        for c, avatar_path in zip(contacts, avatar_paths):
            name = str(c.get("name", "")).strip()
            if not name:
                continue
            exists = name in by_name
            old_entry = by_name.get(name) or {}
            old_avatar = old_entry.get("avatar") or ""
            new_streak = _parse_streak(c.get("streak"))
            if 0 < new_streak <= 9999:
                eff_streak = new_streak
            else:
                eff_streak = int(old_entry.get("streak_days") or 0)
            e = _upsert(entries, {
                "display_name": name,
                "streak_days": eff_streak,
                "has_conversation": True,
                "channel": "consumer",
                "avatar": avatar_path or old_avatar,
            })
            e.setdefault("source", {})["consumer"] = True
            if e.get("source", {}).get("creator") and e.get("nickname") == name:
                e["join_confidence"] = "high"
            if not exists:
                added += 1
            else:
                updated += 1
        _save(entries, account_id)
        return {"added": added, "updated": updated, "total": len(entries)}


def get_selected(account_id: str | None = None) -> list[dict]:
    return [e for e in load_ledger(account_id) if e.get("selected")]


def set_custom_message(display_name: str, message: str, account_id: str | None = None) -> bool:
    with _lock:
        entries = load_ledger(account_id)
        found = False
        for e in entries:
            if e.get("display_name") == display_name:
                e["custom_message"] = str(message or "").strip()
                found = True
                break
        if found:
            _save(entries, account_id)
        return found


def set_selected(entries_in: list[dict], account_id: str | None = None) -> dict:
    with _lock:
        entries = load_ledger(account_id)
        by_name = {e.get("display_name"): e for e in entries}
        updated = 0
        added = 0
        for it in entries_in:
            name = str(it.get("display_name", "")).strip()
            sel = bool(it.get("selected"))
            order = it.get("selected_order")
            if not name:
                continue
            if name in by_name:
                e = by_name[name]
                if bool(e.get("selected")) != sel:
                    e["selected"] = sel
                    updated += 1
                new_order = order if sel else None
                if e.get("selected_order") != new_order:
                    e["selected_order"] = new_order
                    updated += 1
                if "custom_message" in it:
                    c_msg = str(it.get("custom_message") or "").strip()
                    if e.get("custom_message") != c_msg:
                        e["custom_message"] = c_msg
                        updated += 1
            else:
                entries.append({
                    **_default_entry(name),
                    "has_conversation": False,
                    "selected": sel,
                    "selected_order": order if sel else None,
                    "custom_message": str(it.get("custom_message") or "").strip(),
                })
                by_name[name] = entries[-1]
                added += 1
        if updated or added:
            _save(entries, account_id)
        return {"updated": updated, "added": added}


def update_send_result(
    display_name: str,
    ok: bool,
    at: str | None = None,
    via_creator: bool = False,
    account_id: str | None = None,
    msg_text: str | None = None,
) -> None:
    entries = load_ledger(account_id)
    for e in entries:
        if e.get("display_name") == display_name:
            e["last_sent_at"] = at or _now()
            if ok and not via_creator:
                e["has_conversation"] = True
            if ok and msg_text:
                e["last_msg"] = msg_text
            break
    _save(entries, account_id)


def stats(account_id: str | None = None) -> dict:
    entries = load_ledger(account_id)
    now = datetime.now().astimezone()
    week_ago = (now - timedelta(days=7)).isoformat()
    high = sum(1 for e in entries if e.get("join_confidence") == "high")
    no_conv = [
        {"display_name": e["display_name"], "channel": e.get("channel")}
        for e in entries
        if not e.get("has_conversation")
    ]
    low_pending = [
        e["display_name"]
        for e in entries
        if e.get("join_confidence") == "low" and e.get("has_conversation")
    ]
    recent_sent = [
        e["display_name"]
        for e in entries
        if str(e.get("last_sent_at") or "") >= week_ago
    ]
    top_streak = sorted(entries, key=lambda e: -(e.get("streak_days") or 0))[:10]
    return {
        "total": len(entries),
        "selected": sum(1 for e in entries if e.get("selected")),
        "confidence": {"high": high, "low": len(entries) - high},
        "with_short_id": sum(1 for e in entries if e.get("short_id")),
        "no_conversation": no_conv,
        "low_pending": low_pending,
        "recent_sent_7d": recent_sent,
        "top_streak": [
            {"display_name": e["display_name"], "streak_days": e.get("streak_days")}
            for e in top_streak
        ],
    }


update_from_sync = merge_consumer_contacts
