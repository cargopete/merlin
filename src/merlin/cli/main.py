"""Merlin CLI — Typer + Rich.

Commands talk to the local daemon over HTTP when it's running, and fall back to
running the engine in-process when it isn't. Either way you get the same result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from merlin.config import get_settings

app = typer.Typer(
    name="merlin",
    help="Hybrid 'Start Radio' / 'Similar Tracks' engine for YouTube Music.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _daemon_up() -> bool:
    import httpx

    settings = get_settings()
    try:
        r = httpx.get(f"{settings.base_url}/status", timeout=1.5)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    settings = get_settings()
    r = httpx.post(f"{settings.base_url}{path}", json=payload, timeout=120)
    if r.status_code >= 400:
        is_json = r.headers.get("content-type", "").startswith("application/json")
        detail = r.json().get("detail", r.text) if is_json else r.text
        raise typer.BadParameter(detail)
    return r.json()


def _local(fn):
    """Run an in-process engine call, turning known failures into clean exits."""
    from merlin.clients.listenbrainz import ListenBrainzAuthError
    from merlin.clients.ytmusic import YTMusicError

    try:
        return fn()
    except (YTMusicError, ListenBrainzAuthError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


def _render_radio(data: dict[str, Any]) -> None:
    seed = data["seed"]
    table = Table(title=f"Seed: {seed['title']} — {', '.join(seed['artists']) or '?'}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title")
    table.add_column("Artists", style="cyan")
    table.add_column("Album", style="dim")
    for i, t in enumerate(data["tracks"], 1):
        table.add_row(str(i), t["title"], ", ".join(t["artists"]), t.get("album") or "")
    console.print(table)
    if data.get("playlist_url"):
        console.print(f"\n[green]Playlist:[/green] {data['playlist_url']}")
    else:
        console.print("\n[yellow]Dry run — no playlist written.[/yellow]")


# --- commands ----------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind host")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload (dev)")] = False,
) -> None:
    """Run the Merlin daemon (FastAPI + uvicorn)."""
    import uvicorn

    settings = get_settings()
    console.print(
        f"[bold]Merlin[/bold] daemon → http://{host or settings.host}:{port or settings.port}"
    )
    uvicorn.run(
        "merlin.service.app:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )


@app.command()
def status() -> None:
    """Show auth state, storage location and cache counts."""
    if _daemon_up():
        import httpx

        data = httpx.get(f"{get_settings().base_url}/status", timeout=5).json()
        source = "daemon"
    else:
        from merlin.clients.ytmusic import YTMusicClient
        from merlin.db.database import get_db

        s = get_settings()
        ytm = YTMusicClient(s)
        data = {
            "data_dir": str(s.data_dir),
            "auth": {
                "ytm": ytm.is_authenticated(),
                "lastfm": bool(s.lastfm_api_key),
                "listenbrainz": bool(s.listenbrainz_token),
            },
            "ytm_method": ytm.auth_method(),
            "db": get_db(s).stats(),
        }
        source = "local"

    console.print(f"[bold]Merlin status[/bold] ([dim]{source}[/dim])")
    console.print(f"  data dir: {data['data_dir']}")
    auth = data["auth"]
    for svc, ok in auth.items():
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        suffix = ""
        if svc == "ytm" and ok and data.get("ytm_method"):
            suffix = f" [dim]({data['ytm_method']})[/dim]"
        console.print(f"  {mark} {svc}{suffix}")
    console.print("  cache:")
    for table, n in data["db"].items():
        console.print(f"    {table}: {n}")


def _browser_auth_ytm() -> None:
    """Interactive browser-headers setup — no Google Cloud required."""
    import sys

    from merlin.clients.ytmusic import YTMusicClient

    console.print("[bold]YouTube Music — browser-headers auth[/bold]")
    console.print(
        "1. Open [cyan]https://music.youtube.com[/cyan] in your browser (logged in).\n"
        "2. Open DevTools → [cyan]Network[/cyan] tab, filter for [cyan]/browse[/cyan].\n"
        "3. Click a [cyan]POST[/cyan] request to music.youtube.com/...browse.\n"
        "4. Copy the [cyan]request headers[/cyan] (in Firefox: right-click → Copy → "
        "Copy Request Headers; in Chrome: copy the raw headers block).\n"
        "5. Paste them below, then press [cyan]Ctrl-D[/cyan] (Ctrl-Z then Enter on Windows).\n"
        "\n[dim]Tip: hate the Ctrl-D dance? Save the headers to a file and run\n"
        "      merlin auth ytm --from-file headers.txt[/dim]\n"
    )
    console.print("[dim]Paste headers now:[/dim]")
    headers_raw = sys.stdin.read().strip()
    if not headers_raw:
        console.print("[red]No headers pasted — aborted.[/red]")
        raise typer.Exit(1)
    YTMusicClient().setup_browser(headers_raw=headers_raw)
    console.print("[green]✓ browser.json written.[/green]")


@app.command()
def auth(
    service: Annotated[str, typer.Argument(help="ytm | lastfm | listenbrainz")],
    oauth: Annotated[bool, typer.Option("--oauth", help="Use OAuth, not browser headers")] = False,
    from_file: Annotated[
        Path | None,
        typer.Option("--from-file", help="Read browser headers from a file (no pasting)"),
    ] = None,
    open_browser: Annotated[bool, typer.Option(help="Open the OAuth URL (with --oauth)")] = False,
) -> None:
    """Authenticate a backend service."""
    service = service.lower()
    if service in ("ytm", "ytmusic", "youtube"):
        from merlin.clients.ytmusic import YTMusicClient, YTMusicError

        try:
            if oauth:
                console.print("Starting YouTube Music OAuth device flow…")
                YTMusicClient().setup_oauth(open_browser=open_browser)
                console.print("[green]✓ oauth.json written.[/green]")
            elif from_file:
                raw = from_file.read_text()
                if not raw.strip():
                    console.print(f"[red]{from_file} is empty.[/red]")
                    raise typer.Exit(1)
                YTMusicClient().setup_browser(headers_raw=raw)
                console.print(f"[green]✓ browser.json written[/green] (from {from_file})")
            else:
                _browser_auth_ytm()
        except YTMusicError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
    elif service in ("lastfm", "listenbrainz"):
        console.print(
            f"[yellow]{service}[/yellow] uses a static key/token. Set it via env:"
        )
        if service == "lastfm":
            console.print("  export MERLIN_LASTFM_API_KEY=…")
        else:
            console.print("  export MERLIN_LISTENBRAINZ_TOKEN=…")
    else:
        raise typer.BadParameter(f"unknown service: {service}")


@app.command()
def radio(
    query: Annotated[str, typer.Argument(help="Song title, query, or YTM URL")],
    size: Annotated[int, typer.Option(help="Number of tracks")] = 50,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Don't write a playlist")] = False,
) -> None:
    """Start a radio from a song."""
    if _daemon_up():
        data = _post("/radio", {"query": query, "size": size, "dry_run": dry_run})
    else:
        from merlin.core.engine import Engine
        from merlin.service.app import RadioOut

        data = _local(
            lambda: RadioOut.of(
                Engine().build_radio(query, size=size, dry_run=dry_run)
            ).model_dump()
        )
    _render_radio(data)


@app.command(name="lb-radio")
def lb_radio(
    prompt: Annotated[str, typer.Argument(help="lb-radio prompt, e.g. artist:(Radiohead)")],
    mode: Annotated[str, typer.Option(help="easy | medium | hard")] = "medium",
    size: Annotated[int, typer.Option(help="Number of tracks")] = 50,
    name: Annotated[str | None, typer.Option(help="Playlist name")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Don't write a playlist")] = False,
) -> None:
    """Generate a playlist from a ListenBrainz lb-radio prompt."""
    if _daemon_up():
        data = _post(
            "/lb-radio",
            {"prompt": prompt, "mode": mode, "size": size, "name": name, "dry_run": dry_run},
        )
    else:
        from merlin.core.engine import Engine
        from merlin.service.app import RadioOut

        data = _local(
            lambda: RadioOut.of(
                Engine().build_lb_radio(
                    prompt, mode=mode, size=size, name=name, dry_run=dry_run
                )
            ).model_dump()
        )
    _render_radio(data)


@app.command()
def similar(
    seed: Annotated[str, typer.Option(help="Song/query/URL to seed from")],
    size: Annotated[int, typer.Option(help="Number of tracks")] = 50,
    name: Annotated[str | None, typer.Option(help="Playlist name")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Don't write a playlist")] = False,
) -> None:
    """Create a playlist of tracks similar to a song."""
    if _daemon_up():
        data = _post(
            "/similar-playlist",
            {"seed": seed, "size": size, "name": name, "dry_run": dry_run},
        )
    else:
        from merlin.core.engine import Engine
        from merlin.service.app import RadioOut

        data = _local(
            lambda: RadioOut.of(
                Engine().build_similar(seed, size=size, name=name, dry_run=dry_run)
            ).model_dump()
        )
    _render_radio(data)


@app.command()
def sync() -> None:
    """Mirror your YTM library, likes and history into the local cache."""
    if _daemon_up():
        data = _post("/sync", {})
        counts = data["synced"]
    else:
        from merlin.core.engine import Engine

        with console.status("Syncing library from YouTube Music…"):
            counts = _local(Engine().sync_library)
    console.print("[green]✓ synced[/green]")
    for k, v in counts.items():
        console.print(f"  {k}: {v}")


if __name__ == "__main__":
    app()
