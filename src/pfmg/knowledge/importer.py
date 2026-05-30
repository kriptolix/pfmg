"""
pfmg.learn.importer
~~~~~~~~~~~~~~~~~~~~
ModulesImporter — imports modules from individual Flatpak module or Flatpak
manifests (JSON or YAML) from a local directory and writes them as recipe
JSON files under data/nat-recipes/ or data/pip-recipes/.
"""
from __future__ import annotations

import re
import shutil
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
    skipped_no_commands: int = 0
    errors: list[str] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    copied_files: list[Path] = field(default_factory=list)


class ModulesImporter:
    """
    Converts Flatpak module JSON/YAML files into pfmg recipe JSON files.

    Completely standalone — no pipeline or knowledge graph dependency.

    Recipe files are written to:
      <repo_root>/data/nat-recipes/  — for native builds
      <repo_root>/data/pip-recipes/  — for pip-installed Python packages

    Local file sources (``{"type": "file", "path": "..."}`` entries without a
    ``url``) are copied into the same destination directory as the recipe JSON
    so that relative path references remain valid.
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
                # (the last module is the application itself, not a dependency).
                # Each entry in the modules list can be a dict (inline module)
                # or a string (external file reference) — strings are skipped.
                source_dir = p.parent

                if "modules" in data:
                    modules = data.get("modules", [])
                    if isinstance(modules, list):
                        for index, module in enumerate(modules):
                            if not isinstance(module, dict):
                                continue
                            # Skip the last module (the app target itself)
                            if len(modules) == 1 or index == len(modules) - 1:
                                continue
                            self._process_module(module, report, source_dir)
                    continue

                # Possible standalone module file
                if _looks_like_module(data):
                    self._process_module(data, report, source_dir)

        logger.info(
            "modules import: %d scanned, %d imported, "
            "%d skipped (existing), %d skipped (no source), %d local files copied",
            report.scanned, report.imported,
            report.skipped_existing, report.skipped_no_source,
            len(report.copied_files),
        )
        return report

    def import_module(
        self, mod: dict, source_dir: Path | None = None
    ) -> "ImportResult | Path":
        """
        Import a single module dict as a recipe.

        *source_dir* is the directory of the originating manifest or module
        file. It is required to resolve ``{"type": "file", "path": "..."}``
        sources; if omitted, local-file sources are silently skipped.

        Returns the path of the created recipe file, or an ImportResult
        enum value if the module was skipped.
        """
        if not isinstance(mod, dict):
            return ImportResult.INVALID_SOURCE

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

        version = _extract_best_version(name, sources)

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
        logger.debug("Created recipe: %s", recipe_path)

        # Copy any local file sources (type=file, path=...) into dest_dir so
        # that relative path references inside the module remain valid.
        if source_dir is not None:
            _copy_local_sources(mod, source_dir, dest_dir)

        return recipe_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_module(
        self, module: dict, report: ImportReport, source_dir: Path | None = None
    ) -> None:
        """
        Process a single module dict and update *report*.

        Flatpak modules can nest other modules under a ``modules`` key.
        These sub-modules are real build targets (not the app itself) so they
        are all imported — we recurse before importing the parent so that
        dependencies are registered first.
        """
        # Recurse into sub-modules first (depth-first).
        # Each sub-module entry may be a string (external file ref) — skip those.
        for sub in module.get("modules", []):
            if isinstance(sub, dict):
                self._process_module(sub, report, source_dir)

        report.scanned += 1
        result = self.import_module(module, source_dir)

        if result == ImportResult.ALREADY_EXISTS:
            report.skipped_existing += 1
        elif result == ImportResult.INVALID_SOURCE:
            report.skipped_no_source += 1
        else:
            report.created.append(result)
            report.imported += 1
            if source_dir is not None:
                category = _classify_module(module)
                dest_dir = (
                    self.pip_recipes_dir if category == "python"
                    else self.nat_recipes_dir
                )
                for local_src in _list_local_sources(module):
                    report.copied_files.append(dest_dir / local_src)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_PIP_PATTERN = re.compile(
    r'\b(?:python(?:3)?\s+-m\s+)?pip(?:3)?\s+install\b',
    re.IGNORECASE,
)

def _classify_module(mod: dict) -> str | None:
    """Return "python" if the module uses pip install, otherwise "native"."""

    commands = mod.get("build-commands", [])

    if not commands:
        return "native" 
               
    for command in commands:
        if isinstance(command, dict):
            return "native"
        
        if _PIP_PATTERN.search(command):
            return "python"
    return "native"

def _list_local_sources(mod: dict) -> list[str]:
    """
    Return the relative paths of all local (non-URL) file sources in *mod*
    that need to be copied alongside the recipe.

    Covered source types:
    - ``{"type": "file",   "path": "..."}``  — local file, no url
    - ``{"type": "patch",  "path": "..."}``  — patch file
    - ``{"type": "script", "dest-filename": "..."}``  — generated script;
      no file to copy (commands are inline), so these are skipped.
    """
    paths = []
    for s in mod.get("sources", []):
        if not isinstance(s, dict):
            continue
        stype = s.get("type")
        if stype in ("file", "patch") and s.get("path") and not s.get("url"):
            paths.append(s["path"])
    return paths


def _copy_local_sources(
    mod: dict, source_dir: Path, dest_dir: Path
) -> None:
    """
    Copy every local file source referenced by *mod* from *source_dir* into
    *dest_dir*, preserving the bare filename (no sub-directory structure).

    A warning is logged if a referenced file does not exist in *source_dir*
    so the problem is visible without raising an exception that would abort
    the whole import run.
    """
    for rel_path in _list_local_sources(mod):
        src = source_dir / rel_path
        if not src.exists():
            logger.warning(
                "Local source file not found, skipping copy: %s", src
            )
            continue
        dst = dest_dir / src.name
        if dst.exists():
            logger.debug("Local source already present, skipping: %s", dst)
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        logger.info("Copied local source: %s → %s", src, dst)


def _extract_sources(mod: dict) -> list[dict]:
    """Return all sources that have a URL."""
    return [
        s.copy()
        for s in mod.get("sources", [])
        if isinstance(s, dict) and s.get("url")
    ]

def _normalise_pkg_name(name: str) -> str:
    """Lowercase and strip common prefixes/separators for fuzzy comparison."""
    name = name.lower()
    for prefix in ("python3-", "python-", "py-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return re.sub(r"[-_.]", "", name)


def _pkg_name_from_url(url: str) -> str:
    """
    Best-effort extraction of the package name from a URL filename.

    Handles both sdist tarballs (``foo-1.2.3.tar.gz``) and wheels
    (``foo-1.2.3-py3-none-any.whl``), returning just the distribution name.
    """
    filename = url.rstrip("/").rsplit("/", 1)[-1]
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".whl", ".tgz"):
        if filename.endswith(ext):
            filename = filename[: -len(ext)]
            break
    # The package name is everything before the first version-like segment
    # (a segment that starts with a digit).
    parts = re.split(r"[-_]", filename)
    name_parts = []
    for part in parts:
        if part and part[0].isdigit():
            break
        name_parts.append(part)
    return "".join(name_parts).lower()


def _lcs_length(a: str, b: str) -> int:
    """Return the length of the longest common substring of *a* and *b*."""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            length = 0
            while (
                i + length < len(a)
                and j + length < len(b)
                and a[i + length] == b[j + length]
            ):
                length += 1
            if length > best:
                best = length
    return best


def _extract_best_version(module_name: str, sources: list[dict]) -> str:
    """
    Choose the source URL whose package name is most similar to *module_name*
    and return the version extracted from that URL.

    Similarity is the longest common substring between the normalised module
    name and the normalised package name extracted from each URL.
    The first URL is used as a tiebreaker / fallback.
    """
    normalised_module = _normalise_pkg_name(module_name)

    best_url = sources[0].get("url", "")
    best_score = -1

    for source in sources:
        url = source.get("url", "")
        if not url:
            continue
        pkg = _normalise_pkg_name(_pkg_name_from_url(url))
        score = _lcs_length(normalised_module, pkg)
        if score > best_score:
            best_score = score
            best_url = url

    return _extract_version(best_url)


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