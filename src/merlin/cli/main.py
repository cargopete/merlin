"""Merlin CLI — Typer + Rich.

Commands talk to the local daemon over HTTP when it's running, and fall back to
running the engine in-process when it isn't. Either way you get the same result.
"""

from __future__ import annotations

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
        data = {
            "data_dir": str(s.data_dir),
            "auth": {
                "ytm": YTMusicClient(s).is_authenticated(),
                "lastfm": bool(s.lastfm_api_key),
                "listenbrainz": bool(s.listenbrainz_token),
            },
            "db": get_db(s).stats(),
        }
        source = "local"

    console.print(f"[bold]Merlin status[/bold] ([dim]{source}[/dim])")
    console.print(f"  data dir: {data['data_dir']}")
    auth = data["auth"]
    for svc, ok in auth.items():
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"  {mark} {svc}")
    console.print("  cache:")
    for table, n in data["db"].items():
        console.print(f"    {table}: {n}")


@app.command()
def auth(
    service: Annotated[str, typer.Argument(help="ytm | lastfm | listenbrainz")],
    open_browser: Annotated[bool, typer.Option(help="Open the OAuth URL")] = False,
) -> None:
    """Authenticate a backend service."""
    service = service.lower()
    if service in ("ytm", "ytmusic", "youtube"):
        from merlin.clients.ytmusic import YTMusicClient, YTMusicError

        try:
            console.print("Starting YouTube Music OAuth device flow…")
            YTMusicClient().setup_oauth(open_browser=open_browser)
            console.print("[green]✓ oauth.json written.[/green]")
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

        data = RadioOut.of(Engine().build_radio(query, size=size, dry_run=dry_run)).model_dump()
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

        data = RadioOut.of(
            Engine().build_similar(seed, size=size, name=name, dry_run=dry_run)
        ).model_dump()
    _render_radio(data)


if __name__ == "__main__":
    app()
