"""Evening batch entrypoint.

Run by tellybear-batch.timer at TB_BATCH_HOUR_LOCAL. Drains all currently-queued batch
requests through the same worker code path (so behavior is identical to the always-on
worker). target_date is informational for now: all queued requests are
generated regardless of date.
"""
from __future__ import annotations

from tellybear.core.logging import get_logger
from tellybear.worker import runner

log = get_logger("batch")


def main() -> None:
    log.info("evening batch starting: draining queued requests")
    n = runner.drain()
    log.info("evening batch done: processed %d request(s)", n)


if __name__ == "__main__":
    main()
