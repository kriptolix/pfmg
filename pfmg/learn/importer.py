"""
pfmg.learn.importer (ok)
~~~~~~~~~~~~~~~~~~~~~~~~~~
ModulesImporter — imports individual Flatpak module JSON files from a
local directory.

Unlike full manifests, modules files contain a single module object
(or a list of modules) without the surrounding app-id/runtime envelope:

  {
    "name": "libusb",
    "buildsystem": "autotools",
    "config-opts": ["--disable-static", "--disable-udev"],
    "sources": [
      {
        "type": "archive",
        "url": "https://github.com/libusb/libusb/archive/v1.0.21.tar.gz",
        "sha256": "..."
      }
    ],
    "cleanup": ["/include", "/lib/pkgconfig"]
  }

Or a list at the top level (some files export an array of modules):

  [
    { "name": "libfoo", ... },
    { "name": "libbar", ... }
  ]

This importer:
  1. Recursively scans a directory for *.json files
  2. Identifies files that are modules (has "name" + ("sources" or "buildsystem"))
  3. Converts each module into a NativeRecipe YAML and writes it to recipes/native/
  4. Skips files that already have a recipe to avoid overwriting curated content

The output is directly usable by pfmg's RecipeDB — no knowledge graph step needed.

Usage (standalone)::

    importer = ModulesImporter(repo_root=Path("."))
    report = importer.import_from(Path("/path/to/modules"))
    for r in report.created:
        print("Created:", r)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import re

import yaml


from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

# Module name → pkgconfig name(s) — for modules whose pkg-config name
# differs from the directory/module name.

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
    dry_run: bool = False
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
                        for module in modules:
                            self.import_module(module)

                    continue

                # Possível módulo separado
                if _looks_like_module(data):
                    self.import_module(data)
        
        logger.info(
            "modules import: %d scanned, %d imported, "
            "%d skipped (existing), %d skipped (no source)",
            report.scanned, report.imported,
            report.skipped_existing, report.skipped_no_source,
        )
        return report

    def import_from_old(self, modules_dir: Path, dry_run: bool = False) -> ImportReport:
        """
        Scan modules_dir recursively and import all module files found.

        Args:
            modules_dir: Path to the modules repository root or
                         any directory containing module JSON files.
            dry_run:     If True, report what would happen without writing.
        """
        report = ImportReport()
        existing = {p.stem for p in self.recipes_dir.glob("*.yaml")} if self.recipes_dir.exists() else set()

        for json_path in sorted(modules_dir.rglob("*.json")):
            modules = self._load_modules(json_path)
            if not modules:
                continue

            for mod in modules:
                report.scanned += 1
                name = mod.get("name", "")                

                # Normalise name for use as recipe id
                recipe_id = _normalise_id(name)

                if recipe_id in existing:
                    report.skipped_existing += 1
                    logger.debug("Skipping %s — recipe already exists", recipe_id)
                    continue

                source = self._extract_source(mod)
                if not source:
                    report.skipped_no_source += 1
                    logger.debug("Skipping %s — no archive source", recipe_id)
                    continue

                recipe = self._build_recipe(recipe_id, name, mod, source)
                
                version = recipe.get("version")

                if version:
                    suffix = version
                else:
                    sha256 = recipe["source"].get("sha256", "")
                    suffix = sha256[:8] if sha256 else "unknown"

                filename = f'{recipe["id"]}-{suffix}'

                recipe_path = self.recipes_dir / f"{filename}.yaml"

                if not dry_run:
                    self.recipes_dir.mkdir(parents=True, exist_ok=True)
                    recipe_path.write_text(
                        json.dumps(recipe, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    existing.add(recipe_id)   # prevent duplicates within the same run

                report.created.append(recipe_path)
                report.imported += 1
                logger.info("%s recipe: %s", "Would create" if dry_run else "Created", recipe_path)

        logger.info(
            "modules import: %d scanned, %d imported, "
            "%d skipped (existing), %d skipped (no source)",
            report.scanned, report.imported,
            report.skipped_existing, report.skipped_no_source,
        )
        return report
    
    def import_module(self, mod: dict) -> Optional[Path]:
        """
        Import a single module dict as a recipe. Returns the path of the
        created recipe, or None if skipped.
        """
        name = mod.get("name", "")
        recipe_id = _normalise_id(name)

        if (self.recipes_dir / f"{recipe_id}.yaml").exists():
            logger.debug("Skipping %s — recipe already exists", recipe_id)
            return None

        source = self._extract_source(mod)
        if not source:
            logger.debug("Skipping %s — no archive source", recipe_id)
            return None

        recipe = self._build_recipe(recipe_id, name, mod, source)

        version = recipe.get("version")
        suffix = version if version else "unknown"
        filename = f'{recipe["id"]}-{suffix}'

        recipe_path = self.recipes_dir / f"{filename}.yaml"
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
    def _load_modules(path: Path) -> list[dict]:
        """
        Load a JSON file and return a list of module dicts.
        Handles both single-module objects and top-level arrays.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Could not parse %s: %s", path, exc)
            return []

        if isinstance(data, list):
            # Top-level array of modules
            return [m for m in data if isinstance(m, dict) and _looks_like_module(m)]

        if isinstance(data, dict):
            if _looks_like_module(data):
                return [data]
            # Some files wrap a module in a {"modules": [...]} envelope
            inner = data.get("modules", [])
            if isinstance(inner, list):
                return [m for m in inner if isinstance(m, dict) and _looks_like_module(m)]

        return []

    @staticmethod
    def _extract_source(mod: dict) -> Optional[dict]:
        """Return the first archive source entry, or None."""
        for src in mod.get("sources", []):
            if isinstance(src, dict) and src.get("type") == "archive":
                url = src.get("url", "")
                sha256 = src.get("sha256", "")
                if url:
                    return {"url": url, "sha256": sha256}
        return None

    @staticmethod
    def _build_recipe(
        recipe_id: str,
        original_name: str,
        mod: dict,
        source: dict,
    ) -> dict:
        buildsystem = mod.get("buildsystem", "autotools")
        config_opts = mod.get("config-opts", [])
        cleanup = mod.get("cleanup", ["/include", "/lib/pkgconfig"])

        version = re.search(r'(?:^|[/_-])v?(\d+(?:\.\d+)+)', source["url"])
        version = version.group(1) if version else None

        recipe: dict = {
            "id": recipe_id,
        }

        if version:
            recipe["version"] = version

        recipe["name"] = original_name
        recipe["buildsystem"] = buildsystem

        if config_opts:
            recipe["config-opts"] = config_opts

        recipe.update({            
            "source": {
                "type": "archive",
                "url": source["url"],
            },
        })

        if source.get("sha256"):
            recipe["source"]["sha256"] = source["sha256"]        

        if cleanup:
            recipe["cleanup"] = cleanup

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