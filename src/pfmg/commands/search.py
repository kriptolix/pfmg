import typer

from typing import Optional
from rich.console import Console
from rich import print as rprint
from rich.table import Table
from rich.syntax import Syntax
import json

console = Console()

def cmd_search(
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

      pfmg search openssl
      pfmg search numpy --kind pip
      pfmg search libz  --kind nat
      pfmg search clang --kind ext --module
      pfmg search absl  --kind ext
    """
    from pfmg.resolution.profiles import ProfileIndex, ProviderKind
    from pfmg.resolution.matchers import sdk_matches_query, ext_matches_query

    index = ProfileIndex()
    found = False

    # --- SDK profiles ---
    if kind in (None, "sdk"):
        hits = []
        for sdk in index.sdks:
            for field_label, matched_value in sdk_matches_query(query, sdk):
                hits.append((sdk.sdk_id, sdk.sdk_version, f"{field_label}: {matched_value}"))
        if hits:
            found = True
            t = Table(title=f"SDK profiles — [cyan]{query}[/cyan]", show_header=True)
            t.add_column("SDK",        style="cyan")
            t.add_column("Version")
            t.add_column("Matched on", style="dim")
            for row in hits:
                t.add_row(*row)
            console.print(t)

    # --- Extension profiles ---
    if kind in (None, "ext"):
        hits = []
        for ext in index.extensions:
            for field_label, matched_value in ext_matches_query(query, ext):
                hits.append((ext.extension_id, ext.version, f"{field_label}: {matched_value}"))
        if hits:
            found = True
            t = Table(title=f"Extensions — [cyan]{query}[/cyan]", show_header=True)
            t.add_column("Extension ID", style="magenta")
            t.add_column("Version")
            t.add_column("Matched on",   style="dim")
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