# Merlin

A hybrid **"Start Radio"** / **"Similar Tracks Playlist"** engine for YouTube Music.

Merlin fuses several open-ecosystem signals — ListenBrainz session-based
co-occurrence, Last.fm collaborative data, AcousticBrainz audio features, and
YouTube Music's own watch-radio — into a single ranked playlist, then writes it
back to your YTM account. MusicBrainz IDs are the spine that ties the services
together.

> Spotify's `/audio-features` and `/recommendations` were deprecated on
> 27 Nov 2024 and AcousticBrainz froze submissions in June 2022 — Merlin is built
> for that post-deprecation reality and degrades gracefully where data is thin.

## Architecture

A long-running **FastAPI/uvicorn daemon** on `127.0.0.1:7654` plus a **Typer
CLI** that talks to it over localhost HTTP (and falls back to running in-process
when the daemon is down). A single **SQLite** file (with the `sqlite-vec`
extension for vector ANN) stores tokens, cross-service ID mappings, cached
feature vectors, similarity edges, and a library snapshot.

```
src/merlin/
  config.py          # pydantic-settings, paths, fusion weights
  db/                # SQLite + sqlite-vec: schema, access layer
  core/              # models, normalisation, the recommendation engine
  clients/           # ytmusic (more services land in later phases)
  service/app.py     # FastAPI daemon
  cli/main.py        # Typer + Rich CLI
```

## Status — build phases

- [x] **Phase 0** — Skeleton: config, SQLite+sqlite-vec, FastAPI `/status`,
      Typer CLI, ytmusicapi OAuth, and a working **YTM-native radio** end to end.
- [x] **Phase 1** — MBID resolver (ISRC-first MusicBrainz + fuzzy) and a
      ListenBrainz `lb-radio` passthrough. *(lb-radio needs `MERLIN_LISTENBRAINZ_TOKEN`
      — LB started requiring a token for reads in 2025.)*
- [ ] **Phase 2** — Multi-source candidates (Last.fm + LB labs + YTM watch) and
      source-normalised weighted fusion + MMR re-rank.
- [ ] **Phase 3** — AcousticBrainz audio features → cosine similarity in the fusion.
- [ ] **Phase 4** — Background sync (library/history/likes) via APScheduler.

## Setup

```bash
uv sync
cp .env.example .env   # fill in your keys
```

### YouTube Music auth (OAuth, required for writes)

1. In Google Cloud, create an OAuth client of type **"TVs and Limited Input
   devices"**. Put the id/secret in `.env`
   (`MERLIN_YTM_CLIENT_ID` / `MERLIN_YTM_CLIENT_SECRET`).
2. Run the device flow once:

   ```bash
   uv run merlin auth ytm
   ```

   This writes `oauth.json` into the data dir; the token auto-refreshes.

## Usage

```bash
# Inspect state
uv run merlin status

# Start a radio from a song (creates a private YTM playlist)
uv run merlin radio "Bohemian Rhapsody"
uv run merlin radio "https://music.youtube.com/watch?v=..." --size 30

# Preview without writing anything
uv run merlin radio "Teardrop — Massive Attack" --dry-run

# Similar-tracks playlist
uv run merlin similar --seed "Strobe — deadmau5" --size 50 --name "Like Strobe"

# Run the daemon (the CLI will then route through it automatically)
uv run merlin serve
```

## Being a good citizen

Merlin caches aggressively and rate-limits every external service
(MusicBrainz 1 req/s, AcousticBrainz ≤10/10s, Last.fm <5 req/s, YTM ~1 write/s
with backoff). The daemon binds to loopback only — **do not expose it to the
internet**. `ytmusicapi` is unofficial and not endorsed by Google; pin the
version and keep rates conservative.

## Development

```bash
uv run pytest        # tests
uv run ruff check    # lint
```
