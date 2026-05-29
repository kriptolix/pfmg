"""
pfmg.resolve.resolver_cmd
~~~~~~~~~~~~~~~~~~~~~~~~~~
Command-line interface for searching and resolving missing dependencies 
against the local dataset.
"""

from __future__ import annotations

from typing import Optional

import typer
import json
from rich.console import Console
from rich.table import Table
from rich.syntax import Syntax
from rich import print as rprint

from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

console = Console()

def cmd_resolve_errors(
    missing: list[str] = typer.Argument(
        ..., help="Names to resolve: .so names, pkgconfig names, Python package names.",
    ),
    error_type: Optional[str] = typer.Option(
        None, "--type", "-t",
        help="Force error type: native, pkgconfig, header, executable, python, import.",
    ),
    show_module: bool = typer.Option(
        False, "--module", "-m", help="Print full recipe module JSON.",
    ),
):
    """
    Resolve missing dependency names against the local dataset.

    The error type is auto-detected from the name when --type is omitted:
      *.so / *.so.N  → missing native library
      *.h            → missing header
      everything else → tried as both pkgconfig and python package

    Examples:

      pfmg resolve errors libssl.so.3
      pfmg resolve errors openssl --type pkgconfig
      pfmg resolve errors numpy pillow --type python
      pfmg resolve errors clang --module
    """
    from pfmg.resolution.resolvers import ProfileIndex, resolve
    from pfmg.utils.models import SandboxError, SandboxErrorType

    _TYPE_MAP = {
        "native":     SandboxErrorType.MISSING_NATIVE_DEP,
        "pkgconfig":  SandboxErrorType.MISSING_PKGCONFIG,
        "header":     SandboxErrorType.MISSING_HEADER,
        "executable": SandboxErrorType.MISSING_EXECUTABLE,
        "python":     SandboxErrorType.MISSING_PYTHON_PKG,
        "import":     SandboxErrorType.IMPORT_ERROR,
    }

    if error_type and error_type not in _TYPE_MAP:
        rprint(f"[red]Unknown type '{error_type}'. Choose from: {', '.join(_TYPE_MAP)}[/red]")
        raise typer.Exit(1)

    def _auto_errors(name: str) -> list[SandboxError]:
        if ".so" in name:
            return [SandboxError(error_type=SandboxErrorType.MISSING_NATIVE_DEP,
                                 missing=name, source="cli")]
        if name.endswith(".h"):
            return [SandboxError(error_type=SandboxErrorType.MISSING_HEADER,
                                 missing=name, source="cli")]
        # Ambiguous — try both pkgconfig and python
        return [
            SandboxError(error_type=SandboxErrorType.MISSING_PKGCONFIG,
                         missing=name, source="cli"),
            SandboxError(error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                         missing=name, source="cli"),
        ]

    errors: list[SandboxError] = []
    for name in missing:
        if error_type:
            errors.append(SandboxError(
                error_type=_TYPE_MAP[error_type], missing=name, source="cli",
            ))
        else:
            errors.extend(_auto_errors(name))

    suggestions = resolve(errors, ProfileIndex())

    if not suggestions:
        rprint(f"[yellow]No resolution found for: {', '.join(missing)}[/yellow]")
        rprint("  Run [bold]pfmg learn import[/bold] or [bold]pfmg learn inspect[/bold] to grow the dataset.")
        raise typer.Exit(0)

    render_suggestions(suggestions, show_module=show_module)

def cmd_resolve_list(
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k", help="Filter: nat, pip, sdk, ext",
    ),
    limit: int = typer.Option(50, "--limit", "-n"),
):
    """
    List available entries in the local dataset.

    Examples:

      pfmg resolve list
      pfmg resolve list --kind nat
      pfmg resolve list --kind pip --limit 100
    """
    from pfmg.resolution.resolvers import ProfileIndex

    index = ProfileIndex()

    if kind in (None, "nat"):
        _list_recipes(index.nat_recipes, "nat-recipes", limit)
    if kind in (None, "pip"):
        _list_recipes(index.pip_recipes, "pip-recipes", limit)
    if kind in (None, "sdk"):
        t = Table(title="SDK profiles", show_header=True)
        t.add_column("SDK ID",      style="cyan")
        t.add_column("Version")
        t.add_column("pkgconfig",   justify="right")
        t.add_column("libraries",   justify="right")
        t.add_column("executables", justify="right")
        for s in index.sdks[:limit]:
            t.add_row(s.sdk_id, s.sdk_version,
                      str(len(s.pkgconfig)), str(len(s.libraries)), str(len(s.executables)))
        console.print(t)
    if kind in (None, "ext"):
        t = Table(title="Extension profiles", show_header=True)
        t.add_column("Extension ID", style="magenta")
        t.add_column("Version")
        t.add_column("executables",  justify="right")
        t.add_column("pkgconfig",    justify="right")
        t.add_column("libraries",    justify="right")
        for e in index.extensions[:limit]:
            t.add_row(e.extension_id, e.version,
                      str(len(e.executables)), str(len(e.pkgconfig)), str(len(e.libraries)))
        console.print(t)

def _list_recipes(recipes, title: str, limit: int) -> None:
    t = Table(title=title, show_header=True)
    t.add_column("ID",      style="green")
    t.add_column("Version")
    t.add_column("Path",    style="dim")
    for r in recipes[:limit]:
        t.add_row(r.recipe_id, r.version, str(r.path))
    if len(recipes) > limit:
        t.caption = f"Showing {limit} of {len(recipes)} — use --limit to see more"
    console.print(t)

def render_suggestions(suggestions, show_module: bool = False) -> None:
    from pfmg.resolution.resolvers import ProviderKind

    if not suggestions:
        rprint("[yellow]No resolutions found in local dataset.[/yellow]")
        return

    _KIND_LABEL = {
        ProviderKind.SDK:        "[blue]sdk[/blue]",
        ProviderKind.EXTENSION:  "[magenta]extension[/magenta]",
        ProviderKind.NAT_RECIPE: "[yellow]nat-recipe[/yellow]",
        ProviderKind.PIP_RECIPE: "[green]pip-recipe[/green]",
    }

    by_missing: dict[str, list] = {}
    for s in suggestions:
        by_missing.setdefault(s.error_missing, []).append(s)

    for missing, group in by_missing.items():
        rprint(f"\n[bold]Resolutions for [cyan]{missing}[/cyan]:[/bold]")
        t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        t.add_column("Kind",       style="dim",   no_wrap=True, min_width=12)
        t.add_column("Provider",   style="cyan",  min_width=30)
        t.add_column("Version",    style="green", min_width=8)
        t.add_column("Matched on", style="dim")
        for s in group:
            t.add_row(_KIND_LABEL.get(s.provider_kind, s.provider_kind.value),
                      s.provider_id, s.provider_version, s.matched_on)
        console.print(t)

        if show_module:
            for s in group:
                if s.module:
                    rprint(f"  [dim]{s.provider_id}=={s.provider_version}:[/dim]")
                    console.print(Syntax(
                        json.dumps(s.module, indent=2, ensure_ascii=False),
                        "json", theme="monokai",
                    ))
                if s.env:
                    rprint(f"  [dim]env:[/dim] {s.env}")
