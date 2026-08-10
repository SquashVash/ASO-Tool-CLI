"""Typer entrypoint.

    aso init
    aso check "meditation timer"          # score it without tracking it
    aso add "candlestick patterns" --country us --tag lcp
    aso import keywords.csv
    aso refresh --tag lcp --country us
    aso list --sort opportunity --limit 30
    aso show "candlestick patterns"
    aso track --track-id 627114159
    aso export --format csv

Presentation only: no scoring or SQL lives here.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, importers, pipeline, repository
from .clients import apple_popularity, asa
from .clients.charts import ChartsClient
from .clients.hints import HintsClient
from .clients.itunes import ITunesClient
from .config import (
    ASA_DEFAULT_LOOKBACK_DAYS,
    ASA_RATE_LIMIT_PER_MIN,
    COMPETITION_BRIDGE_SOURCE,
    COMPETITION_WEIGHTS,
    SEARCH_DEPTH_WEIGHT,
    SEARCH_EXTENSIONS_WEIGHT,
    settings,
)
from .db import init_db, session, transaction
from .http import Fetcher
from .repository import UnknownKeyword
from .scoring import bridge as bridge_scoring
from .scoring import competition
from .scoring import search as search_scoring

def _force_utf8_output() -> None:
    """Make stdout/stderr able to print any keyword we might have stored.

    Windows consoles default to a legacy code page (cp1252 here), which cannot
    encode a great many perfectly valid App Store keywords. Printing one raises
    UnicodeEncodeError from deep inside the codec — and because that happens in
    the *display* layer, not the fetch layer, `pipeline`'s "failures are
    recorded, not fatal" guarantee does not cover it: a single Japanese keyword
    killed an entire 56-keyword refresh run mid-flight.

    `errors="replace"` rather than strict, because a mangled character in a
    progress line is always preferable to losing the run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - detached stream
                pass


_force_utf8_output()

app = typer.Typer(
    help="ASO keyword research for the iOS App Store.",
    no_args_is_help=True,
    add_completion=False,
)
asa_app = typer.Typer(
    help="Apple Search Ads: verify credentials and pull measured impressions.",
    no_args_is_help=True,
)
app.add_typer(asa_app, name="asa")
console = Console()
err_console = Console(stderr=True)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=err_console, show_path=False, rich_tracebacks=True)],
    )
    # httpx logs every request's full URL at INFO. Our own logging redacts
    # credentials; httpx's does not, so `--verbose` would otherwise print any
    # query-string secret verbatim. Belt and braces alongside sending ASA
    # credentials in the request body — see clients/asa.py.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def fmt(value: float | None, places: int = 1) -> str:
    return "[dim]—[/dim]" if value is None else f"{value:.{places}f}"


def fmt_int(value: int | None) -> str:
    return "[dim]—[/dim]" if value is None else f"{value:,}"


def opportunity_style(value: float | None) -> str:
    """Colour the default sort so a long table is scannable."""
    if value is None:
        return "dim"
    if value >= 50:
        return "bold green"
    if value >= 25:
        return "yellow"
    return "dim"


# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Create the database and apply any pending migrations."""
    applied = init_db()
    if applied:
        console.print(f"[green]Applied migrations:[/green] {', '.join(map(str, applied))}")
    else:
        console.print("[dim]Schema already up to date.[/dim]")
    console.print(f"[dim]Database:[/dim] {settings.db_path}")


@app.command()
def version() -> None:
    """Print the tool version."""
    console.print(f"aso {__version__}")


@app.command()
def serve(
    host: str = typer.Option(settings.api_host, "--host"),
    port: int = typer.Option(settings.api_port, "--port"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the HTTP API.

    Deliberately no --workers flag. Two workers would mean two token buckets
    against a per-IP rate limit, and two job registries behind one URL.
    """
    import uvicorn

    setup_logging(verbose)
    console.print(f"[green]aso api[/green] http://{host}:{port}")
    uvicorn.run(
        "aso.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=1,
        log_level="info" if verbose else "warning",
    )


@app.command()
def add(
    keyword: str = typer.Argument(..., help="The keyword or phrase to track."),
    country: str = typer.Option(settings.default_country, "--country", "-c"),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Repeatable."),
) -> None:
    """Track a keyword in a storefront."""
    with session() as conn:
        init_db()
        try:
            keyword_id, created = repository.add_keyword(conn, keyword, country, tag)
        except ValueError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        row = repository.require_keyword(conn, keyword, country)

    verb = "Added" if created else "Already tracked (tags merged)"
    tags = row["tags"] or "[dim]none[/dim]"
    console.print(f"[green]{verb}[/green] {row['keyword']!r} ({row['country']}) #{keyword_id}")
    console.print(f"  tags: {tags}")
    if created:
        console.print(f"[dim]Run `aso refresh --country {row['country']}` to score it.[/dim]")


@app.command(name="import")
def import_keywords(
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV file."),
    country: Optional[str] = typer.Option(
        None, "--country", "-c", help="Default for rows with no country column."
    ),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Applied to every row."),
) -> None:
    """Import keywords from a CSV with a `keyword` column.

    Optional columns: `country`, `tags` (semicolon- or comma-separated).
    Re-importing is safe — existing keywords have their tags merged.
    """
    default_country = (country or settings.default_country).lower()
    added = merged = 0
    skipped: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "keyword" not in {
            (name or "").strip().lower() for name in reader.fieldnames
        }:
            err_console.print(
                f"[red]{path} needs a header row with a 'keyword' column.[/red]\n"
                f"[dim]Found: {reader.fieldnames}[/dim]"
            )
            raise typer.Exit(1)

        with session() as conn:
            init_db()
            for line_number, raw in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
                keyword = row.get("keyword", "")
                if not keyword:
                    skipped.append(f"line {line_number}: empty keyword")
                    continue
                tags = list(tag) + [
                    part for part in row.get("tags", "").replace(";", ",").split(",") if part
                ]
                try:
                    _, created = repository.add_keyword(
                        conn, keyword, row.get("country") or default_country, tags
                    )
                except ValueError as exc:
                    skipped.append(f"line {line_number}: {exc}")
                    continue
                added += created
                merged += not created

    console.print(f"[green]Imported[/green] {added} new, {merged} already tracked.")
    for problem in skipped:
        err_console.print(f"[yellow]skipped[/yellow] {problem}")


@app.command()
def refresh(
    tag: Optional[str] = typer.Option(None, "--tag", "-t"),
    country: Optional[str] = typer.Option(None, "--country", "-c"),
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k", help="Just this one."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    force: bool = typer.Option(False, "--force", help="Ignore cached responses."),
    include_inactive: bool = typer.Option(False, "--include-inactive"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log every request."),
) -> None:
    """Fetch and score keywords, writing a snapshot for each.

    Safe to interrupt: each keyword commits on its own and every response is
    cached on disk, so restarting only re-does the keyword that was in flight.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        if keyword:
            try:
                rows = [repository.require_keyword(conn, keyword, country or settings.default_country)]
            except UnknownKeyword as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1)
        else:
            rows = repository.list_keywords(
                conn, tag=tag, country=country, active_only=not include_inactive
            )
        if limit is not None:
            rows = rows[:limit]

        if not rows:
            console.print("[yellow]No keywords match that filter.[/yellow]")
            console.print("[dim]Add one with `aso add \"your keyword\"`.[/dim]")
            raise typer.Exit(0)

        estimate = len(rows) * 60.0 / max(settings.rate_limit_per_min, 1) * 4
        console.print(
            f"Refreshing [bold]{len(rows)}[/bold] keyword(s) at "
            f"{settings.rate_limit_per_min} req/min. "
            f"[dim]Cold, expect roughly {estimate / 60:.0f} min. Ctrl-C is safe.[/dim]\n"
        )

        report = None
        try:
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("scoring", total=len(rows))

                def advance(outcome: pipeline.KeywordOutcome) -> None:
                    marker = {"ok": "[green]ok[/green]", "partial": "[yellow]partial[/yellow]"}.get(
                        outcome.status, "[red]failed[/red]"
                    )
                    progress.update(
                        task,
                        advance=1,
                        description=f"{outcome.keyword[:28]:28s} {marker}",
                    )

                report = asyncio.run(
                    pipeline.refresh(conn, rows, force=force, on_progress=advance)
                )
        except KeyboardInterrupt:
            console.print(
                "\n[yellow]Interrupted.[/yellow] "
                "[dim]Completed keywords are saved; rerun to continue.[/dim]"
            )
            raise typer.Exit(130)

        console.print(
            f"\n[green]{report.succeeded} scored[/green]"
            + (f", [red]{report.failed} with errors[/red]" if report.failed else "")
            + f" — {report.requests_made} request(s), "
            f"{report.retries} retr{'y' if report.retries == 1 else 'ies'}, "
            f"{report.duration_seconds / 60:.1f} min"
        )
        for outcome in report.outcomes:
            if outcome.failed:
                err_console.print(
                    f"[yellow]{outcome.status}[/yellow] {outcome.keyword!r} "
                    f"({outcome.country}): {outcome.error}"
                )


@app.command()
def check(
    keyword: str = typer.Argument(..., help="Keyword to score, ad hoc."),
    country: str = typer.Option(settings.default_country, "--country", "-c"),
    force: bool = typer.Option(False, "--force", help="Ignore cached responses."),
    as_json: bool = typer.Option(False, "--json", help="Emit the scores as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log every request."),
) -> None:
    """Score a keyword without tracking it. Writes nothing to your keyword list.

    For answering "is this term worth tracking?" before committing to it. Runs
    exactly the same scoring code as `refresh`, but stores no keyword, no
    snapshot and no SERP — so it leaves no trend history and cannot be compared
    over time. Use `aso add` for anything you want to watch.

    It does build the chart index, which costs up to 48 requests the first time
    it runs in a storefront on a given day and nothing thereafter. That is a
    steep-looking bill for scoring one keyword, and the alternative is worse:
    `comp_app_power` is the heaviest term in the model, so a `check` that
    skipped it would print a materially different competition score from the
    `refresh` of the same keyword — measured at 35.3 against 28.5 on the test
    fixture. Two commands documented as running the same scoring code must not
    disagree.
    """
    setup_logging(verbose)
    if not keyword.strip():
        err_console.print("[red]Keyword cannot be blank.[/red]")
        raise typer.Exit(1)

    async def main():
        async with Fetcher(settings) as fetcher:
            with session() as conn:
                init_db()
                itunes = ITunesClient(fetcher, conn, settings)
                hints = HintsClient(fetcher, conn, settings)
                charts_client = ChartsClient(fetcher, conn, settings)
                # `force` is not passed on: it means "refetch this keyword",
                # and the index is a property of the storefront and the day.
                charts = await charts_client.index(country.lower())
                scored = await pipeline.score_keyword(
                    keyword,
                    country.lower(),
                    itunes=itunes,
                    hints=hints,
                    force=force,
                    charts=charts,
                )
                # Report the same numbers a tracked keyword would, on both
                # axes. See `pipeline.blend_outcome` and `bridge_outcome` —
                # `score_keyword` reads no table, so neither the measured
                # demand value nor the fitted competition scale reaches it.
                pipeline.bridge_outcome(conn, scored.outcome)
                pipeline.blend_outcome(conn, scored.outcome)
            return scored, fetcher.requests_made

    scored, requests = asyncio.run(main())
    outcome = scored.outcome

    if as_json:
        console.print_json(
            data={
                "keyword": keyword,
                "country": country.lower(),
                "search_score": outcome.search_score,
                "search_source": outcome.search_source,
                "search_score_proxy": outcome.search_score_proxy,
                "competition_score": outcome.competition_score,
                "competition_score_raw": outcome.competition_score_raw,
                "opportunity_score": outcome.opportunity_score,
                "search_prefix_depth": outcome.prefix_depth,
                "search_hint_rank": outcome.hint_rank,
                "search_hint_extensions": outcome.hint_extensions,
                "serp_size": outcome.serp_size,
                "tracked": False,
                **scored.components,
            }
        )
        return

    header = Table.grid(padding=(0, 2))
    header.add_row("[bold]keyword[/bold]", f"{keyword} [dim]({country.lower()})[/dim]")
    console.print(header)

    scores = Table(show_header=True, header_style="bold")
    scores.add_column("score")
    scores.add_column("value", justify="right")
    scores.add_row("search", fmt(outcome.search_score))
    scores.add_row("competition", fmt(outcome.competition_score))
    scores.add_row(
        "opportunity",
        f"[{opportunity_style(outcome.opportunity_score)}]"
        f"{fmt(outcome.opportunity_score)}[/]",
    )
    console.print(scores)

    detail = Table(show_header=True, header_style="bold")
    detail.add_column("component")
    detail.add_column("value", justify="right")
    for name, value in scored.components.items():
        detail.add_row(name, fmt(value))
    detail.add_row("search_prefix_depth", fmt_int(outcome.prefix_depth))
    detail.add_row("search_hint_rank", fmt_int(outcome.hint_rank))
    detail.add_row("search_hint_extensions", fmt_int(outcome.hint_extensions))
    console.print(detail)

    if scored.serp is not None and getattr(scored.serp, "apps", None):
        top = Table(title="Top 10 (NOT App Store rank)", header_style="bold")
        top.add_column("#", justify="right")
        top.add_column("app")
        top.add_column("seller")
        top.add_column("ratings", justify="right")
        for position, app in enumerate(scored.serp.top(10), start=1):
            top.add_row(
                str(position),
                app.track_name or "",
                app.seller_name or "",
                fmt_int(app.user_rating_count),
            )
        console.print(top)

    if outcome.failed:
        err_console.print(f"[yellow]{outcome.status}[/yellow]: {outcome.error}")

    console.print(
        f"[dim]{requests} request(s). Not tracked — no snapshot written. "
        f"`aso add {keyword!r} --country {country.lower()}` to start a history.[/dim]"
    )


@app.command()
def rescore(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Recompute every stored score from its saved components. No network access.

    Run this after changing COMPETITION_WEIGHTS or the search mapping, so trend
    history is scored consistently instead of splicing two schemes together.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        report = pipeline.rescore(conn)

    if report.total == 0:
        console.print("[yellow]No snapshots to re-score.[/yellow]")
        return

    console.print(
        f"[green]Re-scored {report.total} snapshot(s)[/green], "
        f"{report.changed} changed."
    )
    if report.largest_move_keyword is not None:
        console.print(
            f"[dim]Largest opportunity move: {report.largest_move:.1f} points "
            f"({report.largest_move_keyword}).[/dim]"
        )


@app.command(name="list")
def list_keywords(
    sort: str = typer.Option("opportunity", "--sort", "-s", help="opportunity|search|competition|keyword|captured"),
    limit: int = typer.Option(30, "--limit", "-n"),
    tag: Optional[str] = typer.Option(None, "--tag", "-t"),
    country: Optional[str] = typer.Option(None, "--country", "-c"),
    include_inactive: bool = typer.Option(False, "--include-inactive"),
) -> None:
    """Show tracked keywords with their latest scores."""
    with session() as conn:
        init_db()
        try:
            rows = repository.latest_scores(
                conn, tag=tag, country=country, sort=sort, limit=limit,
                active_only=not include_inactive,
            )
        except ValueError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

    if not rows:
        console.print("[yellow]No keywords tracked yet.[/yellow]")
        console.print('[dim]Start with `aso add "your keyword"`.[/dim]')
        return

    table = Table(title=f"keywords by {sort}", title_style="bold")
    table.add_column("keyword")
    table.add_column("cc", width=3)
    table.add_column("tags", style="dim")
    table.add_column("search", justify="right")
    table.add_column("comp", justify="right")
    table.add_column("opp", justify="right")
    table.add_column("captured", style="dim")

    unscored = 0
    for row in rows:
        if row["captured_at"] is None:
            unscored += 1
        flag = " [red]![/red]" if row["fetch_failed"] else ""
        opp = row["opportunity_score"]
        table.add_row(
            row["keyword"] + flag,
            row["country"],
            row["tags"] or "",
            fmt(row["search_score"]),
            fmt(row["competition_score"]),
            f"[{opportunity_style(opp)}]{fmt(opp)}[/{opportunity_style(opp)}]",
            (row["captured_at"] or "never")[:10],
        )
    console.print(table)
    console.print(
        "[dim]opp = search × (100 − comp) / 100. "
        "search is an uncalibrated proxy — read it as ordinal.[/dim]"
    )
    if unscored:
        console.print(f"[dim]{unscored} keyword(s) never refreshed.[/dim]")


@app.command()
def show(
    keyword: str = typer.Argument(...),
    country: str = typer.Option(settings.default_country, "--country", "-c"),
    history: int = typer.Option(10, "--history", "-h", help="Snapshots to chart."),
) -> None:
    """Detail for one keyword: components, trend, and the current top 10."""
    with session() as conn:
        init_db()
        try:
            row = repository.require_keyword(conn, keyword, country)
        except UnknownKeyword as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        latest = repository.latest_snapshot(conn, row["id"])
        trend = repository.snapshot_history(conn, row["id"], limit=history)
        serp = repository.latest_serp(conn, row["id"], limit=10)

    console.print(f"\n[bold]{row['keyword']}[/bold] ({row['country']})")
    console.print(f"[dim]tags: {row['tags'] or 'none'} · tracking since {row['created_at'][:10]}[/dim]\n")

    if latest is None:
        console.print("[yellow]Never refreshed.[/yellow]")
        console.print(f"[dim]Run `aso refresh -k \"{row['keyword']}\" -c {row['country']}`.[/dim]")
        return

    if latest["fetch_failed"]:
        console.print(f"[red]Last refresh had errors:[/red] {latest['fetch_error']}\n")

    scores = Table("score", "value", box=None)
    scores.add_row("search", fmt(latest["search_score"]))
    scores.add_row("competition", fmt(latest["competition_score"]))
    scores.add_row("[bold]opportunity[/bold]", f"[bold]{fmt(latest['opportunity_score'])}[/bold]")
    console.print(scores)

    demand = Table("demand component", "weight", "value", title="what drives search")
    demand.add_row(
        "prefix depth",
        f"{SEARCH_DEPTH_WEIGHT:.2f}",
        "no match" if latest["search_prefix_depth"] is None
        else str(latest["search_prefix_depth"]),
    )
    demand.add_row(
        "extensions",
        f"{SEARCH_EXTENSIONS_WEIGHT:.2f}",
        fmt_int(latest["search_hint_extensions"]),
    )
    console.print(demand)

    components = Table("competition component", "weight", "value", title="components")
    # Zero-weighted rows are shown rather than hidden: `rating_count` and
    # `stars` are measured and stored, and a reader comparing two keywords
    # needs to see the numbers that produced `incumbent`. The label says why
    # they carry no weight of their own, so a 0.00 does not read as dead code.
    for name, weight in COMPETITION_WEIGHTS.items():
        label = name.removeprefix("comp_")
        if weight == 0 and name in ("comp_rating_count", "comp_stars"):
            label += " (feeds incumbent)"
        elif weight == 0 and name == "comp_publisher":
            label += " (uplift, not averaged)"
        components.add_row(label, f"{weight:.2f}", fmt(latest[name]))
    console.print(components)
    depth = latest["search_prefix_depth"]
    extensions = latest["search_hint_extensions"]
    console.print(
        "[dim]"
        + (
            "never surfaced in its own autocomplete"
            if depth is None
            else f"prefix depth {depth}, hint rank {latest['search_hint_rank']}"
        )
        + ("" if extensions is None else f"; {extensions} suggestion(s) extend it")
        + f" (keyword is {len(row['keyword'])} chars)[/dim]\n"
    )

    if len(trend) > 1:
        chart = Table("captured", "search", "comp", "opp", title="trend")
        for snap in trend:
            chart.add_row(
                snap["captured_at"][:10],
                fmt(snap["search_score"]),
                fmt(snap["competition_score"]),
                fmt(snap["opportunity_score"]),
            )
        console.print(chart)
    else:
        console.print("[dim]Only one snapshot so far — no trend yet.[/dim]\n")

    if serp:
        results = Table(
            "#", "app", "seller", "ratings", "stars",
            title=f"top 10 — iTunes Search order (NOT App Store rank) · {serp[0]['captured_at'][:10]}",
        )
        for entry in serp:
            results.add_row(
                str(entry["rank"]),
                entry["track_name"] or f"[dim]#{entry['track_id']}[/dim]",
                entry["seller_name"] or "[dim]—[/dim]",
                fmt_int(entry["user_rating_count"]),
                fmt(entry["average_user_rating"], 2),
            )
        console.print(results)


@app.command()
def track(
    track_id: int = typer.Option(..., "--track-id", help="Your app's iTunes track id."),
    country: Optional[str] = typer.Option(None, "--country", "-c"),
) -> None:
    """Where one of your apps sits across every tracked keyword."""
    with session() as conn:
        init_db()
        rows = repository.track_positions(conn, track_id, country=country)

    if not rows:
        console.print(f"[yellow]App {track_id} doesn't appear in any stored ranking.[/yellow]")
        console.print("[dim]Run `aso refresh` first, or check the track id.[/dim]")
        return

    table = Table("keyword", "cc", "rank", "was", "move", title=f"app {track_id}")
    for row in rows:
        current, previous = row["rank"], row["previous_rank"]
        if current is None:
            move = "[red]dropped out[/red]"
        elif previous is None:
            move = "[dim]new[/dim]"
        elif previous > current:
            move = f"[green]▲ {previous - current}[/green]"
        elif previous < current:
            move = f"[red]▼ {current - previous}[/red]"
        else:
            move = "[dim]—[/dim]"
        table.add_row(
            row["keyword"], row["country"],
            "[dim]—[/dim]" if current is None else str(current),
            "[dim]—[/dim]" if previous is None else str(previous),
            move,
        )
    console.print(table)
    console.print("[dim]Position in the iTunes Search result set, not App Store rank.[/dim]")


@app.command()
def export(
    format: str = typer.Option("csv", "--format", "-f", help="csv|json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Defaults to stdout."),
    tag: Optional[str] = typer.Option(None, "--tag", "-t"),
    country: Optional[str] = typer.Option(None, "--country", "-c"),
    sort: str = typer.Option("opportunity", "--sort", "-s"),
) -> None:
    """Export latest scores, including every component, to CSV or JSON."""
    if format not in {"csv", "json"}:
        err_console.print(f"[red]Unknown format {format!r}. Use csv or json.[/red]")
        raise typer.Exit(1)

    with session() as conn:
        init_db()
        try:
            rows = repository.latest_scores(conn, tag=tag, country=country, sort=sort)
        except ValueError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

    records = [dict(row) for row in rows]
    if output is not None:
        # `aso export -o exports/keywords.csv` should just work rather than
        # making the user create the directory first.
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            handle = output.open("w", newline="", encoding="utf-8")
        except OSError as exc:
            err_console.print(f"[red]Could not write {output}:[/red] {exc}")
            raise typer.Exit(1)
    else:
        handle = sys.stdout
    try:
        if format == "json":
            json.dump(records, handle, indent=2)
            handle.write("\n")
        else:
            # Components are exported alongside the finals so a spreadsheet can
            # re-weight them without touching this tool.
            columns = list(records[0]) if records else ["keyword", "country"]
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(records)
    finally:
        if output:
            handle.close()
    if output:
        console.print(f"[green]Wrote[/green] {len(records)} row(s) to {output}")


if __name__ == "__main__":
    app()


# ---------------------------------------------------------------------------
# Apple Search Ads
# ---------------------------------------------------------------------------


def asa_fetcher() -> Fetcher:
    """ASA gets its own Fetcher and its own quota — see config.ASA_RATE_LIMIT_PER_MIN."""
    import dataclasses

    return Fetcher(dataclasses.replace(settings, rate_limit_per_min=ASA_RATE_LIMIT_PER_MIN))


def handle_asa_errors(exc: Exception) -> None:
    """Print an ASA failure as advice, not a traceback, then exit non-zero."""
    err_console.print(f"[red]{exc}[/red]")
    raise typer.Exit(1)


@asa_app.command("whoami")
def asa_whoami(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Verify the credentials and list the orgs they can reach.

    The cheapest possible credential check — one token exchange and one GET.
    Run this before `pull`, and use it to find your ASO_ASA_ORG_ID.
    """
    setup_logging(verbose)

    async def main():
        async with asa_fetcher() as fetcher:
            return await asa.ASAClient(fetcher, settings).orgs()

    try:
        orgs = asyncio.run(main())
    except asa.ASAError as exc:
        handle_asa_errors(exc)

    if not orgs:
        console.print("[yellow]Authenticated, but no orgs were returned.[/yellow]")
        return

    table = Table(title="Apple Search Ads access", header_style="bold")
    for column in ("orgId", "name", "currency", "roles"):
        table.add_column(column)
    for org in orgs:
        configured = org.org_id == (settings.asa.org_id or "")
        table.add_row(
            f"[green]{org.org_id}[/green]" if configured else org.org_id,
            org.name,
            org.currency or "-",
            ", ".join(org.roles) or "-",
        )
    console.print(table)
    if settings.asa.org_id not in {o.org_id for o in orgs}:
        err_console.print(
            f"[yellow]ASO_ASA_ORG_ID is {settings.asa.org_id!r}, which is not in "
            "the list above. Pulls will fail until it matches.[/yellow]"
        )


@asa_app.command("campaigns")
def asa_campaigns(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """List campaigns in the configured org, and which ones can be calibrated against."""
    setup_logging(verbose)

    async def main():
        async with asa_fetcher() as fetcher:
            return await asa.ASAClient(fetcher, settings).campaigns()

    try:
        campaigns = asyncio.run(main())
    except asa.ASAError as exc:
        handle_asa_errors(exc)

    if not campaigns:
        console.print("[yellow]No campaigns in this org.[/yellow]")
        return

    table = Table(title="Campaigns", header_style="bold")
    for column in ("id", "name", "status", "country"):
        table.add_column(column)
    for campaign in campaigns:
        country = campaign.country
        table.add_row(
            str(campaign.campaign_id),
            campaign.name,
            campaign.status or "-",
            country
            or f"[yellow]{len(campaign.countries)} countries — not calibratable[/yellow]",
        )
    console.print(table)


@asa_app.command("pull")
def asa_pull(
    days: int = typer.Option(
        ASA_DEFAULT_LOOKBACK_DAYS, "--days", "-d", help="Lookback window in days."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Pull measured search-term impressions into the database.

    Stored permanently, not cached: these are observations to fit against, and
    re-pulling the same window updates in place rather than double-counting.
    """
    setup_logging(verbose)
    if days < 1:
        err_console.print("[red]--days must be at least 1.[/red]")
        raise typer.Exit(1)
    start, end = asa.default_window(lookback_days=days)

    async def main(conn):
        async with asa_fetcher() as fetcher:
            return await pipeline.pull_asa(
                conn, start=start, end=end, config=settings, fetcher=fetcher
            )

    with session() as conn:
        init_db()
        try:
            report = asyncio.run(main(conn))
        except asa.ASAError as exc:
            handle_asa_errors(exc)

    console.print(
        f"[green]{report.terms_written} search term(s)[/green] from "
        f"{report.campaigns_seen} campaign(s), {report.start} to {report.end} "
        f"— {report.requests_made} request(s)"
    )
    for skipped in report.campaigns_skipped:
        err_console.print(
            f"[yellow]stored without a country (excluded from calibration): "
            f"{skipped}[/yellow]"
        )
    if report.terms_written:
        console.print("[dim]Next: aso calibrate[/dim]")


@app.command(name="import-demand")
def import_demand(
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV export."),
    source: str = typer.Option(
        "appfigures", "--source", "-s", help="Export format: appfigures | impressions."
    ),
    country: str = typer.Option(
        settings.default_country, "--country", "-c", help="Storefront these rows describe."
    ),
    track: bool = typer.Option(
        False, "--track", help="Also start tracking every imported keyword."
    ),
    stratify: Optional[int] = typer.Option(
        None,
        "--stratify",
        "-n",
        help="Track only N keywords, spread evenly across the demand range.",
    ),
) -> None:
    """Import measured demand from a keyword export, for calibration.

    The storefront is a flag, not a column: these exports don't carry one
    (AppFigures puts it in the filename). Passing the wrong --country would
    silently calibrate against another market's demand.
    """
    try:
        result = importers.read_demand_csv(path, source=source, country=country)
    except importers.ImportError_ as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if not result.rows:
        err_console.print("[red]Nothing usable in that file.[/red]")
        raise typer.Exit(1)

    tracked = 0
    with session() as conn:
        init_db()
        with transaction(conn):
            written = repository.write_demand_observations(conn, result.rows)
            if track or stratify:
                to_track = (
                    importers.stratified_sample(result.rows, stratify)
                    if stratify
                    else result.rows
                )
                for row in to_track:
                    _, created = repository.add_keyword(conn, row.keyword, row.country)
                    tracked += created

    console.print(
        f"[green]Imported {written} {source} observation(s)[/green] for "
        f"{country.lower()}."
    )
    for problem in result.skipped[:10]:
        err_console.print(f"[yellow]skipped[/yellow] {problem}")
    if len(result.skipped) > 10:
        err_console.print(f"[dim]...and {len(result.skipped) - 10} more skipped.[/dim]")

    if track or stratify:
        console.print(f"[green]Now tracking {tracked} new keyword(s).[/green]")
        if stratify:
            console.print(
                "[dim]Spread evenly across the demand range — calibration needs "
                "spread, not volume, and each keyword costs a ladder walk.[/dim]"
            )
        console.print("[dim]Next: aso refresh, then aso calibrate[/dim]")
    else:
        console.print(
            "[dim]Calibration needs a ladder observation for each keyword too. "
            "Re-run with --track to add them, then `aso refresh`.[/dim]"
        )


@app.command(name="import-competition")
def import_competition(
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV export."),
    source: str = typer.Option(
        "appfigures", "--source", "-s", help="Export format carrying a difficulty column."
    ),
    country: str = typer.Option(
        settings.default_country, "--country", "-c", help="Storefront these rows describe."
    ),
    track: bool = typer.Option(
        False, "--track", help="Also start tracking every imported keyword."
    ),
) -> None:
    """Import a vendor's keyword difficulty ratings, for calibration.

    The same AppFigures file `aso import-demand` reads: that command takes the
    Popularity column, this one takes Competitiveness. Run both on the file to
    calibrate both halves of the score.

    The storefront is a flag, not a column, for the same reason as import-demand
    — these exports don't carry one, and the wrong --country would silently
    calibrate against another market.
    """
    try:
        result = importers.read_competition_csv(path, source=source, country=country)
    except importers.ImportError_ as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if not result.rows:
        err_console.print("[red]Nothing usable in that file.[/red]")
        raise typer.Exit(1)

    tracked = 0
    with session() as conn:
        init_db()
        with transaction(conn):
            written = repository.write_competition_observations(conn, result.rows)
            if track:
                for row in result.rows:
                    _, created = repository.add_keyword(conn, row.keyword, row.country)
                    tracked += created

    console.print(
        f"[green]Imported {written} {source} difficulty rating(s)[/green] for "
        f"{country.lower()}."
    )
    for problem in result.skipped[:10]:
        err_console.print(f"[yellow]skipped[/yellow] {problem}")
    if len(result.skipped) > 10:
        err_console.print(f"[dim]...and {len(result.skipped) - 10} more skipped.[/dim]")

    if track:
        console.print(f"[green]Now tracking {tracked} new keyword(s).[/green]")
    console.print(
        "[dim]Calibration needs a SERP capture for each keyword too. "
        "Run `aso refresh`, then `aso calibrate-competition`.[/dim]"
    )


@app.command(name="calibrate-competition")
def calibrate_competition(
    source: str = typer.Option(
        "appfigures", "--source", "-s", help="Which difficulty source to fit against."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the fit as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Fit the competition weights against a vendor's difficulty index.

    Prints constants to paste into config.py. Like `aso calibrate`, it does not
    write them: a scoring constant that changes itself is one nobody reviews.
    After pasting, run `aso rescore` to move history onto the new weights.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        samples = [
            dict(row) for row in repository.competition_calibration_samples(conn, source)
        ]

    if not samples:
        err_console.print(
            f"[red]No stored {source} difficulty ratings match a tracked keyword "
            "with a SERP capture.[/red]\n"
            "[dim]Run `aso import-competition <file.csv> --track`, then "
            "`aso refresh`.[/dim]"
        )
        raise typer.Exit(1)

    try:
        fit = competition.calibrate(samples)
    except competition.NotEnoughData as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        console.print_json(
            data={
                **fit.as_config(),
                "source": source,
                "n_samples": fit.n_samples,
                "spearman": round(fit.spearman, 4),
                "baseline_spearman": round(fit.baseline_spearman, 4),
                "cv_spearman": (
                    None if fit.cv_spearman is None else round(fit.cv_spearman, 4)
                ),
            }
        )
        return

    fitted = fit.as_config()
    table = Table(
        title=f"Fitted against {fit.n_samples} keywords rated by {source}",
        header_style="bold",
    )
    table.add_column("component")
    table.add_column("current", justify="right")
    table.add_column("fitted", justify="right")
    for name, value in fitted["COMPETITION_WEIGHTS"].items():  # type: ignore[union-attr]
        current = COMPETITION_WEIGHTS.get(name, 0.0)
        style = "" if abs(current - value) < 1e-9 else "bold"
        table.add_row(
            name.removeprefix("comp_"),
            f"{current:.2f}",
            f"[{style}]{value:.3f}[/{style}]" if style else f"{value:.3f}",
        )
    table.add_row(
        "(aggregation power)",
        f"{competition.COMPETITION_AGGREGATION_POWER:.1f}",
        f"{fit.power:.1f}",
    )
    console.print(table)

    console.print(
        f"\nrank correlation with {source} difficulty: "
        f"[bold]{fit.spearman:.3f}[/bold] in-sample "
        f"(current weights: {fit.baseline_spearman:.3f})"
    )
    if fit.cv_spearman is not None:
        console.print(
            f"cross-validated (out-of-sample): [bold]{fit.cv_spearman:.3f}[/bold]\n"
            "[dim]That is the number to believe — in-sample is the best of "
            "thousands of grid points scored on its own training data.[/dim]"
        )
    console.print(
        f"[dim]{fit.distinct_targets} distinct difficulty values across "
        f"{fit.n_samples} keywords.[/dim]"
    )
    if fit.excluded_components:
        err_console.print(
            "[yellow]Not fitted, too rarely measured: "
            f"{', '.join(n.removeprefix('comp_') for n in fit.excluded_components)}."
            "[/yellow]\n[dim]Those weights are 0 because the fit never saw the "
            "component, which is not the same as the fit rejecting it. Run "
            "`aso refresh` to fill the columns and calibrate again before "
            "reading anything into them.[/dim]"
        )
    console.print(
        f"[dim]{source} difficulty is a vendor's derived index, not ground truth. "
        "Fitting to it reproduces that vendor — better than fitting to nothing, "
        "and not the same as being right.[/dim]"
    )

    if fit.improved and fit.margin >= 0.05:
        console.print(
            f"[green]The fit beats the current weights by {fit.margin:.3f}.[/green] "
            "Paste the values above into aso/config.py, then run "
            "[bold]aso rescore[/bold]."
        )
    elif fit.improved:
        console.print(
            f"[yellow]The fit is ahead by only {fit.margin:.3f}.[/yellow]\n"
            "[dim]That is inside the noise for a sample this size — adopting it "
            "would be churn, not calibration.[/dim]"
        )
    else:
        console.print(
            "[yellow]The fit does not beat the current weights.[/yellow]\n"
            "[dim]Keep what you have and gather more ratings.[/dim]"
        )


@app.command()
def calibrate(
    source: Optional[str] = typer.Option(
        None, "--source", "-s", help="Which demand source to fit against."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the fit as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Fit the search mapping against measured demand.

    Prints constants to paste into config.py. It deliberately does not write
    them: a scoring constant that changes itself is one nobody reviews. After
    pasting, run `aso rescore` to move history onto the new mapping.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        available = repository.demand_sources(conn)
        if not available:
            err_console.print(
                "[red]No measured demand to calibrate against.[/red]\n"
                "[dim]Either `aso asa pull` (needs ad spend), or export keywords "
                "with popularity scores and `aso import-demand <file.csv>`.[/dim]"
            )
            raise typer.Exit(1)

        # Priority, not row count. AppFigures will almost always have the most
        # rows and is the weakest of the three — its popularity is derived from
        # the very Apple indicator `apple` reports directly, so preferring it
        # on volume would fit the copy in preference to the original.
        chosen = source or repository.preferred_demand_source(conn) or available[0]["source"]
        if chosen not in {row["source"] for row in available}:
            err_console.print(
                f"[red]No demand stored for source {chosen!r}.[/red] "
                f"Available: {', '.join(r['source'] for r in available)}"
            )
            raise typer.Exit(1)
        samples = pipeline.calibration_from_db(conn, chosen)

    if source is None and len(available) > 1:
        console.print(
            f"[dim]Fitting against {chosen!r} (highest-priority source with "
            f"scored rows). Use --source to pick another.[/dim]"
        )

    try:
        fit = search_scoring.calibrate(samples)
    except search_scoring.NotEnoughData as exc:
        err_console.print(f"[red]{exc}[/red]")
        stored = next(r for r in available if r["source"] == chosen)
        err_console.print(
            f"[dim]{stored['rows']} {chosen} observation(s) are stored, but only "
            f"{len(samples)} matched a tracked keyword with a ladder observation. "
            "Track those keywords and run `aso refresh`.[/dim]"
        )
        raise typer.Exit(1)

    if as_json:
        console.print_json(
            data={
                **fit.as_config(),
                "source": chosen,
                "n_samples": fit.n_samples,
                "rmse": round(fit.rmse, 3),
                "spearman": round(fit.spearman, 4),
                "baseline_spearman": round(fit.baseline_spearman, 4),
                "cv_spearman": (
                    None if fit.cv_spearman is None else round(fit.cv_spearman, 4)
                ),
            }
        )
        return

    table = Table(
        title=f"Fitted against {fit.n_samples} keywords measured by {chosen}",
        header_style="bold",
    )
    table.add_column("constant")
    table.add_column("value", justify="right")
    for name, value in fit.as_config().items():
        table.add_row(name, str(value))
    console.print(table)

    console.print(
        f"\nrank correlation with measured demand: "
        f"[bold]{fit.spearman:.3f}[/bold] in-sample "
        f"(current constants: {fit.baseline_spearman:.3f})"
    )
    if fit.cv_spearman is not None:
        console.print(
            f"cross-validated (out-of-sample): [bold]{fit.cv_spearman:.3f}[/bold]\n"
            f"[dim]That is the number to believe — in-sample is the best of "
            f"{search_scoring.grid_size():,} grid points scored on its own "
            f"training data.[/dim]"
        )
        if fit.cv_spearman < 0.30:
            err_console.print(
                "[yellow]That is weak.[/yellow] [dim]The autocomplete ladder only "
                "loosely tracks this demand source, so the score stays a rough "
                "ordering — better than uncalibrated, but not a measurement.[/dim]"
            )
    if fit.at_grid_edge:
        err_console.print(
            f"[yellow]At the edge of the search grid: {', '.join(fit.at_grid_edge)}."
            "[/yellow]\n[dim]The best value is probably outside the range searched, "
            "so these are boundary artefacts rather than optima. Widen the "
            "CALIBRATION_*_GRID ranges in config.py, or treat the fit as "
            "provisional.[/dim]"
        )

    if fit.improved and fit.margin >= 0.05:
        console.print(
            f"[green]The fit beats the current constants by {fit.margin:.3f}.[/green] "
            "Paste the values above into aso/config.py, then run [bold]aso rescore[/bold]."
        )
    elif fit.improved:
        console.print(
            f"[yellow]The fit is ahead by only {fit.margin:.3f}.[/yellow]\n"
            "[dim]That is inside the noise for a sample this size — adopting it "
            "would be churn, not calibration. Gather more keywords spanning a "
            "wider demand range and re-run.[/dim]"
        )
    else:
        console.print(
            "[yellow]The fit does not beat the current constants on this sample.[/yellow]\n"
            "[dim]Keep what you have and gather more data — a fit that wins only "
            "on the points it was fitted to is overfitting, not calibration.[/dim]"
        )


@app.command(name="bridge")
def fit_bridge(
    country: str = typer.Option(
        settings.default_country, "--country", "-c", help="Storefront to fit."
    ),
    source: str = typer.Option(
        "apple", "--source", "-s", help="Measured demand source to anchor on."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Store the fit. Without this, nothing is written."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Fit the map from proxy score onto the measured demand scale.

    The proxy's ordering is evidence; its absolute level is arbitrary. Once
    some keywords carry a measured value, the demand column mixes two rulers,
    and `opportunity` multiplies by that column. This fits the monotone map
    that makes them commensurable.

    Note the reported figure is RMSE, not rank correlation. A monotone map
    cannot change a rank correlation by even a rounding error, so a before and
    after Spearman here would print the same number twice and look like proof.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        pairs = repository.bridge_pairs(conn, country=country, source=source)
        if not pairs:
            err_console.print(
                f"[yellow]No keyword in '{country}' has both a proxy score and a "
                f"'{source}' demand value.[/yellow]\n"
                "[dim]Pull measured demand first, then `aso refresh` so those "
                "keywords have ladder observations to score.[/dim]"
            )
            raise typer.Exit(1)

        try:
            fitted = bridge_scoring.fit(
                [(row["proxy_score"], row["measured"]) for row in pairs]
            )
        except bridge_scoring.NotEnoughOverlap as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        table = Table(title=f"Bridge: proxy -> {source} ({country})")
        table.add_column("proxy", justify="right")
        table.add_column("maps to", justify="right")
        for x, y in fitted.knots:
            table.add_row(fmt(x), fmt(y))
        console.print(table)
        console.print(
            f"[dim]{fitted.n_overlap} overlapping keyword(s), "
            f"{len(fitted.knots)} knot(s), RMSE {fitted.rmse:.2f} on Apple's "
            "0-100 scale.[/dim]"
        )

        if not apply:
            console.print(
                "\n[yellow]Not stored.[/yellow] Re-run with [bold]--apply[/bold] "
                "to save it, then [bold]aso rescore[/bold]."
            )
            return

        with transaction(conn):
            repository.write_bridge(
                conn,
                country=country,
                source=source,
                knots_json=fitted.to_json(),
                n_overlap=fitted.n_overlap,
                rmse=fitted.rmse,
            )
        console.print("[green]Bridge stored.[/green] [dim]Next: aso rescore[/dim]")


@app.command(name="bridge-competition")
def fit_competition_bridge(
    country: str = typer.Option(
        settings.default_country, "--country", "-c", help="Storefront to fit."
    ),
    source: str = typer.Option(
        COMPETITION_BRIDGE_SOURCE,
        "--source",
        "-s",
        help="Which difficulty index to anchor the level on.",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Store the fit. Without this, nothing is written."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Fit the map from the raw competition score onto a vendor's difficulty level.

    THE JOB THIS DOES THAT `calibrate-competition` CANNOT
    -----------------------------------------------------
    `calibrate-competition` scores candidate weights by rank correlation, and a
    rank correlation is unchanged by any monotone transform of the output. It
    therefore cannot see level *at all*: weights that reproduce the vendor's
    ordering perfectly while sitting twenty points low score exactly as well as
    weights that also match the level. No amount of extra data changes that —
    it is what the statistic is.

    So the two are split. That command fixes the ordering and reports Spearman.
    This one fixes the scale and reports RMSE.

    RUN IT LAST. A bridge maps whatever the raw score currently is, so fitting
    one before the components and weights are settled bakes today's distortion
    into the knots and hides it behind a plausible number.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        samples = [
            dict(row) for row in repository.competition_calibration_samples(conn, source)
        ]
        samples = [s for s in samples if s.get("country", country) == country]

        if not samples:
            err_console.print(
                f"[red]No keyword in '{country}' has both a competition score "
                f"and a '{source}' difficulty rating.[/red]\n"
                "[dim]Run `aso import-competition <file.csv> --track`, then "
                "`aso refresh`.[/dim]"
            )
            raise typer.Exit(1)

        try:
            fitted = competition.fit_bridge(samples)
        except bridge_scoring.NotEnoughOverlap as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        raw = [
            competition.combine_with(
                competition.derive(s),
                COMPETITION_WEIGHTS,
                competition.COMPETITION_AGGREGATION_POWER,
            )
            for s in samples
        ]
        scored = [(v, float(s["value"])) for v, s in zip(raw, samples) if v is not None]
        before = math.sqrt(sum((v - t) ** 2 for v, t in scored) / len(scored))
        raw_mean = sum(v for v, _ in scored) / len(scored)
        target_mean = sum(t for _, t in scored) / len(scored)

        table = Table(title=f"Competition bridge: raw -> {source} ({country})")
        table.add_column("raw", justify="right")
        table.add_column("maps to", justify="right")
        for x, y in fitted.knots:
            table.add_row(fmt(x), fmt(y))
        console.print(table)

        console.print(
            f"\nmean level: [bold]{raw_mean:.1f}[/bold] raw vs "
            f"[bold]{target_mean:.1f}[/bold] for {source}"
        )
        console.print(
            f"RMSE against {source}: [bold]{before:.2f}[/bold] before, "
            f"[bold]{fitted.rmse:.2f}[/bold] after "
            f"[dim]({fitted.n_overlap} keywords, {len(fitted.knots)} knots)[/dim]"
        )
        console.print(
            "[dim]RMSE is the measure here, not rank correlation. The map is "
            "monotone, so it cannot reorder anything — a before/after Spearman "
            "would move only by tie-pooling and would not be evidence.[/dim]"
        )

        if not apply:
            console.print(
                "\n[yellow]Not stored.[/yellow] Re-run with [bold]--apply[/bold] "
                "to save it, then [bold]aso rescore[/bold]."
            )
            return

        with transaction(conn):
            repository.write_bridge(
                conn,
                country=country,
                source=source,
                knots_json=fitted.to_json(),
                n_overlap=fitted.n_overlap,
                rmse=fitted.rmse,
                metric="competition",
            )
        console.print("[green]Bridge stored.[/green] [dim]Next: aso rescore[/dim]")


@app.command(name="eval")
def evaluate(
    country: str = typer.Option(
        settings.default_country, "--country", "-c", help="Storefront to evaluate."
    ),
    source: Optional[str] = typer.Option(
        None, "--source", "-s", help="Demand source to grade against. Default: best available."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Grade the demand proxy against measured demand. No network access.

    This is the number that says whether a change helped. Run it before and
    after touching the scoring constants, the components, or the bridge.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        chosen = source or repository.preferred_demand_source(conn)
        if chosen is None:
            err_console.print(
                "[yellow]No demand source has any scored keyword to grade "
                "against.[/yellow]\n"
                "[dim]Import an export with `aso import-demand`, or pull "
                "Apple's popularity index.[/dim]"
            )
            raise typer.Exit(1)

        pairs = repository.bridge_pairs(conn, country=country, source=chosen)
        if len(pairs) < 2:
            err_console.print(
                f"[yellow]Only {len(pairs)} keyword(s) in '{country}' carry both "
                f"a proxy score and a '{chosen}' value — too few to grade.[/yellow]"
            )
            raise typer.Exit(1)

        proxy = [row["proxy_score"] for row in pairs]
        measured = [row["measured"] for row in pairs]
        rho = search_scoring.spearman(proxy, measured)

        coverage = conn.execute(
            """
            SELECT COALESCE(s.search_source, 'proxy') AS src, COUNT(*) AS n
            FROM keywords k
            JOIN snapshots s ON s.id = (
                SELECT id FROM snapshots
                WHERE keyword_id = k.id
                ORDER BY captured_at DESC, id DESC LIMIT 1
            )
            WHERE k.country = ? AND k.active = 1
            GROUP BY src
            """,
            (country.lower(),),
        ).fetchall()

        table = Table(title=f"Demand proxy vs '{chosen}' ({country})")
        table.add_column("metric")
        table.add_column("value", justify="right")
        table.add_row("overlapping keywords", str(len(pairs)))
        table.add_row("Spearman (proxy vs measured)", f"{rho:+.3f}")

        stored = repository.latest_bridge(conn, country=country, source=chosen)
        if stored is not None:
            fitted = bridge_scoring.Bridge.from_json(
                stored["knots"], n_overlap=stored["n_overlap"], rmse=stored["rmse"] or 0.0
            )
            residuals = [fitted.apply(p) - m for p, m in zip(proxy, measured)]
            rmse = (sum(r * r for r in residuals) / len(residuals)) ** 0.5
            table.add_row("RMSE after bridge", f"{rmse:.2f}")
        else:
            table.add_row("RMSE after bridge", "[dim]no bridge fitted[/dim]")

        for row in coverage:
            table.add_row(f"latest snapshots scored by {row['src']}", str(row["n"]))
        console.print(table)

        console.print(
            "[dim]Spearman is the proxy's own quality — the bridge cannot change "
            "it, and the blended column is not graded here because where a "
            "measured value exists the blend simply IS that value, which would "
            "score a perfect correlation against itself.[/dim]"
        )


apple_app = typer.Typer(
    help="Apple's keyword popularity index (undocumented endpoint, opt-in).",
    no_args_is_help=True,
)
app.add_typer(apple_app, name="apple")


@apple_app.command(name="pull")
def apple_pull(
    country: str = typer.Option(
        settings.default_country, "--country", "-c", help="Storefront to read."
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n", help="Stop after this many keywords."
    ),
    force: bool = typer.Option(False, "--force", help="Ignore cached readings."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Pull Apple's 5-100 popularity for your tracked keywords.

    This calls an endpoint Apple does not document, so it is opt-in: set
    ASO_APPLE_POPULARITY_ENABLED=true in .env. With it off, every demand score
    falls back to the autocomplete and SERP proxy, exactly as before.

    Readings land in `demand_observations` under source 'apple'. A keyword
    Apple declines to score is stored as censored — that is a measurement of
    low demand, not a missing row.
    """
    setup_logging(verbose)
    with session() as conn:
        init_db()
        rows = repository.list_keywords(conn, country=country, active_only=True)
        keywords = [row["keyword"] for row in rows][: limit or None]
        if not keywords:
            err_console.print(
                f"[yellow]No active keywords tracked in '{country}'.[/yellow]"
            )
            raise typer.Exit(1)

        async def main():
            async with asa_fetcher() as fetcher:
                return await pipeline.pull_apple_popularity(
                    conn, keywords, country=country, config=settings, fetcher=fetcher
                )

        try:
            report = asyncio.run(main())
        except apple_popularity.ApplePopularityDisabled as exc:
            err_console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(1) from exc
        except (
            apple_popularity.ApplePopularityNotConfigured,
            apple_popularity.ApplePopularitySessionExpired,
        ) as exc:
            err_console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(1) from exc
        except apple_popularity.ApplePopularityError as exc:
            err_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    console.print(
        f"[green]{report.scored} scored[/green], "
        f"[yellow]{report.censored} below Apple's threshold[/yellow], "
        f"of {report.requested} requested ({report.from_cache} from cache)\n"
        f"[green]+{report.related} related keyword(s)[/green] measured for free"
    )
    if report.scored:
        console.print("[dim]Next: aso bridge --apply, then aso rescore[/dim]")


@apple_app.command(name="login")
def apple_login(
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for sign-in."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    """Sign in once, and keep the session for headless pulls afterwards.

    Opens a real browser window at the Apple Ads dashboard and waits for you to
    sign in — password, two-factor, whatever Apple asks. Nothing types on your
    behalf. The resulting session is saved to a browser profile on disk, and
    `aso apple pull` reuses it without a window.

    The saved profile is a credential. It lives at ASO_APPLE_PROFILE_DIR
    (default `.apple-session/`) and is gitignored.

    Sessions do eventually end. When one does, `aso apple pull` says so and
    points back here rather than failing obscurely.
    """
    setup_logging(verbose)
    from .clients import apple_transport

    profile = settings.apple_profile_dir
    console.print(
        f"Opening a browser window at {apple_transport.DASHBOARD_URL} …\n"
        f"[dim]Sign in there. Nothing is typed for you, and the session is "
        f"saved to {profile}.[/dim]"
    )

    try:
        landed = asyncio.run(
            apple_transport.interactive_login(profile, timeout_seconds=timeout)
        )
    except apple_transport.ApplePopularityError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Signed in.[/green] [dim]Landed on {landed.split('?')[0]}[/dim]")
    console.print(
        "[dim]Session saved. Next: set ASO_APPLE_POPULARITY_ENABLED=true and "
        "ASO_APPLE_ADAM_ID in .env, then `aso apple pull`.[/dim]"
    )


@apple_app.command(name="status")
def apple_status() -> None:
    """Report which Apple session is configured, and whether it looks usable.

    Answers "why did that fail?" without spending a request on an endpoint that
    is not supposed to be poked for diagnostics.
    """
    table = Table(title="Apple popularity configuration")
    table.add_column("setting")
    table.add_column("value", justify="right")

    ok = "[green]set[/green]"
    missing = "[yellow]missing[/yellow]"
    table.add_row(
        "ASO_APPLE_POPULARITY_ENABLED",
        "[green]true[/green]" if settings.apple_popularity_enabled else "[yellow]false[/yellow]",
    )
    table.add_row("ASO_APPLE_TRANSPORT", settings.apple_transport)
    table.add_row("ASO_APPLE_ADAM_ID", ok if settings.apple_adam_id else missing)
    table.add_row(
        "browser profile",
        f"[green]{settings.apple_profile_dir}[/green]"
        if settings.apple_profile_exists
        else f"[yellow]none at {settings.apple_profile_dir}[/yellow]",
    )
    table.add_row("ASO_APPLE_COOKIE", ok if settings.apple_cookie else missing)
    console.print(table)

    if not settings.apple_popularity_enabled:
        console.print(
            "[dim]Disabled — demand scores use the autocomplete and SERP "
            "proxy. Set ASO_APPLE_POPULARITY_ENABLED=true to change that.[/dim]"
        )
    elif not settings.apple_profile_exists and not settings.apple_cookie:
        console.print(
            "[yellow]No session available.[/yellow] [dim]Run `aso apple login` "
            "for the durable path, or paste ASO_APPLE_COOKIE for the quick "
            "one.[/dim]"
        )
