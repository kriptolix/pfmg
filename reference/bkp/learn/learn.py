"""
pfmg.learn.cli — standalone learning commands.

All commands write directly to recipes/ and data/ — no knowledge graph.
Export is the default behavior for every command.

Commands:
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


from pfmg.learn.importer import ModulesImporter
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
# pfmg learn import
# ---------------------------------------------------------------------------

@learn_app.command("import")
def cmd_import_modules(
    modules_dir: Path = typer.Argument(
        ...,
        help="Path to a cloned shared-modules repo or any dir with module JSON files",
    ),
    repo_root: Path = typer.Option(_DEFAULT_REPO_ROOT, "--repo-root", "-r"),    
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
        report = importer.import_from(modules_dir)

    rprint(f"\n[bold]shared-modules import[/bold]")
    rprint(f"  Scanned           : {report.scanned}")
    rprint(f"  Imported          : {report.imported}")
    rprint(f"  Skipped (exists)  : {report.skipped_existing}")
    rprint(f"  Skipped (no src)  : {report.skipped_no_source}")

    if report.created:
        rprint(f"\n[bold]{'Would create' 'Created'} {len(report.created)} recipe(s):[/bold]")
        for p in report.created:
            rprint(f"  [green]{'(dry)' ''}[/green] {p}")

    if report.errors:
        rprint(f"\n[red]{len(report.errors)} error(s):[/red]")
        for e in report.errors[:5]:
            rprint(f"  {e}")


# ---------------------------------------------------------------------------
# pfmg learn sdk
# ---------------------------------------------------------------------------

@learn_app.command("inspect")
def cmd_import(
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
      Extension: pfmg/data/pip-recipes/<shortname>.<sdk version>.toml
      Extension: pfmg/data/nat-recipes/<shortname>.<sdk version>.toml
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

        
        _cmd_import_ext(target, target_version, output_dir, 
                       no_cleanup=no_cleanup, prober=prober)
        return
    
    _cmd_import_sdk(target, target_version, output_dir, no_cleanup=no_cleanup, prober=prober)
 
def _cmd_import_sdk(
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

def _cmd_import_ext(
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
    from reference.resolvers.sdk_capability import _BUILTIN_PROFILES_DIR
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
    rprint(f"  recipes/native/   : {_count(repo_root/'recipes'/'native',  '*.json')}")
    rprint(f"  recipes/python/   : {_count(repo_root/'recipes'/'python',  '*.json')}")
    rprint(f"  sdk-profiles      : {_count(repo_root/'pfmg'/'data'/'sdk-profiles', '*.json')}")
    rprint(f"  ext-profiles      : {_count(repo_root/'pfmg'/'data'/'ext-profiles', '*.json')}")    
    rprint("")
    rprint("  [dim]Note: extensions are data (extension-profiles/), not recipes.[/dim]")

