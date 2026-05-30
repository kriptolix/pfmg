"""
pfmg.resolution.resolvers
~~~~~~~~~~~~~~~~~~~~~~~~~~
Matches SandboxErrors produced by the probe phase against the local dataset
of SDK profiles, extension profiles, and recipes, and returns actionable
resolution suggestions.

Data sources consulted (all loaded lazily from disk, cached for the lifetime
of a ProfileIndex instance):

  data/sdk-profiles/<sdk>.<version>.json
  data/ext-profiles/<shortname>.<version>.json
  data/nat-recipes/<id>-<version>.json
  data/pip-recipes/<id>-<version>.json

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
    index = ProfileIndex()
    suggestions = resolve(errors, index)
    for s in suggestions:
        print(s)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pfmg.utils.models import SandboxError, SandboxErrorType
from pfmg.utils.logging import get_logger

# Re-export so callers that import from here keep working unchanged.
from pfmg.resolution.profiles import (       # noqa: F401
    ProfileIndex,
    ProviderKind,
    _Recipe,
    _SdkProfile,
    _ExtProfile,
)
from pfmg.resolution.matchers import (
    lib_matches,
    pc_matches_header,
    recipe_matches,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

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
    recipe_path:      Optional[Path]  = None
    module:           Optional[dict]  = None
    env:              dict[str, str]  = field(default_factory=dict)

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
# Internal helpers
# ---------------------------------------------------------------------------

def _adder(seen: set[tuple[str, str]], results: list[ResolutionSuggestion]):
    """Return a closure that deduplicates by (provider_id, matched_on)."""
    def _add(s: ResolutionSuggestion) -> None:
        key = (s.provider_id, s.matched_on)
        if key not in seen:
            results.append(s)
            seen.add(key)
    return _add


# ---------------------------------------------------------------------------
# Per-error-type resolution functions
# ---------------------------------------------------------------------------

def _resolve_native_dep(
    missing: str,
    index: ProfileIndex,
    seen: set[tuple[str, str]],
    results: list[ResolutionSuggestion],
) -> None:
    _add = _adder(seen, results)

    for sdk in index.sdks:
        for lib in sdk.libraries:
            if lib_matches(missing, lib):
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.SDK,
                    provider_id=      sdk.sdk_id,
                    provider_version= sdk.sdk_version,
                    matched_on=       lib,
                ))

    for ext in index.extensions:
        for lib in ext.libraries:
            if lib_matches(missing, lib):
                _add(ResolutionSuggestion(
                    error_missing=    missing,
                    provider_kind=    ProviderKind.EXTENSION,
                    provider_id=      ext.extension_id,
                    provider_version= ext.version,
                    matched_on=       lib,
                    env=              ext.env,
                ))

    for recipe in index.nat_recipes:
        if recipe_matches(missing, recipe):
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
    from pfmg.utils.text import normalise_pkg
    _add = _adder(seen, results)
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
        if recipe_matches(missing, recipe):
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
    _add = _adder(seen, results)

    for ext in index.extensions:
        if pc_matches_header(missing, ext.pkgconfig):
            _add(ResolutionSuggestion(
                error_missing=    missing,
                provider_kind=    ProviderKind.EXTENSION,
                provider_id=      ext.extension_id,
                provider_version= ext.version,
                matched_on=       f"pkgconfig heuristic for {missing}",
                env=              ext.env,
            ))

    for recipe in index.nat_recipes:
        if recipe_matches(Path(missing).stem, recipe):
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
    _add = _adder(seen, results)
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
    _add = _adder(seen, results)

    for recipe in index.pip_recipes:
        if recipe_matches(missing, recipe):
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