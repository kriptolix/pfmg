"""
pfmg.learn.cli — standalone learning commands.

All commands write directly to data/ — no knowledge graph.

Commands:
  pfmg learn import   — import modules from a shared-modules clone
  pfmg learn inspect  — probe a Flatpak SDK or extension and write its profile
  pfmg learn stats    — show recipe/data counts
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich import print as rprint

from pfmg.learn.importer import ModulesImporter
from pfmg.learn.inspector import Prober 
from pfmg.utils.logging import get_logger
from pfmg.utils.text import is_extension

logger = get_logger(__name__)


console = Console()

_DEFAULT_REPO_ROOT = Path(".")


# ---------------------------------------------------------------------------
# pfmg learn import
# ---------------------------------------------------------------------------

def cmd_import(
    modules_dir: Path = typer.Argument(
        ...,
        help="Path to modules repo or any dir with module JSON/YAML files",
    ),
    repo_root: Path = typer.Option(_DEFAULT_REPO_ROOT, "--repo-root", "-r"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Import native library and Python recipes from a modules directory."""
    import os
    if verbose:
        os.environ["PFMG_LOG_LEVEL"] = "DEBUG"

    if not modules_dir.exists():
        rprint(f"[red]Directory not found: {modules_dir}[/red]")
        raise typer.Exit(1)

    importer = ModulesImporter(repo_root=repo_root)

    with console.status("[bold green]Scanning modules..."):
        report = importer.import_from(modules_dir)

    rprint("\n[bold]shared-modules import[/bold]")
    rprint(f"  Scanned           : {report.scanned}")
    rprint(f"  Imported          : {report.imported}")
    rprint(f"  Skipped (exists)  : {report.skipped_existing}")
    rprint(f"  Skipped (no src)  : {report.skipped_no_source}")

    if report.created:
        rprint(f"\n[bold]Created {len(report.created)} recipe(s):[/bold]")
        for p in report.created:
            rprint(f"  [green]{p}[/green]")

    if report.errors:
        rprint(f"\n[red]{len(report.errors)} error(s):[/red]")
        for e in report.errors[:5]:
            rprint(f"  {e}")


# ---------------------------------------------------------------------------
# pfmg learn inspect
# ---------------------------------------------------------------------------

def cmd_inspect(
    target: str = typer.Argument(
        ...,
        help=(
            "SDK or Extension ID. Extensions (containing .Extension.) "
            "are detected automatically."
        ),
    ),
    target_version: str = typer.Option("25.08", "--sdk-version", "-V"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o"),
    no_cleanup: bool = typer.Option(
        False, "--nocleanup",
        help="Don't uninstall after probing",
    ),
):
    """
    Introspect a Flatpak SDK or Extension and write a static profile JSON.

    Auto-detects base SDKs vs extensions from the ID:

      # Base SDK:
      pfmg inspect org.freedesktop.Sdk --sdk-version 24.08

      # Extension (auto-routed):
      pfmg inspect org.freedesktop.Sdk.Extension.node24 --sdk-version 25.08

    Requires flatpak to be installed.  For extensions the base SDK must also
    be installed.

    Written to:
      Base SDK:  data/sdk-profiles/<shortname>.<version>.json
      Extension: data/ext-profiles/<shortname>.<version>.json
    """
    prober = Prober(output_dir=output_dir, no_cleanup=no_cleanup)

    if not prober.is_available():
        rprint("[red]flatpak not found. Install with your package manager.[/red]")
        raise typer.Exit(1)

    if is_extension(target):
        _cmd_inspect_ext(target, target_version, prober)
    else:
        _cmd_inspect_sdk(target, target_version, prober)


def _cmd_inspect_sdk(sdk: str, sdk_version: str, prober: Prober) -> None:
    with console.status(f"[bold green]Inspecting {sdk}..."):
        result = prober.probe_sdk(sdk, sdk_version)

    if result.success:
        rprint(f"[bold green]OK[/bold green] — {result.sdk_id}//{result.sdk_version}")
        rprint(f"  pkg-config : {len(result.pkgconfig)}")
        rprint(f"  libraries  : {len(result.libraries)}")
        exe_preview = ", ".join(result.executables[:8])
        extra = f" +{len(result.executables) - 8} more" if len(result.executables) > 8 else ""
        rprint(f"  executables: {exe_preview}{extra}")
    else:
        rprint(f"[bold red]Failed[/bold red]: {result.error}")
        raise typer.Exit(1)


def _cmd_inspect_ext(ext: str, ext_version: str, prober: Prober) -> None:
    with console.status(f"[bold green]Inspecting {ext}..."):
        result = prober.probe_ext(ext, ext_version)

    if result.success:
        rprint(f"[bold green]OK[/bold green] — {ext}")
        rprint(f"  executables: {result.executables}")
        rprint(f"  pkg-config : {len(result.pkgconfig)}")
    else:
        rprint(f"[red]Failed: {result.error}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# pfmg learn stats
# ---------------------------------------------------------------------------

def cmd_stats(
    repo_root: Path = typer.Option(_DEFAULT_REPO_ROOT, "--repo-root", "-r"),
):
    """Show counts of recipes and data files."""

    def _count(directory: Path, glob: str) -> int:
        if not directory.exists():
            return 0
        return len(list(directory.glob(glob)))

    data = repo_root / "data"
    rprint(f"\n[bold]pfmg repository stats[/bold] ({repo_root.resolve()})")
    rprint(f"  data/nat-recipes/  : {_count(data / 'nat-recipes',  '*.json')}")
    rprint(f"  data/pip-recipes/  : {_count(data / 'pip-recipes',  '*.json')}")
    rprint(f"  data/sdk-profiles/ : {_count(data / 'sdk-profiles', '*.json')}")
    rprint(f"  data/ext-profiles/ : {_count(data / 'ext-profiles', '*.json')}")
    rprint("")
