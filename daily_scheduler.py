"""Daily lifetime-proxy recheck scheduler."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import time
from zoneinfo import ZoneInfo

import config
from checker import check_all, group_and_rank
from delivery import send_logs_album
from lifetime_store import lifetime_store

logger = logging.getLogger(__name__)
_scheduler_task: asyncio.Task | None = None


def seconds_until_next_run(now: datetime | None = None) -> float:
    zone = ZoneInfo(config.DAILY_LOG_TIMEZONE)
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    target = current.replace(
        hour=config.DAILY_LOG_HOUR,
        minute=config.DAILY_LOG_MINUTE,
        second=0,
        microsecond=0,
    )
    if target <= current:
        target += timedelta(days=1)
    return max(0.0, (target - current).total_seconds())


async def run_daily_lifetime_recheck(bot) -> tuple[int, int]:
    """Recheck the full SQLite set, trim failures, and publish surviving files."""
    snapshot_started_at = time.time()
    proxies = await asyncio.to_thread(lifetime_store.load_all)
    if not proxies:
        logger.info("Daily lifetime recheck skipped: database is empty")
        return 0, 0

    results = await check_all(proxies)
    groups, working = group_and_rank(results)
    kept, removed = await asyncio.to_thread(
        lifetime_store.reconcile,
        proxies,
        results,
        snapshot_started_at=snapshot_started_at,
    )

    if config.GROUP_ID:
        try:
            await send_logs_album(
                bot,
                config.GROUP_ID,
                "Daily scheduler",
                len(proxies),
                working,
                groups,
                daily=True,
                checked=results,
            )
        except Exception as error:
            logger.warning("Could not send daily lifetime proxy album: %s", error)

    logger.info(
        "Daily lifetime recheck complete: checked=%d working=%d removed=%d",
        len(proxies),
        kept,
        removed,
    )
    return kept, removed


async def daily_scheduler_loop(bot) -> None:
    while True:
        try:
            delay = seconds_until_next_run()
            logger.info(
                "Next lifetime recheck in %.0f seconds (%02d:%02d %s)",
                delay,
                config.DAILY_LOG_HOUR,
                config.DAILY_LOG_MINUTE,
                config.DAILY_LOG_TIMEZONE,
            )
            await asyncio.sleep(delay)
            await run_daily_lifetime_recheck(bot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Daily lifetime scheduler failed: %s", error)
            await asyncio.sleep(60)


def launch_daily_scheduler(bot) -> asyncio.Task:
    """Start exactly one scheduler task for this process."""
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(
            daily_scheduler_loop(bot),
            name="daily-lifetime-proxy-recheck",
        )
    return _scheduler_task