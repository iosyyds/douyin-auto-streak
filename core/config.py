"""配置读写。配置保存在 data/config.json（默认账号）或 data/accounts/{id}/config.json，由网页端编辑。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_DIR = DATA_DIR / "accounts"
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
ROOT_STATE_PATH = BASE_DIR / "state.json"

DEFAULT_ACCOUNT_ID = "default"


def account_dir(account_id: str | None = None) -> Path:
    aid = account_id or DEFAULT_ACCOUNT_ID
    if aid == DEFAULT_ACCOUNT_ID:
        return DATA_DIR
    return ACCOUNTS_DIR / aid


def account_config_path(account_id: str | None = None) -> Path:
    return account_dir(account_id) / "config.json"


def account_state_path(account_id: str | None = None) -> Path:
    return account_dir(account_id) / "state.json"


def get_valid_state_path(account_id: str | None = None) -> Path | None:
    aid = account_id or DEFAULT_ACCOUNT_ID
    sp = account_state_path(aid)
    if sp.exists() and sp.stat().st_size > 30:
        return sp
    if aid == DEFAULT_ACCOUNT_ID and ROOT_STATE_PATH.exists() and ROOT_STATE_PATH.stat().st_size > 30:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(ROOT_STATE_PATH, sp)
        except Exception:
            pass
        return sp
    return None


DEFAULT_CONFIG = {
    "schedule_time": "08:00",
    "jitter_minutes": 30,
    "send_gap_min": 6,
    "send_gap_max": 12,
    "max_friends_per_run": 0,
    "friends": [],
    "messages": ["早上好"],
    "creator_user_detail_path": "aweme/v1/creator/im/user_detail/",
    "creator_max_scrolls": 80,
    "auto_run_enabled": True,
    "allow_first_message": False,
    "first_message_daily_limit": 1,
    "schedule_harvest_day": "mon",
}

_lock = threading.Lock()


def load_config(account_id: str | None = None) -> dict:
    aid = account_id or DEFAULT_ACCOUNT_ID
    cfg = dict(DEFAULT_CONFIG)
    cpath = account_config_path(aid)
    if cpath.exists():
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict | None, account_id: str | None = None) -> dict:
    aid = account_id or DEFAULT_ACCOUNT_ID
    merged = dict(DEFAULT_CONFIG)
    if cfg:
        merged.update(cfg)

    merged["friends"] = [str(x).strip() for x in merged.get("friends", []) if str(x).strip()]
    merged["messages"] = [str(x) for x in merged.get("messages", []) if str(x).strip()]
    if not merged["messages"]:
        merged["messages"] = ["早上好"]

    schedule = str(merged.get("schedule_time", "08:00"))
    try:
        hh, mm = schedule.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        merged["schedule_time"] = f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        raise ValueError("schedule_time 必须是 HH:MM 格式")

    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run", "creator_max_scrolls", "first_message_daily_limit"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]
    merged["auto_run_enabled"] = bool(merged.get("auto_run_enabled"))
    merged["allow_first_message"] = bool(merged.get("allow_first_message"))
    day = str(merged.get("schedule_harvest_day") or "").strip().lower()
    merged["schedule_harvest_day"] = day if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun", "off"} else "off"

    with _lock:
        d = account_dir(aid)
        d.mkdir(parents=True, exist_ok=True)
        cpath = account_config_path(aid)
        tmp_path = cpath.with_suffix('.tmp')
        tmp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(cpath)
    return merged
