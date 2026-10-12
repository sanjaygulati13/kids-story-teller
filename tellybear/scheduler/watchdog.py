"""Watchdog entrypoint.

Run by tellybear-watchdog.timer every 5 minutes. Re-queues requests stuck in `generating`
and stories stuck in `synthesizing` for longer than the stale window, so a crashed job is
recovered automatically.
"""
from __future__ import annotations

from tellybear.core.logging import get_logger
from tellybear.db.base import SessionLocal
from tellybear.worker import runner

log = get_logger("watchdog")


def main() -> None:
    db = SessionLocal()
    try:
        n = runner.requeue_stale(db)
        if n:
            log.info("watchdog re-queued %d stuck job(s)", n)
    finally:
        db.close()


if __name__ == "__main__":
    main()
