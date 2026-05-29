import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich import print as rprint

from pfmg.knowledge.inspector import RuntimeInspector
from pfmg.utils import is_extension

console = Console()

def cmd_inspect(
    target: str = typer.Argument(
        ...,
        help=(
            "SDK or Extension ID. Extensions (containing .Extension.) "
            "are detected automatically."
        ),
    ),
    target_version: str = typer.Option("25.08", "--sdk-version", "-V"),
    ext_version: Optional[str] = typer.Option(
        None, "--ext-version", "-E",
        help=(
            "Version tag used to install the extension when it differs from "
            "--sdk-version.  Example: --ext-version 1.6 for gcc8."
        ),
    ),
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

      # Extension (same version as SDK):
      pfmg inspect org.freedesktop.Sdk.Extension.node24 --sdk-version 25.08

      # Extension with its own version tag:
      pfmg inspect org.freedesktop.Sdk.Extension.gcc8 --sdk-version 24.08 --ext-version 1.6

    Requires flatpak to be installed.  For extensions the base SDK must also
    be installed.

    Written to:
      Base SDK:  data/sdk-profiles/<shortname>.<version>.json
      Extension: data/ext-profiles/<shortname>.<version>.json
    """
    prober = RuntimeInspector(output_dir=output_dir, no_cleanup=no_cleanup)

    if not prober.is_available():
        rprint("[red]flatpak not found. Install with your package manager.[/red]")
        raise typer.Exit(1)

    if is_extension(target):
        _cmd_inspect_ext(target, target_version, prober, ext_version=ext_version)
    else:
        _cmd_inspect_sdk(target, target_version, prober)


def _cmd_inspect_sdk(sdk: str, sdk_version: str, prober: RuntimeInspector) -> None:
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


def _cmd_inspect_ext(
    ext: str,
    sdk_version: str,
    prober: RuntimeInspector,
    *,
    ext_version: Optional[str] = None,
) -> None:
    display_ref = f"{ext}//{ext_version or sdk_version}"
    with console.status(f"[bold green]Inspecting {display_ref}..."):
        result = prober.probe_ext(ext, sdk_version, ext_version)

    if result.success:
        rprint(f"[bold green]OK[/bold green] — {display_ref}")
        rprint(f"  executables: {result.executables}")
        rprint(f"  pkg-config : {len(result.pkgconfig)}")
    else:
        rprint(f"[red]Failed: {result.error}[/red]")
        raise typer.Exit(1)