"""每天定时触发发送任务（按账号独立注册 job）。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .accounts import list_accounts
from .config import DEFAULT_ACCOUNT_ID, load_config

logger = logging.getLogger("douyin-cloud-streak")
TZ = "Asia/Shanghai"

_scheduler: BackgroundScheduler | None = None
_run_func: Callable | None = None
_harvest_func: Callable | None = None
_on_outcome: Callable | None = None


def _notify_outcome(account_id: str, ok: bool, detail: str) -> None:
    if _on_outcome:
        try:
            _on_outcome(account_id, ok, detail)
        except Exception:
            pass


def _job_id(account_id: str, kind: str) -> str:
    return f"{kind}_{account_id}"


def _gates_open(account_id: str) -> tuple[bool, str]:
    try:
        acc = next((a for a in list_accounts() if a["id"] == account_id), None)
    except Exception:
        acc = None
    if acc is not None and not acc.get("enabled", True):
        return False, "账号已停用"
    cfg = load_config(account_id)
    if not bool(cfg.get("auto_run_enabled", True)):
        return False, "auto_run_enabled=false"
    return True, ""


def _daily_job(account_id: str) -> None:
    ok, reason = _gates_open(account_id)
    if not ok:
        logger.info("[%s] 定时任务跳过：%s", account_id, reason)
        _notify_outcome(account_id, False, reason)
        return
    jitter = max(0, int(load_config(account_id).get("jitter_minutes", 30) or 30))
    if jitter:
        delay = random.uniform(0, jitter * 60)
        logger.info("[%s] 随机延迟 %.0f 秒后开始发送", account_id, delay)
        time.sleep(delay)
        ok, reason = _gates_open(account_id)
        if not ok:
            logger.info("[%s] 抖动等待后定时任务跳过：%s", account_id, reason)
            _notify_outcome(account_id, False, reason)
            return
    if not _run_func:
        return
    try:
        _run_func(account_id=account_id)
    except Exception as e:
        logger.error("[%s] 定时发送未能触发: %s", account_id, e)
        _notify_outcome(account_id, False, f"触发失败: {e}")
    else:
        _notify_outcome(account_id, True, "")


def _harvest_job(account_id: str) -> None:
    if _harvest_func:
        _harvest_func(account_id=account_id)


def configure(run_func: Callable, harvest_func: Callable | None = None,
              on_scheduled_outcome: Callable | None = None) -> None:
    global _scheduler, _run_func, _harvest_func, _on_outcome
    _run_func = run_func
    _harvest_func = harvest_func
    _on_outcome = on_scheduled_outcome
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def _remove_job_safe(job_id: str) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass


def apply_schedule(account_id: str | None = None) -> None:
    if _scheduler is None:
        return

    accounts = list_accounts()
    if account_id is not None:
        accounts = [a for a in accounts if a["id"] == account_id]
    if not accounts:
        return

    for acc in accounts:
        aid = acc["id"]
        if not acc.get("enabled", True):
            for kind in ("daily_send", "weekly_harvest", "retry"):
                _remove_job_safe(_job_id(aid, kind))
            continue

        cfg = load_config(aid)
        hh, mm = cfg.get("schedule_time", "21:00").split(":")
        _scheduler.add_job(
            _daily_job,
            CronTrigger(hour=int(hh), minute=int(mm), timezone=TZ),
            args=[aid],
            id=_job_id(aid, "daily_send"),
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )

        day = str(cfg.get("schedule_harvest_day") or "off").strip().lower()
        if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"} and _harvest_func:
            _scheduler.add_job(
                _harvest_job,
                CronTrigger(day_of_week=day, hour=3, minute=0, timezone=TZ),
                args=[aid],
                id=_job_id(aid, "weekly_harvest"),
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=3600,
            )
        else:
            _remove_job_safe(_job_id(aid, "weekly_harvest"))


def _next_run(job) -> str | None:
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def next_run_time(account_id: str | None = None) -> str | None:
    if _scheduler is None:
        return None
    if account_id is None:
        return next_run_time(DEFAULT_ACCOUNT_ID)
    return _next_run(_scheduler.get_job(_job_id(account_id, "daily_send")))


def next_harvest_time(account_id: str | None = None) -> str | None:
    if _scheduler is None:
        return None
    if account_id is None:
        return next_harvest_time(DEFAULT_ACCOUNT_ID)
    return _next_run(_scheduler.get_job(_job_id(account_id, "weekly_harvest")))


def _retry_job(run_func: Callable, account_id: str) -> None:
    ok, reason = _gates_open(account_id)
    if not ok:
        logger.info("[%s] 自动补发已跳过：%s", account_id, reason)
        _notify_outcome(account_id, False, f"补发跳过: {reason}")
        return
    try:
        run_func()
    except Exception as e:
        logger.error("[%s] 自动补发执行失败: %s", account_id, e)
        _notify_outcome(account_id, False, f"补发失败: {e}")


def schedule_retry(run_func: Callable, delay_minutes: int = 45, account_id: str | None = None) -> None:
    if _scheduler is None:
        return
    aid = account_id or DEFAULT_ACCOUNT_ID
    job_id = _job_id(aid, "retry")
    if _scheduler.get_job(job_id):
        return
    run_at = datetime.now() + timedelta(minutes=delay_minutes)
    _scheduler.add_job(
        _retry_job,
        DateTrigger(run_date=run_at, timezone=TZ),
        args=[run_func, aid],
        id=job_id,
        replace_existing=True,
    )


def cancel_retry(account_id: str | None = None) -> None:
    job_id = _job_id(account_id or DEFAULT_ACCOUNT_ID, "retry")
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
