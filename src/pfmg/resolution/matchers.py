"""
pfmg.resolution.matchers
~~~~~~~~~~~~~~~~~~~~~~~~~
Pure matching/scoring functions used by the resolver and the search command.

All functions are stateless and dependency-free (no I/O, no index access).
"""
from __future__ import annotations

import re
from pathlib import Path

from pfmg.utils.text import normalise_lib, normalise_pkg


def lib_matches(missing: str, candidate: str) -> bool:
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


def pc_matches_header(header_path: str, pc_entries: frozenset[str]) -> bool:
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


def recipe_matches(missing: str, recipe) -> bool:
    """
    Return True if *recipe* is a plausible provider for *missing*.

    Checks (in order):
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


def ext_matches_query(query: str, ext) -> list[tuple[str, str]]:
    """
    Return a list of (field_label, matched_value) tuples for every item in
    *ext* that contains *query* (case-insensitive substring match).

    Covers executables, pkgconfig, and libraries — used by the search command
    to show exactly which field produced each hit.
    """
    q = query.lower()
    hits: list[tuple[str, str]] = []
    for label, items in [
        ("executable", ext.executables),
        ("pkgconfig",  ext.pkgconfig),
        ("library",    ext.libraries),
    ]:
        for item in items:
            if q in item.lower():
                hits.append((label, item))
    return hits


def sdk_matches_query(query: str, sdk) -> list[tuple[str, str]]:
    """
    Return a list of (field_label, matched_value) tuples for every item in
    *sdk* that contains *query* (case-insensitive substring match).

    Covers libraries, pkgconfig, and executables — used by the search command.
    """
    q = query.lower()
    hits: list[tuple[str, str]] = []
    for label, items in [
        ("library",    sdk.libraries),
        ("pkgconfig",  sdk.pkgconfig),
        ("executable", sdk.executables),
    ]:
        for item in items:
            if q in item.lower():
                hits.append((label, item))
    return hits