"""FastAPI daemon. Bind to loopback only — never expose this to the internet.

Run with:  uvicorn merlin.service.app:app --host 127.0.0.1 --port 7654
or simply: merlin serve
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

from merlin.clients.ytmusic import YTMusicError
from merlin.config import get_settings
from merlin.core.engine import Engine, RadioResult
from merlin.core.models import Track
from merlin.db.database import get_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    get_db(settings)  # init schema eagerly

    scheduler = None
    if settings.scheduler_enabled:
        from merlin.service.scheduler import build_scheduler

        scheduler = build_scheduler(settings)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(title="Merlin", version="0.1.0", lifespan=lifespan)


# --- response models ---------------------------------------------------------


class TrackOut(BaseModel):
    title: str
    artists: list[str]
    album: str | None = None
    video_id: str | None = None
    mbid: str | None = None

    @classmethod
    def of(cls, t: Track) -> TrackOut:
        return cls(
            title=t.title,
            artists=t.artists,
            album=t.album,
            video_id=t.video_id,
            mbid=t.mbid,
        )


class RadioOut(BaseModel):
    seed: TrackOut
    tracks: list[TrackOut]
    playlist_id: str | None
    playlist_url: str | None

    @classmethod
    def of(cls, r: RadioResult) -> RadioOut:
        return cls(
            seed=TrackOut.of(r.seed),
            tracks=[TrackOut.of(t) for t in r.tracks],
            playlist_id=r.playlist_id,
            playlist_url=r.playlist_url,
        )


# --- routes ------------------------------------------------------------------


@app.get("/status")
def status() -> dict:
    from merlin.clients.ytmusic import YTMusicClient

    settings = get_settings()
    db = get_db(settings)
    return {
        "service": "merlin",
        "version": "0.1.0",
        "data_dir": str(settings.data_dir),
        "auth": {
            "ytm": YTMusicClient(settings).is_authenticated(),
            "lastfm": bool(settings.lastfm_api_key),
            "listenbrainz": bool(settings.listenbrainz_token),
        },
        "db": db.stats(),
    }


@app.post("/radio")
def radio(
    query: str = Body(..., embed=True),
    size: int = Body(50, embed=True),
    dry_run: bool = Body(False, embed=True),
) -> RadioOut:
    try:
        result = Engine().build_radio(query, size=size, dry_run=dry_run)
    except YTMusicError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return RadioOut.of(result)


@app.post("/similar-playlist")
def similar(
    seed: str = Body(..., embed=True),
    size: int = Body(50, embed=True),
    name: str | None = Body(None, embed=True),
    dry_run: bool = Body(False, embed=True),
) -> RadioOut:
    try:
        result = Engine().build_similar(seed, size=size, name=name, dry_run=dry_run)
    except YTMusicError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return RadioOut.of(result)


@app.post("/lb-radio")
def lb_radio(
    prompt: str = Body(..., embed=True),
    mode: str = Body("medium", embed=True),
    size: int = Body(50, embed=True),
    name: str | None = Body(None, embed=True),
    dry_run: bool = Body(False, embed=True),
) -> RadioOut:
    from merlin.clients.listenbrainz import ListenBrainzAuthError

    try:
        result = Engine().build_lb_radio(
            prompt, mode=mode, size=size, name=name, dry_run=dry_run
        )
    except (YTMusicError, ListenBrainzAuthError) as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return RadioOut.of(result)


@app.post("/sync")
def sync() -> dict:
    try:
        counts = Engine().sync_library()
    except YTMusicError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    return {"synced": counts}


@app.post("/prefetch")
def prefetch(limit: int = Body(100, embed=True)) -> dict:
    return {"prefetched": Engine().prefetch_features(limit)}
