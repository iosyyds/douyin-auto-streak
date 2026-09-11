"""全局单工作线程执行器。"""

from __future__ import annotations

import logging
import queue
import threading
import time

logger = logging.getLogger("douyin-cloud-streak")


class _Executor:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="browser-worker")
        self._thread.start()

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            if fn is None:
                break
            try:
                fn()
            except Exception:
                logger.exception("浏览器任务执行异常")

    def submit(self, fn) -> None:
        self._q.put(fn)

    def submit_and_wait(self, fn, timeout: float | None = None):
        holder: dict = {}

        def _wrap() -> None:
            try:
                holder["result"] = fn()
            except Exception as e:
                holder["error"] = e

        self._q.put(_wrap)
        end = None if timeout is None else time.time() + timeout
        while "result" not in holder and "error" not in holder:
            if end is not None and time.time() > end:
                raise TimeoutError("任务执行超时")
            time.sleep(0.05)
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def shutdown(self) -> None:
        self._q.put(None)


executor = _Executor()
