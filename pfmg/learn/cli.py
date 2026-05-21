"""
pfmg.learn.cli — standalone learning commands.

All commands write directly to recipes/ and data/ — no knowledge graph.
Export is the default behavior for every command.

Commands:
  pfmg learn analyzer <path>  — analyze a manifest file or directory
  pfmg learn import   — import modules from a shared-modules clone
  pfmg learn inspect    — probe all default SDKs and extensions
  pfmg learn list         — list available sdk-profile TOMLs
  pfmg learn stats            — show recipe/data counts
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from pfmg.learn.analyzer import ManifestAnalyzer
from pfmg.learn.importer import ModulesImporter
from pfmg.learn.exporter import Exporter, ExportReport
from pfmg.learn.inspector import Prober, _is_extension, _base_sdk_from_extension
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

learn_app = typer.Typer(
    name="learn",
    help="Mine manifests and import recipes without a CI environment.",
    rich_markup_mode="rich",
)
console = Console()

_DEFAULT_REPO_ROOT = Path(".")


# ---------------------------------------------------------------------------
# Shared helper: analysis → recipes
# ---------------------------------------------------------------------------

def _analyses_to_recipes(analyses, repo_root: Path, dry_run: bool) -> ExportReport:
    """Convert ManifestAnalysis objects directly to recipe files."""
    exporter = Exporter(analyses, repo_root)
    return exporter.export(dry_run=dry_run)


def _print_export_report(report: ExportReport, dry_run: bool) -> None:
    if not any(report.created + report.updated):
        rprint("[dim]No new recipe files.[/dim]")
        return
    label = "Would write" if dry_run else "Written"
    for c in report.created:
        rprint(f"  [green]create[/green]  {c.path}  [dim]{c.reason}[/dim]")
    for c in report.updated:
        rprint(f"  [yellow]update[/yellow]  {c.path}  [dim]{c.reason}[/dim]")


# ---------------------------------------------------------------------------
# pfmg learn manifest
# ---------------------------------------------------------------------------

@learn_app.command("analyze")
def cmd_manifest(
    target: Path = typer.Argument(
        ...,
        help="Manifest file (JSON/YAML) or directory to scan recursively",
    ),
    repo_root: Path = typer.Option(_DEFAULT_REPO_ROOT, "--repo-root", "-r"),
    no_export: bool = typer.Option(False, "--no-export"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive"),
):
    """
    Analyze a Flatpak manifest file or directory and extract native recipes.

    Accepts a single JSON/YAML manifest or a directory (scanned recursively).
    Writes extracted native module recipes to recipes/native/.
    """
    analyzer = ManifestAnalyzer()

    if target.is_dir():
        analyses = analyzer.analyze_directory(target, recursive=recursive)
        rprint(f"\nFound [bold]{len(analyses)}[/bold] manifests in {target}")
    elif target.is_file():
        analysis = analyzer.analyze(target)
        analyses = [analysis] if analysis else []
    else:
        rprint(f"[red]Not found: {target}[/red]")
        raise typer.Exit(1)

    if not analyses:
        rprint("[yellow]No manifests found.[/yellow]")
        raise typer.Exit()

    # Quick summary
    total_native = sum(len(a.native_modules) for a in analyses)
    total_python = sum(len(a.python_packages) for a in analyses)
    rprint(f"  Native modules : {total_native}")
    rprint(f"  Python packages: {total_python}")

    if not no_export:
        rprint("\n[bold]Exporting recipes...[/bold]")
        exporter = Exporter(analyses, repo_root)
        report = exporter.export(dry_run=dry_run)
        _print_export_report(report, dry_run)


# ---------------------------------------------------------------------------
# pfmg learn shared-modules
# ---------------------------------------------------------------------------

@learn_app.command("import")
def cmd_shared_modules(
    modules_dir: Path = typer.Argument(
        ...,
        help="Path to a cloned shared-modules repo or any dir with module JSON files",
    ),
    repo_root: Path = typer.Option(_DEFAULT_REPO_ROOT, "--repo-root", "-r"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """
    Import native library recipes from a modules directory.
    """
    import os
    if verbose:
        os.environ["pfmg_LOG_LEVEL"] = "DEBUG"

    if not modules_dir.exists():
        rprint(f"[red]Directory not found: {modules_dir}[/red]")
        raise typer.Exit(1)

    importer = ModulesImporter(repo_root=repo_root)

    with console.status("[bold green]Scanning modules..."):
        report = importer.import_from(modules_dir, dry_run=dry_run)

    rprint(f"\n[bold]shared-modules import[/bold]")
    rprint(f"  Scanned           : {report.scanned}")
    rprint(f"  Imported          : {report.imported}")
    rprint(f"  Skipped (exists)  : {report.skipped_existing}")
    rprint(f"  Skipped (no src)  : {report.skipped_no_source}")

    if report.created:
        rprint(f"\n[bold]{'Would create' if dry_run else 'Created'} {len(report.created)} recipe(s):[/bold]")
        for p in report.created:
            rprint(f"  [green]{'(dry)' if dry_run else ''}[/green] {p}")

    if report.errors:
        rprint(f"\n[red]{len(report.errors)} error(s):[/red]")
        for e in report.errors[:5]:
            rprint(f"  {e}")


# ---------------------------------------------------------------------------
# pfmg learn sdk
# ---------------------------------------------------------------------------

@learn_app.command("inspect")
def cmd_probe(
    target: str = typer.Argument(
        ...,
        help="SDK or Extension ID. Extensions (containing .Extension.) are detected automatically.",
    ),
    target_version: str = typer.Option("25.08", "--sdk-version", "-V"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o"),
    no_cleanup: bool = typer.Option(False,"--nocleanup",
        help="Don't uninstall after probing",),
):
    """
    Introspect a Flatpak SDK or Extension and write a static profile TOML.

    Works with both base SDKs and extensions — auto-detected from the ID:

      # Base SDK:
      pfmg learn sdk probe -s org.freedesktop.Sdk -V 24.08

      # Extension (auto-routed to extension probe):
      pfmg learn sdk probe -s org.freedesktop.Sdk.Extension.node24 -V 25.08

    Requires flatpak to be installed. Uses flatpak build-init + flatpak build.
    For extensions the base SDK must also be installed.

    Written to:
      Base SDK:  pfmg/data/sdk-profiles/<sdk name>.<sdk version>.toml
      Extension: pfmg/data/ext-profiles/<shortname>.<sdk version>.toml
    """
    

    prober = Prober(
        output_dir=output_dir,        
        no_cleanup=no_cleanup,
    )
    if not prober.is_available():
        rprint("[red]flatpak not found. Install with your package manager.[/red]")
        raise typer.Exit(1)

    # Show what we're about to do
    if _is_extension(target):

        
        _cmd_probe_ext(target, target_version, output_dir, 
                       no_cleanup=no_cleanup, prober=prober)
        return
    
    _cmd_probe_sdk(target, target_version, output_dir, no_cleanup=no_cleanup, prober=prober)
        

def _cmd_probe_sdk(
    sdk: str ,
    sdk_version: str,
    output_dir: Optional[Path],    
    prober: Prober, 
    no_cleanup: bool = False,   
    ):

    """Probe a Flatpak SDK and update its profile."""
    
    with console.status(f"[bold green]Probing {sdk}..."):
        result = prober.probe_sdk(sdk, sdk_version)

    if result.success:
        rprint(f"[bold green]OK[/bold green] — {result.sdk_id}//{result.sdk_version}")
        rprint(f"  pkg-config : {len(result.pkgconfig)}")
        rprint(f"  libraries  : {len(result.libraries)}")
        rprint(f"  executables: {', '.join(result.executables[:8])}"
               + (f" +{len(result.executables)-8} more" if len(result.executables) > 8 else ""))
    else:
        rprint(f"[bold red]Failed[/bold red]: {result.error}")
        raise typer.Exit(1)

def _cmd_probe_ext(
    ext: str,
    ext_version: str,
    output_dir: Optional[Path],        
    prober: Prober,
    no_cleanup: bool = False,
):
    """Probe a Flatpak SDK extension and update its extension profile."""      

    with console.status(f"[bold green]Probing {ext}..."):
        result = prober.probe_ext(ext, ext_version)

    if result.success:
        rprint(f"[bold green]OK[/bold green] — {ext}")
        rprint(f"  executables: {result.executables}")
        rprint(f"  pkg-config : {len(result.pkgconfig)}")
    else:
        rprint(f"[red]Failed: {result.error}[/red]")
        raise typer.Exit(1)

@learn_app.command("list")
def cmd_sdk_list():
    """List available SDK profile TOMLs."""
    from pfmg.resolvers.sdk_capability import _BUILTIN_PROFILES_DIR
    profiles = sorted(_BUILTIN_PROFILES_DIR.glob("**/*.toml"))
    table = Table(title=f"SDK profiles ({len(profiles)})")
    table.add_column("SDK", style="cyan")
    table.add_column("Version")
    for p in profiles:
        table.add_row(p.parent.name, p.stem)
    console.print(table)


# ---------------------------------------------------------------------------
# pfmg learn stats
# ---------------------------------------------------------------------------

@learn_app.command("stats")
def cmd_stats(
    repo_root: Path = typer.Option(_DEFAULT_REPO_ROOT, "--repo-root", "-r"),
):
    """Show counts of recipes and data files."""
    def _count(directory: Path, glob: str) -> int:
        if not directory.exists():
            return 0
        return len(list(directory.glob(glob)))

    rprint(f"\n[bold]pfmg repository stats[/bold] ({repo_root.resolve()})")
    rprint(f"  recipes/native/   : {_count(repo_root/'recipes'/'native',  '*.yaml')}")
    rprint(f"  recipes/python/   : {_count(repo_root/'recipes'/'python',  '*.yaml')}")
    rprint(f"  sdk-profiles      : {_count(repo_root/'pfmg'/'data'/'sdk-profiles', '**/*.toml')}")
    rprint(f"  extension-profiles: {_count(repo_root/'pfmg'/'data'/'extension-profiles', '*.toml')}")
    rprint(f"  native-hints      : {_count(repo_root/'pfmg'/'data'/'native-hints', '*.toml')}")
    rprint("")
    rprint("  [dim]Note: extensions are data (extension-profiles/), not recipes.[/dim]")

