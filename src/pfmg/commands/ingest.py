import typer
from pathlib import Path
from rich.console import Console
from rich import print as rprint

from pfmg.knowledge.importer import ModulesImporter

console = Console()

_DEFAULT_REPO_ROOT = Path(".")

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