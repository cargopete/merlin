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
- [x] **Phase 2** — Multi-source candidates (Last.fm + LB labs `similar-recordings`
      + YTM watch), max-normalised weighted fusion + cross-source agreement + MMR
      re-rank. *(Last.fm needs `MERLIN_LASTFM_API_KEY`; LB labs needs no auth.)*
- [x] **Phase 3** — AcousticBrainz low+high-level → 35-dim feature vectors (bulk,
      cached in `sqlite-vec`) → cosine audio similarity fused at `w_audio=0.4`,
      also driving MMR diversity. Degrades gracefully where AB has no coverage.
- [x] **Phase 4** — Background sync via APScheduler (nightly library/likes/history
      mirror + 6-hourly AcousticBrainz cache warm with negative caching), plus
      `merlin sync` / `POST /sync` and `POST /prefetch`.

## Setup

```bash
uv sync
cp .env.example .env   # fill in your keys
```

### YouTube Music auth (required for everything)

Two options — browser headers (no Google Cloud) or OAuth.

#### Option 1 — Browser headers (default, no Google Cloud)

```bash
uv run merlin auth ytm
```

Then, when prompted to paste headers:

1. Open **[music.youtube.com](https://music.youtube.com)** in your browser, logged in.
2. Open DevTools (`Cmd/Ctrl-Opt-I`) → **Network** tab.
3. Type `browse` in the filter box, then click around the app so a request appears.
4. Click a **POST** request to `…/youtubei/v1/browse`.
5. Copy its **request headers**:
   - **Firefox:** right-click the request → Copy → **Copy Request Headers**
   - **Chrome/Edge:** in the Headers tab, find *Request Headers* and copy the block
     (the two-column "name / value" copy works too — Merlin normalises it)
6. Paste into the terminal, then press **Ctrl-D** (Windows: Ctrl-Z then Enter).

**Prefer not to paste into a prompt?** Save the headers to a file and point Merlin
at it (easier, and avoids the Ctrl-D faff):

```bash
# paste the headers into headers.txt with any editor, then:
uv run merlin auth ytm --from-file headers.txt
rm headers.txt   # delete it afterwards — it holds your session cookies
```

Either way writes `browser.json` (valid ~2 years, until you log out). No Google
Cloud, and it can also upload.

> **Security:** those headers contain your session cookies. Paste them only into
> your own terminal — never into a chat, issue, or paste-bin. If they leak, sign
> out of YouTube and re-auth to rotate them.

#### Option 2 — OAuth device flow

1. In Google Cloud, create an OAuth client of type **"TVs and Limited Input
   devices"** and put the id/secret in `.env`
   (`MERLIN_YTM_CLIENT_ID` / `MERLIN_YTM_CLIENT_SECRET`).
2. `uv run merlin auth ytm --oauth` — prints a code to enter at `google.com/device`;
   approve on any device. Writes `oauth.json`; the token auto-refreshes.

If both `browser.json` and `oauth.json` exist, browser headers take precedence.
Check which is active with `merlin status` (e.g. `✓ ytm (browser)`).

### Optional similarity sources

These are optional — Merlin degrades gracefully without them, but each one adds a
signal to the fusion. Set them in `.env`:

| Source | Env var | Where to get it |
|---|---|---|
| **Last.fm** | `MERLIN_LASTFM_API_KEY` | [last.fm/api/account/create](https://www.last.fm/api/account/create) |
| **ListenBrainz** | `MERLIN_LISTENBRAINZ_TOKEN` | [listenbrainz.org/settings](https://listenbrainz.org/settings/) (needed for `lb-radio`) |
| **MusicBrainz** | `MERLIN_CONTACT_EMAIL` | your email — MusicBrainz blocks anonymous clients |

ListenBrainz `similar-recordings`, MusicBrainz and AcousticBrainz need no key
(just a contact email for MusicBrainz). YouTube Music's own watch-radio works as
soon as you're authenticated above.

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

# Playlist from a ListenBrainz lb-radio prompt (needs MERLIN_LISTENBRAINZ_TOKEN)
uv run merlin lb-radio "artist:(Radiohead)" --mode medium

# Mirror your library/likes/history locally (also runs nightly in the daemon)
uv run merlin sync

# Run the daemon (the CLI will then route through it automatically; runs the
# nightly library sync + 6-hourly AcousticBrainz cache-warm jobs)
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
