from __future__ import annotations

import logging
import threading
from datetime import datetime

from .weekly_sync import WeeklySyncRunner


LOG = logging.getLogger(__name__)


class WeeklyScheduler:
    """Small catch-up scheduler; launchd keeps the owning web process alive."""

    def __init__(self, runner: WeeklySyncRunner, check_interval_seconds: float = 60.0) -> None:
        self.runner = runner
        self.check_interval_seconds = max(5.0, float(check_interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="wechat-weekly-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def tick(self, now: datetime | None = None) -> None:
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self.runner.run_due(now=now)
        finally:
            self._tick_lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception("weekly archive scheduler tick failed")
            self._stop.wait(self.check_interval_seconds)
