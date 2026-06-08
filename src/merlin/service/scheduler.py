"""Background jobs via APScheduler (daemon only).

Sync runs against YouTube Music; if the user hasn't authenticated yet the jobs
simply no-op rather than crashing the scheduler. Sync work is synchronous
(ytmusicapi), so we hand it to a worker thread to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from merlin.config import Settings

log = logging.getLogger("merlin.scheduler")


async def _sync_library_job() -> None:
    from merlin.clients.ytmusic import YTMusicClient, YTMusicError
    from merlin.core.engine import Engine

    if not YTMusicClient().is_authenticated():
        log.info("library sync skipped — YTM not authenticated")
        return
    try:
        counts = await asyncio.to_thread(Engine().sync_library)
        log.info("library sync done: %s", counts)
    except YTMusicError as e:
        log.warning("library sync failed: %s", e)
    except Exception:  # never let a job kill the scheduler
        log.exception("library sync errored")


async def _prefetch_features_job() -> None:
    from merlin.core.engine import Engine

    try:
        n = await asyncio.to_thread(Engine().prefetch_features, 100)
        if n:
            log.info("prefetched AB features for %d recordings", n)
    except Exception:
        log.exception("feature prefetch errored")


def build_scheduler(settings: Settings) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    # Nightly full library/likes/history snapshot.
    sched.add_job(
        _sync_library_job, "cron", hour=3, id="nightly_sync", replace_existing=True
    )
    # Warm the AcousticBrainz cache a few times a day, gently.
    sched.add_job(
        _prefetch_features_job,
        "interval",
        hours=6,
        id="prefetch_features",
        replace_existing=True,
    )
    return sched
