"""
pfmg.sandbox.errors
~~~~~~~~~~~~~~~~~~~~
Parses raw stderr / stdout from inside the Flatpak build sandbox and
produces normalised SandboxError objects.

Recognised patterns (in priority order):
  - Missing shared library  (ldd / linker output)
  - Missing header file      (compiler fatal error)
  - Missing pkg-config entry (configure / meson / cmake output)
  - Missing executable       (command not found)
  - Python ImportError       (python -c / pip install output)
  - pip / uv install failure (package not found, version conflict)
  - Generic build failure    (non-zero exit with unrecognised message)
"""
from __future__ import annotations

import re

from pfmg.models import SandboxError, SandboxErrorType

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# ldd output:  "    libusb-1.0.so.0 => not found"
_P_LDD_NOT_FOUND = re.compile(
    r"^\s*(?P<lib>\S+\.so\S*)\s*=>\s*not found", re.MULTILINE
)

# linker:  "cannot find -lusb-1.0"  or  "error while loading shared libraries: libfoo.so"
_P_LINKER_NOT_FOUND = re.compile(
    r"(?:cannot find -l(?P<lib1>\S+)|"
    r"error while loading shared libraries:\s*(?P<lib2>[^\s:]+\.so[^\s:]*))",
    re.IGNORECASE,
)

# GCC/Clang fatal:  "fatal error: openssl/ssl.h: No such file or directory"
_P_MISSING_HEADER = re.compile(
    r"fatal error:\s*(?P<header>[^\s:]+\.h[^:]*?):\s*No such file",
    re.IGNORECASE,
)

# pkg-config:  "Package openssl was not found in the pkg-config search path"
#              "No package 'libffi' found"
#              "Could not find dependency 'foo' ..."
_P_PKGCONFIG_NOT_FOUND = re.compile(
    r"(?:Package\s+(?P<pkg1>\S+)\s+was not found|"
    r"No package\s+'?(?P<pkg2>[^'>\s]+)'?\s+found|"
    r"Could not find dependency\s+'?(?P<pkg3>[^'>\s]+)'?)",
    re.IGNORECASE,
)

# "command not found" / "No such file or directory" for executables
_P_EXEC_NOT_FOUND = re.compile(
    r"(?P<cmd>\S+):\s*(?:command not found|No such file or directory)",
    re.IGNORECASE,
)

# Python ImportError
_P_IMPORT_ERROR = re.compile(
    r"(?:ModuleNotFoundError|ImportError):\s*(?:No module named\s*'?(?P<mod>[^'\";\n]+)'?)",
    re.IGNORECASE,
)

# pip / uv install failure
_P_PIP_NOT_FOUND = re.compile(
    r"(?:ERROR: No matching distribution found for\s+(?P<dist>[^\s=<>!]+)|"
    r"error: Package\s+'?(?P<pkg>[^'\s]+)'?\s+not found|"
    r"Could not find a version that satisfies the requirement\s+(?P<req>[^\s=<>!]+))",
    re.IGNORECASE,
)

# pip build-time missing Python dep:
#   "ModuleNotFoundError: No module named 'pybind11'"  inside a pip subprocess
#   (different from a runtime ImportError — the package itself couldn't be built)
_P_BUILD_DEP_MISSING = re.compile(
    r"ModuleNotFoundError: No module named\s+'?(?P<mod>[^'\";\s]+)'?",
    re.IGNORECASE,
)

# pip BackendUnavailable — build backend module could not be imported:
#   "BackendUnavailable: Cannot import 'mesonpy'"
#   "BackendUnavailable: Cannot import 'setuptools'"
#   Full path variant: "pip._vendor.pyproject_hooks._impl.BackendUnavailable: Cannot import 'X'"
_P_BACKEND_UNAVAILABLE = re.compile(
    r"BackendUnavailable:\s*Cannot import\s+'?(?P<mod>[^'\";\s]+)'?",
    re.IGNORECASE,
)

# pip build backend subprocess failure — covers cases where the backend exists
# but fails at import time with an arbitrary exception:
#   "Failed to import build backend 'mesonpy'"
#   "error: Failed to load PEP 517 backend"
_P_BACKEND_FAILED = re.compile(
    r"(?:Failed to (?:import|load)(?: build backend| PEP 517 backend)?\s*'?(?P<mod>[^'\";\s]+)'?)",
    re.IGNORECASE,
)

# flatpak-builder module-level failure:
#   "Error: module python3-pillow: Child process exited with code 1"
#   (localised — match the invariant English prefix + module name)
_P_FLATPAK_MODULE_FAILED = re.compile(
    r"Error:\s+module\s+(?P<module>[^:]+):\s+",
    re.IGNORECASE,
)

# Meson "Dependency X not found"
_P_MESON_DEP = re.compile(
    r"Dependency\s+(?P<dep>\S+)\s+found:\s+NO",
    re.IGNORECASE,
)

# CMake "Could NOT find <Foo>"
_P_CMAKE_NOT_FOUND = re.compile(
    r"Could NOT find\s+(?P<dep>\S+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_errors(
    stderr: str,
    stdout: str = "",
    ldd_output: str = "",
    context: str = "",
) -> list[SandboxError]:
    """
    Parse all available output and return a deduplicated list of SandboxError.
    """
    errors: list[SandboxError] = []
    seen: set[tuple[str, str]] = set()   # (error_type, missing)

    def _add(error: SandboxError) -> None:
        key = (error.error_type.value, error.missing.lower())
        if key not in seen:
            errors.append(error)
            seen.add(key)

    # --- ldd output ---
    for m in _P_LDD_NOT_FOUND.finditer(ldd_output):
        _add(SandboxError(
            error_type=SandboxErrorType.MISSING_NATIVE_DEP,
            missing=m.group("lib"),
            source="ldd",
            context=context,
            raw_line=m.group(0).strip(),
        ))

    # --- stderr + stdout (combined for patterns that appear in either) ---
    combined = stderr + "\n" + stdout

    for m in _P_LINKER_NOT_FOUND.finditer(combined):
        lib = m.group("lib1") or m.group("lib2") or ""
        if lib:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_NATIVE_DEP,
                missing=lib,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_MISSING_HEADER.finditer(combined):
        _add(SandboxError(
            error_type=SandboxErrorType.MISSING_HEADER,
            missing=m.group("header").strip(),
            source="stderr",
            context=context,
            raw_line=m.group(0).strip(),
        ))

    for m in _P_PKGCONFIG_NOT_FOUND.finditer(combined):
        pkg = m.group("pkg1") or m.group("pkg2") or m.group("pkg3") or ""
        if pkg:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PKGCONFIG,
                missing=pkg.strip(),
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_MESON_DEP.finditer(combined):
        _add(SandboxError(
            error_type=SandboxErrorType.MISSING_PKGCONFIG,
            missing=m.group("dep").strip(),
            source="stderr",
            context=context,
            raw_line=m.group(0).strip(),
        ))

    for m in _P_CMAKE_NOT_FOUND.finditer(combined):
        dep = m.group("dep").strip()
        # Skip generic cmake internal deps (uppercase, short)
        if len(dep) > 2:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PKGCONFIG,
                missing=dep,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # Build-time missing Python dep — ModuleNotFoundError inside a pip
    # subprocess (metadata generation, setup.py, etc.).  Must run BEFORE
    # _P_IMPORT_ERROR so the same text is classified as MISSING_PYTHON_PKG
    # and the import-error handler skips it via the `seen` guard.
    for m in _P_BUILD_DEP_MISSING.finditer(combined):
        mod = m.group("mod").strip().split(".")[0]
        if mod:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=mod,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # Build backend unavailable — pip could not import the backend module.
    # Same classification as a missing build dep: the package needs to be
    # added as a python3-<backend> module before this one in the manifest.
    for m in _P_BACKEND_UNAVAILABLE.finditer(combined):
        mod = m.group("mod").strip().split(".")[0]
        if mod:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=mod,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # Build backend subprocess failure — backend exists but failed at import.
    for m in _P_BACKEND_FAILED.finditer(combined):
        mod = m.group("mod")
        if mod:
            mod = mod.strip().split(".")[0]
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=mod,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_IMPORT_ERROR.finditer(combined):
        mod = m.group("mod").strip().split(".")[0]  # top-level module only
        # Skip if already captured as a build-time missing dep — same
        # ModuleNotFoundError pattern, different root cause and classification.
        if mod and (SandboxErrorType.MISSING_PYTHON_PKG.value, mod.lower()) not in seen:
            _add(SandboxError(
                error_type=SandboxErrorType.IMPORT_ERROR,
                missing=mod,
                source="import",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # flatpak-builder top-level module failure line — tells us which module
    # failed when the specific error was already captured above.
    for m in _P_FLATPAK_MODULE_FAILED.finditer(combined):
        module_name = m.group("module").strip()
        # Only add if no more specific error was recorded for this module.
        key = (SandboxErrorType.BUILD_FAILURE.value, module_name.lower())
        if key not in seen:
            _add(SandboxError(
                error_type=SandboxErrorType.BUILD_FAILURE,
                missing=module_name,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_PIP_NOT_FOUND.finditer(combined):
        pkg = m.group("dist") or m.group("pkg") or m.group("req") or ""
        if pkg:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=pkg.strip(),
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_EXEC_NOT_FOUND.finditer(combined):
        cmd = m.group("cmd").strip()
        # Filter noise — single-char tokens and common shell words
        if len(cmd) > 2 and "/" not in cmd:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_EXECUTABLE,
                missing=cmd,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    return errors