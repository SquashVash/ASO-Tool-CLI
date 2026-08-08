"""Typer entrypoint.

Scaffold only at this stage: `init` and `version`. The keyword commands
(`add`, `import`, `refresh`, `list`, `show`, `track`, `export`) land with the
pipeline.
"""

from __future__ import annotations

import typer
from rich.console import Console

from . import __version__
from .config import settings
from .db import init_db

app = typer.Typer(
    help="ASO keyword research for the iOS App Store.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def init() -> None:
    """Create the database and apply any pending migrations."""
    applied = init_db()
    if applied:
        console.print(
            f"[green]Applied migrations:[/green] {', '.join(str(v) for v in applied)}"
        )
    else:
        console.print("[dim]Schema already up to date.[/dim]")
    console.print(f"[dim]Database:[/dim] {settings.db_path}")


@app.command()
def version() -> None:
    """Print the tool version."""
    console.print(f"aso {__version__}")


if __name__ == "__main__":
    app()
