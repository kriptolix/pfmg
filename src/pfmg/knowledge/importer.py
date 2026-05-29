"""
pfmg.learn.importer
~~~~~~~~~~~~~~~~~~~~
ModulesImporter — imports modules from individual Flatpak module or Flatpak
manifests (JSON or YAML) from a local directory and writes them as recipe
JSON files under data/nat-recipes/ or data/pip-recipes/.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pfmg.utils.io import load_json_or_yaml, write_json
from pfmg.utils.text import normalise_id
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)


class ImportResult(Enum):
    ALREADY_EXISTS = "already_exists"
    INVALID_SOURCE = "invalid_source"


@dataclass
class ImportReport:
    """Summary of a modules import run."""
    scanned: int = 0
    imported: int = 0
    skipped_existing: int = 0
    skipped_no_source: int = 0
    errors: list[str] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)


class ModulesImporter:
    """
    Converts Flatpak module JSON/YAML files into pfmg recipe JSON files.

    Completely standalone — no pipeline or knowledge graph dependency.

    Recipe files are written to:
      <repo_root>/data/nat-recipes/  — for native builds
      <repo_root>/data/pip-recipes/  — for pip-installed Python packages
    """

    def __init__(self, repo_root: Path):
        data = repo_root / "src" / "pfmg" / "data"
        self.nat_recipes_dir = data / "nat-recipes"
        self.pip_recipes_dir = data / "pip-recipes"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def import_from(self, directory: Path) -> ImportReport:
        """
        Scan *directory* recursively for *.json / *.yaml / *.yml files and
        attempt to parse each as a Flatpak manifest or a standalone module.

        Files that don't match either shape are silently skipped.
        """
        report = ImportReport()
        seen: set[str] = set()

        for pattern in ("**/*.json", "**/*.yaml", "**/*.yml"):
            for p in sorted(directory.glob(pattern)):
                key = str(p.resolve())
                if key in seen:
                    continue
                seen.add(key)

                try:
                    data = load_json_or_yaml(p)
                except Exception as exc:
                    logger.debug("Could not load %s: %s", p, exc)
                    continue

                if not isinstance(data, dict):
                    continue

                # Full Flatpak manifest — import all modules except the last
                # (the last module is the application itself, not a dependency)
                if "modules" in data:
                    modules = data.get("modules", [])
                    if isinstance(modules, list):
                        for index, module in enumerate(modules):
                            # Skip the last module (the app target itself)
                            if len(modules) == 1 or index == len(modules) - 1:
                                continue
                            self._process_module(module, report)
                    continue

                # Possible standalone module file
                if _looks_like_module(data):
                    self._process_module(data, report)

        logger.info(
            "modules import: %d scanned, %d imported, "
            "%d skipped (existing), %d skipped (no source)",
            report.scanned, report.imported,
            report.skipped_existing, report.skipped_no_source,
        )
        return report

    def import_module(self, mod: dict) -> "ImportResult | Path":
        """
        Import a single module dict as a recipe.

        Returns the path of the created recipe file, or an ImportResult
        enum value if the module was skipped.
        """
        name = mod.get("name", "")
        recipe_id = normalise_id(name)

        category = _classify_module(mod)
        dest_dir = self.pip_recipes_dir if category == "python" else self.nat_recipes_dir

        # Check for any existing recipe with this id (any version)
        existing = list(dest_dir.glob(f"{recipe_id}-*.json"))
        if existing:
            logger.debug("Skipping %s — recipe already exists", recipe_id)
            return ImportResult.ALREADY_EXISTS

        sources = _extract_sources(mod)
        if not sources:
            logger.debug("Skipping %s — no archive source with URL", recipe_id)
            return ImportResult.INVALID_SOURCE

        version = _extract_version(sources[0].get("url", ""))

        recipe = {
            "header": {
                "id": recipe_id,
                "version": version,
                "type": category,
                "build_requires": [],
            },
            "module": mod,
        }

        filename = f"{recipe_id}-{version}.json"
        recipe_path = dest_dir / filename
        write_json(recipe_path, recipe, mkdir=True)
        logger.info("Created recipe: %s", recipe_path)
        return recipe_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_module(self, module: dict, report: ImportReport) -> None:
        """Helper that updates *report* after calling import_module."""
        report.scanned += 1
        result = self.import_module(module)

        if result == ImportResult.ALREADY_EXISTS:
            report.skipped_existing += 1
        elif result == ImportResult.INVALID_SOURCE:
            report.skipped_no_source += 1
        else:
            report.created.append(result)
            report.imported += 1

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_PIP_PATTERN = re.compile(
    r'\b(?:python(?:3)?\s+-m\s+)?pip(?:3)?\s+install\b',
    re.IGNORECASE,
)

def _classify_module(mod: dict) -> str:
    """Return "python" if the module uses pip install, otherwise "native"."""
    for command in mod.get("build-commands", []):
        if _PIP_PATTERN.search(command):
            return "python"
    return "native"

def _extract_sources(mod: dict) -> list[dict]:
    """Return all sources that have a URL."""
    return [
        s.copy()
        for s in mod.get("sources", [])
        if isinstance(s, dict) and s.get("url")
    ]

def _extract_version(url: str) -> str:
    """Extract a semver-style version from a URL, falling back to 'unknown'."""
    m = re.search(r'(?:^|[/_-])v?(\d+(?:\.\d+)+)', url)
    return m.group(1) if m else "unknown"

def _looks_like_module(data: dict) -> bool:
    """Return True if *data* looks like a Flatpak module (not a full manifest)."""
    has_name = bool(data.get("name"))
    has_build = bool(
        data.get("buildsystem") or data.get("sources") or data.get("build-commands")
    )
    no_appid = "app-id" not in data and "id" not in data
    return has_name and has_build and no_appid
