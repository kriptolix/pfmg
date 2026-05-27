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
from pfmg.learn.learn_cmd import cmd_import, cmd_inspect, cmd_stats 
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

app = typer.Typer(
    name="pfmg",
    help="Python Flatpak Module Generator — A a more featured replacement for flatpak-pip-generator.",
    rich_markup_mode="rich",
)
console = Console()


app.command("import")(cmd_import)

app.command("inspec")(cmd_inspect)

app.command("stats")(cmd_stats)

# ---------------------------------------------------------------------------
# pfmg version
# ---------------------------------------------------------------------------

@app.command("version")
def cmd_version():
    """Print pfmg version."""
    rprint(f"pfmg [bold]{__version__}[/bold]")

# ---------------------------------------------------------------------------
# pfmg generate
# ---------------------------------------------------------------------------

@app.command("generate")
def cmd_generate(
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

      pfmg generate numpy==1.26.0
      pfmg generate cryptography --extension org.freedesktop.Sdk.Extension.rust-stable
      pfmg generate pillow lxml --sdk-version 24.08 --output-dir ./modules
    """
    import os
    if verbose:
        os.environ["PFMG_LOG_LEVEL"] = "DEBUG"

    from pfmg.probe.probe import BuildSandboxProber
    from pfmg.utils.models import ResolvedPackage

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


@app.command("errors")
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
    from pfmg.resolve.resolvers import ProfileIndex, resolve
    from pfmg.resolve.resolver_cmd import render_suggestions 

    render_suggestions(resolve(errors, ProfileIndex()))

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app()


if __name__ == "__main__":
    main()