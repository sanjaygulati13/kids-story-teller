"""In-process scheduler for container installs (stands in for the two systemd timers).

- watchdog: every 5 minutes, re-queues jobs stuck mid-flight.
- nightly batch: once a day, at or after TB_BATCH_HOUR_LOCAL (local time — set TZ on the
  container), writes every queued "tonight" story. If the scheduler starts after that hour it runs
  the batch right away, like systemd's Persistent=true; draining is idempotent, so an extra run
  after a restart is harmless.

  python -m tellybear.scheduler.loop
"""
from __future__ import annotations

import time
from datetime import datetime

from tellybear.core.config import get_settings
from tellybear.core.logging import get_logger
from tellybear.scheduler import batch, watchdog

log = get_logger("scheduler")

WATCHDOG_EVERY_SECONDS = 300
TICK_SECONDS = 30


def main() -> None:
    s = get_settings()
    log.info("scheduler starting (watchdog every %ds, batch daily at %02d:00 local)",
             WATCHDOG_EVERY_SECONDS, s.TB_BATCH_HOUR_LOCAL)
    next_watchdog = time.monotonic() + 120  # give the worker a moment after boot
    last_batch_day = None
    while True:
        if time.monotonic() >= next_watchdog:
            next_watchdog = time.monotonic() + WATCHDOG_EVERY_SECONDS
            try:
                watchdog.main()
            except Exception as e:  # never let the loop die
                log.exception("watchdog run failed: %s", e)
        now = datetime.now()
        if now.hour >= s.TB_BATCH_HOUR_LOCAL and last_batch_day != now.date():
            last_batch_day = now.date()
            try:
                batch.main()
            except Exception as e:
                log.exception("nightly batch failed: %s", e)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
