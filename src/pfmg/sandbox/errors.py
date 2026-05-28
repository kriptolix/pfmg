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

from dataclasses import dataclass
from enum import Enum

import re

class SandboxErrorType(str, Enum):
    MISSING_NATIVE_DEP   = "missing_native_dependency"
    MISSING_HEADER       = "missing_header"
    MISSING_PKGCONFIG    = "missing_pkgconfig"
    MISSING_EXECUTABLE   = "missing_executable"
    MISSING_PYTHON_PKG   = "missing_python_package"
    BUILD_FAILURE        = "build_failure"
    IMPORT_ERROR         = "import_error"
    UNKNOWN              = "unknown"

@dataclass
class SandboxError:
    """A single normalised error captured from the build sandbox."""
    error_type: SandboxErrorType
    missing: str                    # the thing that is missing
    source: str                     # "stderr" | "ldd" | "pkg-config" | "import"
    context: str = ""               # which package / build step triggered this
    raw_line: str = ""              # original unparsed line for debugging

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