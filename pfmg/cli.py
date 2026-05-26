from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.syntax import Syntax
from rich import print as rprint

from pfmg import __version__
from pfmg.learn.learn import learn_app
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

app = typer.Typer(
    name="pfmg",
    help="Python Flatpak Manifest Resolver — modern replacement for flatpak-pip-generator.",
    rich_markup_mode="rich",
)
console = Console()

app.add_typer(learn_app, name="learn")


# ---------------------------------------------------------------------------
# pfmg version
# ---------------------------------------------------------------------------

@app.command("version")
def cmd_version():
    """Print pfmg version."""
    rprint(f"pfmg [bold]{__version__}[/bold]")


# ---------------------------------------------------------------------------
# pfmg probe
# ---------------------------------------------------------------------------

probe_app = typer.Typer(
    name="probe",
    help="Probe Python packages inside a Flatpak build sandbox.",
    rich_markup_mode="rich",
)
app.add_typer(probe_app, name="probe")


@probe_app.command("run")
def cmd_probe_run(
    packages: list[str] = typer.Argument(
        ...,
        help="Package specs to probe, e.g. numpy==1.26.0 or just numpy.",
    ),
    sdk: str = typer.Option("org.freedesktop.Sdk", "--sdk", "-s"),
    runtime: str = typer.Option("org.freedesktop.Platform", "--runtime"),
    sdk_version: str = typer.Option("24.08", "--sdk-version", "-V"),
    sdk_extensions: Optional[list[str]] = typer.Option(
        None, "--extension", "-e",
        help="SDK extension IDs to activate (repeat for multiple).",
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o",
        help="Write generated module JSON files here.",
    ),
    fmt: str = typer.Option("json", "--format", "-f", help="Output format: json or yaml."),
    use_uv: bool = typer.Option(True, "--uv/--pip", help="Use uv (default) or pip."),
    keep: bool = typer.Option(False, "--keep", help="Keep the sandbox work directory."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    raw_output: bool = typer.Option(False, "--raw-output", "-r", help="Print raw sandbox stdout/stderr."),
):
    """
    Probe one or more Python packages inside a real Flatpak build sandbox.

    Installs each package, tests the import, runs ldd on .so files, and checks
    pkg-config for declared native dependencies. Generates a Flatpak module JSON
    for each successfully probed package.

    Examples:

      pfmg probe run numpy==1.26.0
      pfmg probe run cryptography --extension org.freedesktop.Sdk.Extension.rust-stable
      pfmg probe run pillow lxml --sdk-version 24.08 --output-dir ./modules
    """
    import os
    if verbose:
        os.environ["PFMG_LOG_LEVEL"] = "DEBUG"

    from pfmg.probe import BuildSandboxProber
    from pfmg.models import ResolvedPackage

    resolved: list[ResolvedPackage] = []
    for spec in packages:
        if "==" in spec:
            name, version = spec.split("==", 1)
        else:
            name, version = spec, ""
        resolved.append(ResolvedPackage(name=name.strip(), version=version.strip()))

    prober = BuildSandboxProber(
        sdk=sdk,
        runtime=runtime,
        runtime_version=sdk_version,
        sdk_extensions=sdk_extensions or [],
        keep_work_dir=keep,
        use_uv=use_uv,
    )

    if not prober.is_available():
        rprint("[red]flatpak not found. Install with your package manager.[/red]")
        raise typer.Exit(1)

    pkg_label = ", ".join(p.name for p in resolved)
    with console.status(f"[bold green]Probing {pkg_label}..."):
        report = prober.probe(resolved)

    rprint(f"\n[bold]Probe report[/bold] — {pkg_label}")
    if not report.ran:
        rprint(f"  [yellow]Skipped:[/yellow] {report.skip_reason}")
        raise typer.Exit(1)

    rprint(f"  SDK sufficient   : {'[green]yes[/green]' if report.sdk_sufficient else '[red]no[/red]'}")
    rprint(f"  Build possible   : {'[green]yes[/green]' if report.build_possible else '[red]no[/red]'}")
    rprint(f"  Modules generated: {len(report.modules)}")

    if report.errors:
        rprint(f"\n[bold red]Errors ({len(report.errors)}):[/bold red]")
        t = Table(show_header=True, header_style="bold")
        t.add_column("Type",       style="red",   no_wrap=True)
        t.add_column("Missing",    style="yellow")
        t.add_column("Context",    style="dim")
        t.add_column("Raw line",   style="dim")
        for err in report.errors:
            t.add_row(
                err.error_type.value,
                err.missing,
                err.context,
                err.raw_line[:120] if err.raw_line else "",
            )
        console.print(t)

    for label, items in [
        ("Missing native libs", report.missing_native_libs),
        ("Missing pkg-config",  report.missing_pkgconfig),
        ("Missing headers",     report.missing_headers),
        ("Missing Python pkgs", report.missing_python_packages),
    ]:
        if items:
            rprint(f"[yellow]{label}:[/yellow] {', '.join(items)}")

    # Raw sandbox output — only shown when explicitly requested via --raw-output.
    # Use --raw-output to inspect the full flatpak-builder stderr when the
    # structured error table doesn't explain the failure.
    if raw_output and (report.stdout.strip() or report.stderr.strip()):
        rprint("\n[bold]Raw sandbox output:[/bold]")
        _print_sandbox_output(report.stdout, report.stderr)

    has_failure = not report.build_possible or not report.sdk_sufficient or report.errors

    if report.errors:
        rprint("\n[bold]Resolving errors against local dataset...[/bold]")
        _print_suggestions(report.errors)
    elif has_failure and not report.errors:
        rprint(
            "\n[yellow]Build failed but no structured errors were recognised.\n"
            "Check the raw output above for clues.[/yellow]"
        )

    if report.modules:
        if output_dir:
            written = prober.write_modules(report, output_dir, fmt=fmt)
            rprint(f"\n[bold green]Wrote {len(written)} module(s) to {output_dir}[/bold green]")
            for p in written:
                rprint(f"  {p}")
        else:
            rprint("\n[bold]Generated modules (use --output-dir to save):[/bold]")
            for name, module in report.modules.items():
                rprint(f"\n  [cyan]{name}[/cyan]")
                console.print(Syntax(
                    json.dumps(module, indent=2), "json", theme="monokai",
                ))


@probe_app.command("errors")
def cmd_probe_errors(
    stderr_file: Path = typer.Argument(
        ..., help="File containing stderr from a failed build.",
    ),
    stdout_file: Optional[Path] = typer.Option(None, "--stdout"),
    ldd_file:    Optional[Path] = typer.Option(None, "--ldd"),
    context: str = typer.Option("", "--context", "-c"),
    no_resolve: bool = typer.Option(False, "--no-resolve"),
):
    """
    Parse build errors from a captured stderr file and suggest resolutions.

    Useful for analysing failures from an existing flatpak-builder run
    without re-running the full probe.

    Example:

      pfmg probe errors build-stderr.txt --stdout build-stdout.txt
    """
    from pfmg.sandbox.errors import parse_errors

    if not stderr_file.exists():
        rprint(f"[red]File not found: {stderr_file}[/red]")
        raise typer.Exit(1)

    stderr = stderr_file.read_text(errors="replace")
    stdout = stdout_file.read_text(errors="replace") if stdout_file else ""
    ldd    = ldd_file.read_text(errors="replace")    if ldd_file    else ""

    errors = parse_errors(stderr, stdout=stdout, ldd_output=ldd, context=context)

    if not errors:
        rprint("[green]No recognisable errors found.[/green]")
        raise typer.Exit(0)

    rprint(f"\n[bold]Found {len(errors)} error(s):[/bold]")
    t = Table(show_header=True, header_style="bold")
    t.add_column("Type",     style="red",    no_wrap=True)
    t.add_column("Missing",  style="yellow")
    t.add_column("Source",   style="dim")
    t.add_column("Raw line", style="dim")
    for err in errors:
        t.add_row(err.error_type.value, err.missing, err.source,
                  err.raw_line[:120] if err.raw_line else "")
    console.print(t)

    if not no_resolve:
        _print_suggestions(errors)


# ---------------------------------------------------------------------------
# pfmg resolve
# ---------------------------------------------------------------------------

resolve_app = typer.Typer(
    name="resolve",
    help="Query the local recipe and profile dataset.",
    rich_markup_mode="rich",
)
app.add_typer(resolve_app, name="resolve")


@resolve_app.command("search")
def cmd_resolve_search(
    query: str = typer.Argument(..., help="Library, package, or recipe name to search for."),
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k",
        help="Filter by kind: nat, pip, sdk, ext  (default: all).",
    ),
    show_module: bool = typer.Option(
        False, "--module", "-m", help="Print the full module JSON for matching recipes.",
    ),
):
    """
    Search the local dataset for a library, package, or recipe name.

    Searches SDK profiles, extension profiles, nat-recipes, and pip-recipes
    using the same fuzzy matching as the error resolver.

    Examples:

      pfmg resolve search openssl
      pfmg resolve search numpy --kind pip
      pfmg resolve search libz  --kind nat
      pfmg resolve search clang --kind ext --module
    """
    from pfmg.resolvers import ProfileIndex, ProviderKind

    index = ProfileIndex()
    found = False
    q = query.lower()

    # --- SDK profiles ---
    if kind in (None, "sdk"):
        hits = []
        for sdk in index.sdks:
            for field, items in [
                ("library",    sdk.libraries),
                ("pkgconfig",  sdk.pkgconfig),
                ("executable", sdk.executables),
            ]:
                matched = [i for i in items if q in i.lower()]
                for m in matched:
                    hits.append((sdk.sdk_id, sdk.sdk_version, f"{field}: {m}"))
        if hits:
            found = True
            t = Table(title=f"SDK profiles — [cyan]{query}[/cyan]", show_header=True)
            t.add_column("SDK",     style="cyan")
            t.add_column("Version")
            t.add_column("Matched on", style="dim")
            for row in hits:
                t.add_row(*row)
            console.print(t)

    # --- Extension profiles ---
    if kind in (None, "ext"):
        hits = []
        for ext in index.extensions:
            for field, items in [
                ("executable", ext.executables),
                ("pkgconfig",  ext.pkgconfig),
                ("library",    ext.libraries),
            ]:
                matched = [i for i in items if q in i.lower()]
                for m in matched:
                    hits.append((ext.extension_id, ext.version, f"{field}: {m}"))
        if hits:
            found = True
            t = Table(title=f"Extensions — [cyan]{query}[/cyan]", show_header=True)
            t.add_column("Extension ID", style="magenta")
            t.add_column("Version")
            t.add_column("Matched on", style="dim")
            for row in hits:
                t.add_row(*row)
            console.print(t)

    # --- Recipes ---
    if kind not in ("sdk", "ext"):
        recipe_kind = (
            ProviderKind.NAT_RECIPE if kind == "nat" else
            ProviderKind.PIP_RECIPE if kind == "pip" else
            None
        )
        recipes = index.search_recipes(query, kind=recipe_kind)
        if recipes:
            found = True
            t = Table(title=f"Recipes — [cyan]{query}[/cyan]", show_header=True)
            t.add_column("ID",      style="green")
            t.add_column("Version")
            t.add_column("Type",    style="dim")
            t.add_column("Path",    style="dim")
            for r in recipes:
                t.add_row(r.recipe_id, r.version, r.kind.value, str(r.path))
            console.print(t)

            if show_module:
                for r in recipes:
                    rprint(f"\n[bold green]{r.recipe_id}=={r.version}[/bold green] ({r.kind.value})")
                    console.print(Syntax(
                        json.dumps(r.module, indent=2, ensure_ascii=False),
                        "json", theme="monokai",
                    ))

    if not found:
        rprint(f"[yellow]No results for '[bold]{query}[/bold]'.[/yellow]")
        rprint("  Run [bold]pfmg learn import[/bold] or [bold]pfmg learn inspect[/bold] to grow the dataset.")


@resolve_app.command("errors")
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
    from pfmg.resolvers import ProfileIndex, resolve
    from pfmg.models import SandboxError, SandboxErrorType

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

    _render_suggestions(suggestions, show_module=show_module)


@resolve_app.command("list")
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
    from pfmg.resolvers import ProfileIndex

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


# ---------------------------------------------------------------------------
# Shared rendering helpers
# ---------------------------------------------------------------------------

def _print_sandbox_output(stdout: str, stderr: str) -> None:
    """Print stdout and stderr from the sandbox, trimmed to the useful tail."""
    # Show up to last 80 lines of each stream — the tail is where errors live.
    def _tail(text: str, n: int = 80) -> str:
        lines = text.splitlines()
        if len(lines) > n:
            omitted = len(lines) - n
            return f"  [dim]... ({omitted} lines omitted) ...[/dim]\n" + "\n".join(lines[-n:])
        return "\n".join(lines)

    if stdout.strip():
        rprint("[dim]─── stdout ────────────────────────────────[/dim]")
        console.print(_tail(stdout), highlight=False, markup=False)

    if stderr.strip():
        rprint("[dim]─── stderr ────────────────────────────────[/dim]")
        # Highlight lines that look like errors for readability
        for line in stderr.splitlines()[-80:]:
            stripped = line.strip()
            if any(kw in stripped.lower() for kw in (
                "error:", "fatal:", "not found", "cannot find",
                "no such file", "failed", "undefined reference",
            )):
                console.print(f"  [red]{line}[/red]", highlight=False, markup=False)
            elif any(kw in stripped.lower() for kw in ("warning:", "note:")):
                console.print(f"  [yellow]{line}[/yellow]", highlight=False, markup=False)
            else:
                console.print(f"  {line}", highlight=False, markup=False)
    rprint("[dim]───────────────────────────────────────────[/dim]")


def _print_suggestions(errors) -> None:
    from pfmg.resolvers import ProfileIndex, resolve
    _render_suggestions(resolve(errors, ProfileIndex()))


def _render_suggestions(suggestions, show_module: bool = False) -> None:
    from pfmg.resolvers import ProviderKind

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app()


if __name__ == "__main__":
    main()