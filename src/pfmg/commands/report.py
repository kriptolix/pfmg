import typer
from pathlib import Path
from rich import print as rprint

_DEFAULT_REPO_ROOT = Path(".")

def cmd_stats(
    repo_root: Path = typer.Option(_DEFAULT_REPO_ROOT, "--repo-root", "-r"),
):
    """Show counts of recipes and data files."""

    def _count(directory: Path, glob: str) -> int:
        if not directory.exists():
            return 0
        return len(list(directory.glob(glob)))

    data = (repo_root / "pfmg" / "data").resolve()
    
    rprint(f"\n[bold]pfmg repository stats[/bold] ({repo_root.resolve()})")
    rprint(f"  data/nat-recipes/  : {_count(data / 'nat-recipes',  '*.json')}")
    rprint(f"  data/pip-recipes/  : {_count(data / 'pip-recipes',  '*.json')}")
    rprint(f"  data/sdk-profiles/ : {_count(data / 'sdk-profiles', '*.json')}")
    rprint(f"  data/ext-profiles/ : {_count(data / 'ext-profiles', '*.json')}")
    rprint("")