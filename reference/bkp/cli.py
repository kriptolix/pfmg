from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint
from pfmg import __version__

from pfmg.learn.learn import learn_app
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

app = typer.Typer(
    name="pfmg",
    help="Python Flatpak Manifest Resolver - modern replacement for flatpak-pip-generator.",
    rich_markup_mode="rich",
)
console = Console()

app.add_typer(learn_app, name="learn")

# ---------------------------------------------------------------------------
# pfmg version
# ---------------------------------------------------------------------------

@app.command("version")
def version():
    """Print pfmg version."""
    rprint(f"pfmg [bold]{__version__}[/bold]")


def main():
    app()


if __name__ == "__main__":
    main()