"""多账号管理：账号注册表、账号目录解析与全局并发控制。"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .config import ACCOUNTS_DIR, DATA_DIR, account_dir

REGISTRY_PATH = DATA_DIR / "accounts" / "accounts.json"

MAX_CONCURRENT_BROWSERS = 5

_lock = threading.Lock()
_current_account: threading.local = threading.local()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_account(account_id: str) -> dict:
    return {
        "id": account_id,
        "name": "默认账号",
        "enabled": True,
        "device": "",
        "created_at": _now(),
        "updated_at": _now(),
    }


def _load_registry() -> dict[str, dict]:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            out[str(item["id"])] = item
    return out


def _save_registry(reg: dict[str, dict]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries = sorted(reg.values(), key=lambda a: a.get("created_at", ""))
    REGISTRY_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_accounts() -> list[dict]:
    reg = _load_registry()
    out: list[dict] = []
    ids = ["default"] + sorted(k for k in reg if k != "default")
    for aid in ids:
        meta = reg.get(aid) or _default_account(aid)
        d = account_dir(aid)
        entry = {
            **meta,
            "is_default": aid == "default",
            "dir": str(d),
            "state_file_exists": (d / "state.json").exists(),
            "config_file_exists": (d / "config.json").exists(),
        }
        out.append(entry)
    return out


def get_account(account_id: str) -> dict | None:
    reg = _load_registry()
    if account_id == "default":
        meta = reg.get("default") or _default_account("default")
        return {
            **meta,
            "is_default": True,
            "dir": str(account_dir("default")),
        }
    meta = reg.get(account_id)
    if meta is None:
        return None
    return {**meta, "is_default": False, "dir": str(account_dir(account_id))}


def account_exists(account_id: str) -> bool:
    if account_id == "default":
        return True
    return account_id in _load_registry()


def create_account(name: str = "", device: str = "") -> dict:
    with _lock:
        reg = _load_registry()
        while True:
            aid = f"acc_{uuid.uuid4().hex[:8]}"
            if aid not in reg:
                break
        meta = _default_account(aid)
        meta["name"] = (name or "").strip() or f"账号 {aid[4:8]}"
        meta["device"] = (device or "").strip()
        reg[aid] = meta
        _save_registry(reg)
    d = account_dir(aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return {**meta, "is_default": False, "dir": str(d)}


def update_account(account_id: str, name: str | None = None, device: str | None = None, enabled: bool | None = None) -> dict | None:
    with _lock:
        reg = _load_registry()
        if account_id == "default":
            meta = reg.get("default") or _default_account("default")
        elif account_id not in reg:
            return None
        else:
            meta = reg[account_id]
        if name is not None:
            meta["name"] = (name or "").strip() or meta["name"]
        if device is not None:
            meta["device"] = (device or "").strip()
        if enabled is not None:
            meta["enabled"] = bool(enabled)
        meta["updated_at"] = _now()
        reg[account_id] = meta
        _save_registry(reg)
    return get_account(account_id)


def remove_account(account_id: str) -> bool:
    if account_id == "default":
        return False
    with _lock:
        reg = _load_registry()
        if account_id not in reg:
            return False
        del reg[account_id]
        _save_registry(reg)

    d = account_dir(account_id)
    if d.exists():
        archived = DATA_DIR / "archived" / f"{account_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        try:
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(archived))
        except Exception:
            pass
    return True


def set_current_account(account_id: str) -> None:
    _current_account.id = account_id


def current_account() -> str:
    return getattr(_current_account, "id", "default") or "default"


def acquire_browser_slot() -> None:
    _BROWSER_SLOT.acquire()


def release_browser_slot() -> None:
    _BROWSER_SLOT.release()


def browser_slots_available() -> int:
    return int(_BROWSER_SLOT._value)


_BROWSER_SLOT = threading.BoundedSemaphore(MAX_CONCURRENT_BROWSERS)
