"""
pfmg.resolve.resolvers
~~~~~~~~~~~~~~~
Matches SandboxErrors produced by the probe phase against the local dataset
of SDK profiles, extension profiles, and recipes, and returns actionable
resolution suggestions.

Data sources consulted (all loaded lazily from disk, cached for the lifetime
of a ProfileIndex instance):

  data/sdk-profiles/<sdk>.<version>.json
      {
        "sdk_id":      "org.freedesktop.Sdk",
        "sdk_version": "24.08",
        "pkgconfig":   ["zlib", ...],
        "libraries":   ["libz.so.1", ...],
        "executables": ["gcc", ...]
      }

  data/ext-profiles/<shortname>.<version>.json
      {
        "extension_id":         "org.freedesktop.Sdk.Extension.llvm17",
        "display_name":         "llvm17.24.08",
        "mount_path":           "/usr/lib/sdk/llvm17",
        "provides_pkgconfig":   ["clang", ...],
        "provides_libraries":   ["libLLVM-17.so", ...],
        "provides_executables": ["clang", "llvm-config", ...],
        "env":                  {"PATH": "/usr/lib/sdk/llvm17/bin:$PATH"}
      }

  data/nat-recipes/<id>-<version>.json
      {
        "header": {"id": "zlib", "version": "1.3.1", "type": "native"},
        "module": { <flatpak module dict> }
      }

  data/pip-recipes/<id>-<version>.json
      {
        "header": {"id": "numpy", "version": "1.26.0", "type": "python"},
        "module": { <flatpak module dict> }
      }

Error → resolution mapping
--------------------------
  MISSING_NATIVE_DEP  → sdk.libraries, ext.provides_libraries, nat-recipes
  MISSING_HEADER      → ext.provides_pkgconfig (prefix heuristic), nat-recipes
  MISSING_PKGCONFIG   → sdk.pkgconfig, ext.provides_pkgconfig, nat-recipes
  MISSING_EXECUTABLE  → sdk.executables, ext.provides_executables
  MISSING_PYTHON_PKG  → pip-recipes
  IMPORT_ERROR        → pip-recipes
  BUILD_FAILURE       → nat-recipes + pip-recipes (best-effort)

Usage
-----
    index = ProfileIndex()                   # loads all profiles on first use
    suggestions = resolve(errors, index)
    for s in suggestions:
        print(s)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from src.pfmg.utils.models import SandboxError, SandboxErrorType
from src.pfmg.utils.io import load_json_or_yaml
from src.pfmg.utils.logging import get_logger
from src.pfmg.utils.text import normalise_lib, normalise_pkg

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Canonical data directories
# ---------------------------------------------------------------------------

_DATA_DIR         = Path(__file__).parent / "data"
_SDK_PROFILES_DIR = _DATA_DIR / "sdk-profiles"
_EXT_PROFILES_DIR = _DATA_DIR / "ext-profiles"
_NAT_RECIPES_DIR  = _DATA_DIR / "nat-recipes"
_PIP_RECIPES_DIR  = _DATA_DIR / "pip-recipes"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class ProviderKind(str, Enum):
    SDK        = "sdk"
    EXTENSION  = "extension"
    NAT_RECIPE = "nat-recipe"
    PIP_RECIPE = "pip-recipe"


@dataclass
class ResolutionSuggestion:
    """
    A single actionable suggestion for resolving one SandboxError.

    Attributes
    ----------
    error_missing:
        The ``missing`` field from the originating SandboxError.
    provider_kind:
        What kind of provider was found.
    provider_id:
        Human-readable identifier:
          SDK       → sdk_id (e.g. "org.freedesktop.Sdk")
          EXTENSION → extension_id
          *_RECIPE  → recipe header id (e.g. "zlib", "numpy")
    provider_version:
        Version string from the profile or recipe header.
    matched_on:
        The specific item that triggered the match.
    recipe_path:
        Absolute path to the recipe JSON file (only for *_RECIPE providers).
    module:
        The ready-to-use Flatpak module dict from the recipe.
    env:
        Environment variables to activate the provider (EXTENSION only).
    """

    error_missing:    str
    provider_kind:    ProviderKind
    provider_id:      str
    provider_version: str
    matched_on:       str
    recipe_path:      Optional[Path]   = None
    module:           Optional[dict]   = None
    env:              dict[str, str]   = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover
        kind = self.provider_kind.value
        if self.provider_kind in (ProviderKind.NAT_RECIPE, ProviderKind.PIP_RECIPE):
            return (
                f"[{kind}] '{self.error_missing}' → {self.provider_id}"
                f"=={self.provider_version}  (recipe: {self.recipe_path})"
            )
        return (
            f"[{kind}] '{self.error_missing}' → {self.provider_id}"
            f"  (matched: {self.matched_on})"
        )


# ---------------------------------------------------------------------------
# Profile & recipe index
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
    # Loaders  (use shared load_json_or_yaml — no local duplication)
    # ------------------------------------------------------------------

    def _load_sdks(self):
        if not self._sdk_dir.exists():
            logger.debug("sdk-profiles dir not found: %s", self._sdk_dir)
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
            logger.debug("ext-profiles dir not found: %s", self._ext_dir)
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

    def search_recipes(
        self,
        query: str,
        kind: Optional[ProviderKind] = None,
    ) -> list[_Recipe]:
        """
        Search all recipes (or only *kind* recipes) for *query*.

        Matching uses the same fuzzy logic as the resolver so results are
        consistent with what ``resolve()`` would return.
        """
        pools: list[list[_Recipe]] = []
        if kind in (None, ProviderKind.NAT_RECIPE):
            pools.append(self.nat_recipes)
        if kind in (None, ProviderKind.PIP_RECIPE):
            pools.append(self.pip_recipes)

        results: list[_Recipe] = []
        for pool in pools:
            for recipe in pool:
                if _recipe_matches(query, recipe):
                    results.append(recipe)
        return results


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _lib_matches(missing: str, candidate: str) -> bool:
    """
    Return True if *candidate* (from a profile's libraries list) could
    provide the *missing* shared library.

    Handles exact matches and stem-level fuzzy matches.
    """
    if missing == candidate:
        return True
    if candidate.startswith(missing) or missing.startswith(candidate):
        return True
    return normalise_lib(missing) == normalise_lib(candidate)


def _pc_matches_header(header_path: str, pc_entries: frozenset[str]) -> bool:
    """
    Heuristic: a header like ``openssl/ssl.h`` is likely provided by the
    ``openssl`` pkgconfig entry.
    """
    parts = Path(header_path).parts
    candidates: set[str] = set()
    if parts:
        candidates.add(parts[0].lower())
    stem = Path(header_path).stem.lower()
    candidates.add(stem)
    candidates.add(normalise_pkg(stem))
    return bool(candidates & {normalise_pkg(e) for e in pc_entries})


def _recipe_matches(missing: str, recipe: _Recipe) -> bool:
    """
    Return True if *recipe* is a plausible provider for *missing*.

    Checks:
      1. Exact normalised id match
      2. Prefix/suffix containment (covers "libfoo" matching recipe "foo")
      3. .so filename normalisation (libz.so.1 → "z" matches recipe "zlib")
      4. Module's declared "name" field
    """
    norm_missing = normalise_pkg(missing)
    norm_id      = normalise_pkg(recipe.recipe_id)

    if norm_missing == norm_id:
        return True

    stripped = re.sub(r"^lib", "", norm_missing)
    if stripped == norm_id or norm_missing.endswith(norm_id) or norm_id.endswith(stripped):
        return True

    if ".so" in missing:
        lib_stem = re.sub(r"^lib", "", normalise_lib(missing))
        lib_full = normalise_lib(missing)
        for candidate in (lib_stem, lib_full, normalise_pkg(lib_full)):
            if candidate and (
                candidate == norm_id
                or norm_id.startswith(candidate)
                or candidate.startswith(norm_id)
            ):
                return True

    mod_name = normalise_pkg(recipe.module.get("name", ""))
    if mod_name and (norm_missing == mod_name or stripped == mod_name):
        return True

    return False


# ---------------------------------------------------------------------------
# Core resolution logic
# ---------------------------------------------------------------------------

def _resolve_native_dep(
    missing: str,
    index: ProfileIndex,
    seen: set[tuple[str, str]],
    results: list[ResolutionSuggestion],
) -> None:

    def _add(s: ResolutionSuggestion) -> None:
        key = (s.provider_id, s.matched_on)
        if key not in seen:
            results.append(s)
            seen.add(key)

    for sdk in index.sdks:
        for lib in sdk.libraries:
            if _lib_matches(missing, lib):
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.SDK,
                    provider_id=      sdk.sdk_id,
                    provider_version= sdk.sdk_version,
                    matched_on=       lib,
                ))

    for ext in index.extensions:
        for lib in ext.libraries:
            if _lib_matches(missing, lib):
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.EXTENSION,
                    provider_id=      ext.extension_id,
                    provider_version= ext.version,
                    matched_on=       lib,
                    env=              ext.env,
                ))

    for recipe in index.nat_recipes:
        if _recipe_matches(missing, recipe):
            _add(ResolutionSuggestion(
                error_missing=    missing,
                provider_kind=    ProviderKind.NAT_RECIPE,
                provider_id=      recipe.recipe_id,
                provider_version= recipe.version,
                matched_on=       recipe.recipe_id,
                recipe_path=      recipe.path,
                module=           recipe.module,
            ))


def _resolve_pkgconfig(
    missing: str,
    index: ProfileIndex,
    seen: set[tuple[str, str]],
    results: list[ResolutionSuggestion],
) -> None:

    def _add(s: ResolutionSuggestion) -> None:
        key = (s.provider_id, s.matched_on)
        if key not in seen:
            results.append(s)
            seen.add(key)

    norm = normalise_pkg(missing)

    for sdk in index.sdks:
        for pc in sdk.pkgconfig:
            if normalise_pkg(pc) == norm:
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.SDK,
                    provider_id=      sdk.sdk_id,
                    provider_version= sdk.sdk_version,
                    matched_on=       pc,
                ))

    for ext in index.extensions:
        for pc in ext.pkgconfig:
            if normalise_pkg(pc) == norm:
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.EXTENSION,
                    provider_id=      ext.extension_id,
                    provider_version= ext.version,
                    matched_on=       pc,
                    env=              ext.env,
                ))

    for recipe in index.nat_recipes:
        if _recipe_matches(missing, recipe):
            _add(ResolutionSuggestion(
                error_missing=    missing,
                provider_kind=    ProviderKind.NAT_RECIPE,
                provider_id=      recipe.recipe_id,
                provider_version= recipe.version,
                matched_on=       recipe.recipe_id,
                recipe_path=      recipe.path,
                module=           recipe.module,
            ))


def _resolve_header(
    missing: str,
    index: ProfileIndex,
    seen: set[tuple[str, str]],
    results: list[ResolutionSuggestion],
) -> None:

    def _add(s: ResolutionSuggestion) -> None:
        key = (s.provider_id, s.matched_on)
        if key not in seen:
            results.append(s)
            seen.add(key)

    for ext in index.extensions:
        if _pc_matches_header(missing, ext.pkgconfig):
            _add(ResolutionSuggestion(
                error_missing=    missing,
                provider_kind=    ProviderKind.EXTENSION,
                provider_id=      ext.extension_id,
                provider_version= ext.version,
                matched_on=       f"pkgconfig heuristic for {missing}",
                env=              ext.env,
            ))

    for recipe in index.nat_recipes:
        if _recipe_matches(Path(missing).stem, recipe):
            _add(ResolutionSuggestion(
                error_missing=    missing,
                provider_kind=    ProviderKind.NAT_RECIPE,
                provider_id=      recipe.recipe_id,
                provider_version= recipe.version,
                matched_on=       recipe.recipe_id,
                recipe_path=      recipe.path,
                module=           recipe.module,
            ))


def _resolve_executable(
    missing: str,
    index: ProfileIndex,
    seen: set[tuple[str, str]],
    results: list[ResolutionSuggestion],
) -> None:

    def _add(s: ResolutionSuggestion) -> None:
        key = (s.provider_id, s.matched_on)
        if key not in seen:
            results.append(s)
            seen.add(key)

    norm = missing.lower()

    for sdk in index.sdks:
        for exe in sdk.executables:
            if exe.lower() == norm:
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.SDK,
                    provider_id=      sdk.sdk_id,
                    provider_version= sdk.sdk_version,
                    matched_on=       exe,
                ))

    for ext in index.extensions:
        for exe in ext.executables:
            if exe.lower() == norm:
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.EXTENSION,
                    provider_id=      ext.extension_id,
                    provider_version= ext.version,
                    matched_on=       exe,
                    env=              ext.env,
                ))


def _resolve_python_pkg(
    missing: str,
    index: ProfileIndex,
    seen: set[tuple[str, str]],
    results: list[ResolutionSuggestion],
) -> None:

    def _add(s: ResolutionSuggestion) -> None:
        key = (s.provider_id, s.matched_on)
        if key not in seen:
            results.append(s)
            seen.add(key)

    for recipe in index.pip_recipes:
        if _recipe_matches(missing, recipe):
            _add(ResolutionSuggestion(
                error_missing=    missing,
                provider_kind=    ProviderKind.PIP_RECIPE,
                provider_id=      recipe.recipe_id,
                provider_version= recipe.version,
                matched_on=       recipe.recipe_id,
                recipe_path=      recipe.path,
                module=           recipe.module,
            ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(
    errors: list[SandboxError],
    index: Optional[ProfileIndex] = None,
) -> list[ResolutionSuggestion]:
    """
    Match each SandboxError against the local profile and recipe dataset.

    Parameters
    ----------
    errors:
        List of ``SandboxError`` objects from ``parse_errors()`` or a probe
        report's ``.errors`` list.
    index:
        A ``ProfileIndex`` instance.  If omitted, a default one is created.

    Returns
    -------
    A deduplicated list of ``ResolutionSuggestion`` objects.
    Errors with no match produce no entry.
    """
    if index is None:
        index = ProfileIndex()

    results: list[ResolutionSuggestion] = []
    seen:    set[tuple[str, str]]       = set()  # (provider_id, matched_on)

    for error in errors:
        missing  = error.missing
        err_type = error.error_type

        if err_type == SandboxErrorType.MISSING_NATIVE_DEP:
            _resolve_native_dep(missing, index, seen, results)

        elif err_type == SandboxErrorType.MISSING_PKGCONFIG:
            _resolve_pkgconfig(missing, index, seen, results)

        elif err_type == SandboxErrorType.MISSING_HEADER:
            _resolve_header(missing, index, seen, results)

        elif err_type == SandboxErrorType.MISSING_EXECUTABLE:
            _resolve_executable(missing, index, seen, results)

        elif err_type in (
            SandboxErrorType.MISSING_PYTHON_PKG,
            SandboxErrorType.IMPORT_ERROR,
        ):
            _resolve_python_pkg(missing, index, seen, results)

        elif err_type == SandboxErrorType.BUILD_FAILURE:
            _resolve_python_pkg(missing, index, seen, results)
            _resolve_native_dep(missing, index, seen, results)

    logger.debug(
        "resolve(): %d errors → %d suggestions", len(errors), len(results)
    )
    return results


def resolve_report(
    report,
    index: Optional[ProfileIndex] = None,
) -> list[ResolutionSuggestion]:
    """
    Convenience wrapper: resolve all errors in a ``SandboxProbeReport``.

    Attaches the result list to ``report.suggestions`` if the attribute exists.
    """
    suggestions = resolve(report.errors, index=index)
    if hasattr(report, "suggestions"):
        report.suggestions = suggestions
    return suggestions
