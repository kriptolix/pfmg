"""
pfmg.learn.importer (ok)
~~~~~~~~~~~~~~~~~~~~~~~~~~
ModulesImporter — imports modules from individual Flatpak module or Flatpak manifests, JSON or YAML, files from a
local directory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import re
from enum import Enum
import yaml


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
    Converts modules JSON files into pfmg recipe YAML files.

    Completely standalone — no pipeline or knowledge graph dependency.
    """

    def __init__(self, repo_root: Path):
        self.recipes_dir = repo_root / "recipes" / "native"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def import_from(
    self,
    directory: Path,
    ) -> ImportReport:
        """
        Scans for *.json, *.yaml, *.yml and attempts to parse each as a
        Flatpak manifest or a shared module. Files that don't look like any
        of these two are silently ignored.
        """
        patterns = (
            ["**/*.json", "**/*.yaml", "**/*.yml"]            
        )

        report = ImportReport()        

        seen: set[str] = set()

        for pattern in patterns:
            for p in sorted(directory.glob(pattern)):
                key = str(p.resolve())

                if key in seen:
                    continue

                seen.add(key)

                try:
                    data = self._load(p)
                except Exception:
                    continue

                if not isinstance(data, dict):
                    continue

                # Manifesto Flatpak
                if "modules" in data:
                    modules = data.get("modules", [])

                    if isinstance(modules, list):
                         for index, module in enumerate(modules):

                            if len(modules) == 1 or index == len(modules) - 1:
                                continue                            

                            result = self.import_module(module)
                            report.scanned += 1

                            if result == ImportResult.ALREADY_EXISTS:
                                report.skipped_existing += 1
                                continue

                            if result == ImportResult.INVALID_SOURCE:
                                report.skipped_no_source += 1
                                continue
                            
                            report.created.append(result)
                            report.imported += 1

                    continue

                # Possível módulo separado
                if _looks_like_module(data):
                    
                    result = self.import_module(data)
                    report.scanned += 1

                    if result == ImportResult.ALREADY_EXISTS:
                        report.skipped_existing += 1
                        continue

                    if result == ImportResult.INVALID_SOURCE:
                        report.skipped_no_source += 1
                        continue
                    
                    report.created.append(result)
                    report.imported += 1

        logger.info(
            "modules import: %d scanned, %d imported, "
            "%d skipped (existing), %d skipped (no source)",
            report.scanned, report.imported,
            report.skipped_existing, report.skipped_no_source,
        )
        return report

    def import_module(self, mod: dict) -> ImportResult | Path:
        """
        Import a single module dict as a recipe. Returns the path of the
        created recipe, or None if skipped.
        """       

        name = mod.get("name", "")
        recipe_id = _normalise_id(name)       

        if (self.recipes_dir / f"{recipe_id}.yaml").exists():
            logger.debug("Skipping %s — recipe already exists", recipe_id)
            return ImportResult.ALREADY_EXISTS

        source = self._extract_source(mod)
        if not source:
            logger.debug("Skipping %s — no archive source", recipe_id)
            return ImportResult.INVALID_SOURCE

        recipe = self._build_recipe(recipe_id, mod, source)

        version = recipe["header"]["version"]        
        filename = f'{recipe_id}-{version}'

        recipe_path = self.recipes_dir / f"{filename}.json"
        self.recipes_dir.mkdir(parents=True, exist_ok=True)
        recipe_path.write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Created recipe: %s", recipe_path)
        return recipe_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    @staticmethod
    def _load(path: Path) -> dict:
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(text) or {}
        return json.loads(text)        

    @staticmethod
    def _extract_source(mod: dict) -> list[dict]:
        sources = mod.get("sources", [])
        valid_sources = []

        for source in sources:
            if not isinstance(source, dict):
                continue

            if not source.get("url"):
                continue

            valid_sources.append(source.copy())

        return valid_sources

    @staticmethod
    def _build_recipe(
        recipe_id: str,        
        mod: dict,
        source: list,
    ) -> dict:

        pattern = re.compile(
            r'\b(?:python(?:3)?\s+-m\s+)?pip(?:3)?\s+install\b',
            re.IGNORECASE
        )

        category = "native"

        # build = mod.get("name", "")
        build = mod.get("build-commands", [])

        if build:
            for command in build:
                if pattern.search(command):
                    category = "python"
                    break        

        version = re.search(r'(?:^|[/_-])v?(\d+(?:\.\d+)+)', source[0]["url"])
        version = version.group(1) if version else "unknown"

        recipe: dict = {
            "header":{
                "id": recipe_id,
                "version": version,
                "type": category,
            },
            "module": mod               
        }        

        return recipe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_module(data: dict) -> bool:
    """Return True if the dict looks like a Flatpak module (not a full manifest)."""
    has_name = bool(data.get("name"))
    has_build = bool(data.get("buildsystem") or data.get("sources") or data.get("build-commands"))
    no_appid = "app-id" not in data and "id" not in data
    return has_name and has_build and no_appid


def _normalise_id(name: str) -> str:
    """Convert a module name to a safe recipe id."""
    return name.lower().replace(" ", "-").replace("_", "-")