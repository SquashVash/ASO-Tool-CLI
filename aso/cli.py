"""Typer entrypoint.

    aso init
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
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, pipeline, repository
from .config import COMPETITION_WEIGHTS, settings
from .db import init_db, session
from .repository import UnknownKeyword

app = typer.Typer(
    help="ASO keyword research for the iOS App Store.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=err_console, show_path=False, rich_tracebacks=True)],
    )


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

    components = Table("competition component", "weight", "value", title="components")
    for name, weight in COMPETITION_WEIGHTS.items():
        components.add_row(name.removeprefix("comp_"), f"{weight:.2f}", fmt(latest[name]))
    console.print(components)
    console.print(
        f"[dim]prefix depth {latest['search_prefix_depth']}, "
        f"hint rank {latest['search_hint_rank']} "
        f"(keyword is {len(row['keyword'])} chars)[/dim]\n"
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
