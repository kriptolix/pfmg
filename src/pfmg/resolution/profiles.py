"""
pfmg.resolution.profiles
~~~~~~~~~~~~~~~~~~~~~~~~~
Data-model types and lazy-loading index for all local profiles and recipes.

Directory layout (relative to the package root ``src/pfmg/``):

  data/sdk-profiles/<sdk>.<version>.json
  data/ext-profiles/<shortname>.<version>.json
  data/nat-recipes/<id>-<version>.json
  data/pip-recipes/<id>-<version>.json
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from pfmg.utils.io import load_json_or_yaml
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical data directories
# src/pfmg/resolution/profiles.py → parent = resolution/ → parent = pfmg/
# ---------------------------------------------------------------------------

_DATA_DIR         = Path(__file__).parent.parent / "data"
_SDK_PROFILES_DIR = _DATA_DIR / "sdk-profiles"
_EXT_PROFILES_DIR = _DATA_DIR / "ext-profiles"
_NAT_RECIPES_DIR  = _DATA_DIR / "nat-recipes"
_PIP_RECIPES_DIR  = _DATA_DIR / "pip-recipes"


# ---------------------------------------------------------------------------
# Enums & result types shared across the resolution package
# ---------------------------------------------------------------------------

class ProviderKind(str, Enum):
    SDK        = "sdk"
    EXTENSION  = "extension"
    NAT_RECIPE = "nat-recipe"
    PIP_RECIPE = "pip-recipe"


# ---------------------------------------------------------------------------
# Internal data model
# ---------------------------------------------------------------------------

@dataclass
class _SdkProfile:
    sdk_id:      str
    sdk_version: str
    pkgconfig:   frozenset[str]
    libraries:   frozenset[str]
    executables: frozenset[str]


@dataclass
class _ExtProfile:
    extension_id: str
    display_name: str
    version:      str
    mount_path:   str
    pkgconfig:    frozenset[str]
    libraries:    frozenset[str]
    executables:  frozenset[str]
    env:          dict[str, str]


@dataclass
class _Recipe:
    recipe_id: str
    version:   str
    kind:      ProviderKind
    path:      Path
    module:    dict


# ---------------------------------------------------------------------------
# Profile & recipe index
# ---------------------------------------------------------------------------

class ProfileIndex:
    """
    Lazy-loading, cached index of all local profiles and recipes.

    One instance per tool session is sufficient; pass it into ``resolve()``
    to avoid redundant disk reads.
    """

    def __init__(
        self,
        sdk_profiles_dir: Path = _SDK_PROFILES_DIR,
        ext_profiles_dir: Path = _EXT_PROFILES_DIR,
        nat_recipes_dir:  Path = _NAT_RECIPES_DIR,
        pip_recipes_dir:  Path = _PIP_RECIPES_DIR,
    ) -> None:
        self._sdk_dir = sdk_profiles_dir
        self._ext_dir = ext_profiles_dir
        self._nat_dir = nat_recipes_dir
        self._pip_dir = pip_recipes_dir

        self._sdks: Optional[list[_SdkProfile]] = None
        self._exts: Optional[list[_ExtProfile]]  = None
        self._nat:  Optional[list[_Recipe]]       = None
        self._pip:  Optional[list[_Recipe]]       = None

    # ------------------------------------------------------------------
    # Accessors (trigger lazy load)
    # ------------------------------------------------------------------

    @property
    def sdks(self) -> list[_SdkProfile]:
        if self._sdks is None:
            self._sdks = list(self._load_sdks())
        return self._sdks

    @property
    def extensions(self) -> list[_ExtProfile]:
        if self._exts is None:
            self._exts = list(self._load_exts())
        return self._exts

    @property
    def nat_recipes(self) -> list[_Recipe]:
        if self._nat is None:
            self._nat = list(self._load_recipes(self._nat_dir, ProviderKind.NAT_RECIPE))
        return self._nat

    @property
    def pip_recipes(self) -> list[_Recipe]:
        if self._pip is None:
            self._pip = list(self._load_recipes(self._pip_dir, ProviderKind.PIP_RECIPE))
        return self._pip

    def reload(self) -> None:
        """Invalidate all cached data and force a fresh load on next access."""
        self._sdks = self._exts = self._nat = self._pip = None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_recipes(
        self,
        query: str,
        kind: Optional[ProviderKind] = None,
    ) -> list[_Recipe]:
        """
        Search all recipes (or only *kind* recipes) for *query*.

        Matching uses the same logic as the resolver so results are
        consistent with what ``resolve()`` would return.
        """
        # Import here to avoid circular dependency (matchers → profiles)
        from pfmg.resolution.matchers import recipe_matches

        pools: list[list[_Recipe]] = []
        if kind in (None, ProviderKind.NAT_RECIPE):
            pools.append(self.nat_recipes)
        if kind in (None, ProviderKind.PIP_RECIPE):
            pools.append(self.pip_recipes)

        results: list[_Recipe] = []
        for pool in pools:
            for recipe in pool:
                if recipe_matches(query, recipe):
                    results.append(recipe)
        return results

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    def _load_sdks(self):
        if not self._sdk_dir.exists():
            logger.warning("sdk-profiles dir not found: %s", self._sdk_dir)
            return
        for p in sorted(self._sdk_dir.glob("**/*.json")):
            try:
                data = load_json_or_yaml(p)
                yield _SdkProfile(
                    sdk_id=      data.get("sdk_id", ""),
                    sdk_version= data.get("sdk_version", ""),
                    pkgconfig=   frozenset(data.get("pkgconfig",   [])),
                    libraries=   frozenset(data.get("libraries",   [])),
                    executables= frozenset(data.get("executables", [])),
                )
            except Exception as exc:
                logger.warning("Failed to load SDK profile %s: %s", p, exc)

    def _load_exts(self):
        if not self._ext_dir.exists():
            logger.warning("ext-profiles dir not found: %s", self._ext_dir)
            return
        for p in sorted(self._ext_dir.glob("**/*.json")):
            try:
                data = load_json_or_yaml(p)
                # version is encoded in the filename: <shortname>.<version>.json
                parts = p.stem.split(".", 1)
                version = parts[1] if len(parts) == 2 else ""
                yield _ExtProfile(
                    extension_id= data.get("extension_id", ""),
                    display_name= data.get("display_name", p.stem),
                    version=      version,
                    mount_path=   data.get("mount_path", ""),
                    pkgconfig=    frozenset(data.get("provides_pkgconfig",   [])),
                    libraries=    frozenset(data.get("provides_libraries",   [])),
                    executables=  frozenset(data.get("provides_executables", [])),
                    env=          data.get("env", {}),
                )
            except Exception as exc:
                logger.warning("Failed to load ext profile %s: %s", p, exc)

    def _load_recipes(self, directory: Path, kind: ProviderKind):
        if not directory.exists():
            logger.debug("recipes dir not found: %s", directory)
            return
        for p in sorted(directory.glob("**/*.json")):
            try:
                data = load_json_or_yaml(p)
                header = data.get("header", {})
                module = data.get("module", {})
                if not header or not module:
                    continue
                yield _Recipe(
                    recipe_id= header.get("id", p.stem),
                    version=   header.get("version", "unknown"),
                    kind=      kind,
                    path=      p,
                    module=    module,
                )
            except Exception as exc:
                logger.warning("Failed to load recipe %s: %s", p, exc)