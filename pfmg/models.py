"""
pfmg.models — shared data models for the entire pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BuildBackend(str, Enum):
    SETUPTOOLS = "setuptools"
    MATURIN = "maturin"
    MESON_PYTHON = "meson-python"
    SETUPTOOLS_RUST = "setuptools-rust"
    SCIKIT_BUILD = "scikit-build"
    SCIKIT_BUILD_CORE = "scikit-build-core"
    FLIT = "flit"
    PDM = "pdm-backend"
    HATCH = "hatchling"
    POETRY = "poetry"
    UNKNOWN = "unknown"

class SourceType(str, Enum):
    WHEEL = "wheel"
    SDIST = "sdist"

@dataclass
class ResolvedPackage:
    """A single resolved Python package with all metadata needed for Flatpak manifest generation."""

    name: str
    version: str
    wheel_available: bool = False
    build_backend: BuildBackend = BuildBackend.UNKNOWN
    requires_native: bool = False
    # direct or transitive
    is_direct: bool = False
    # sha256 of the chosen source (wheel or sdist)
    source_hash: Optional[str] = None
    source_url: Optional[str] = None
    source_type: Optional[SourceType] = None
    # native libraries this package needs (populated by NativeDependencyAnalyzer in Phase 2)
    native_deps: list[str] = field(default_factory=list)
    # sdk extensions this package needs (populated by SDKExtensionResolver in Phase 2)
    required_extensions: list[str] = field(default_factory=list)
    # extras / env markers
    extras: list[str] = field(default_factory=list)

@dataclass
class FlatpakSource:
    type: str  # "archive", "file", "git", "patch"
    url: Optional[str] = None
    sha256: Optional[str] = None
    path: Optional[str] = None
    dest_filename: Optional[str] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    tag: Optional[str] = None

@dataclass
class FlatpakModule:
    name: str
    buildsystem: str = "simple"
    build_commands: list[str] = field(default_factory=list)
    sources: list[FlatpakSource] = field(default_factory=list)
    build_options: dict = field(default_factory=dict)
    modules: list["FlatpakModule"] = field(default_factory=list)  # sub-modules
    cleanup: list[str] = field(default_factory=list)
    config_opts: list[str] = field(default_factory=list)

@dataclass
class FlatpakManifest:
    app_id: str
    runtime: str
    runtime_version: str
    sdk: str
    sdk_extensions: list[str] = field(default_factory=list)
    modules: list[FlatpakModule] = field(default_factory=list)
    finish_args: list[str] = field(default_factory=list)

@dataclass
class ResolutionResult:
    """Final output of the full resolver pipeline."""

    packages: list[ResolvedPackage] = field(default_factory=list)
    # packages that need native recipes
    unresolved_natives: list[str] = field(default_factory=list)
    # recipes found in local DB
    native_recipes: list[NativeRecipe] = field(default_factory=list)
    # sdk-extension ids for manifest sdk-extensions field
    required_extensions: list[str] = field(default_factory=list)
    # full ExtensionMatch objects (populated by SDKExtensionResolver)
    extension_matches: list["ExtensionMatch"] = field(default_factory=list)
    lockfile_hash: Optional[str] = None

# ---------------------------------------------------------------------------
# Phase 3 — Build Sandbox Prober types
# ---------------------------------------------------------------------------

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

@dataclass
class SandboxProbeReport:
    """
    Full report produced by BuildSandboxProber.probe().

    Answers the five questions from spec §18.5:
      1. quais dependências Python faltam
      2. quais libs nativas estão ausentes
      3. se o SDK atual é suficiente
      4. se SDK extension é necessária
      5. se o build é possível sem modificações
    """
    # The packages that were probed
    probed_packages: list[str] = field(default_factory=list)

    # Errors found during probe
    errors: list[SandboxError] = field(default_factory=list)

    # Parsed conclusions
    missing_python_packages: list[str] = field(default_factory=list)
    missing_native_libs: list[str] = field(default_factory=list)
    missing_headers: list[str] = field(default_factory=list)
    missing_pkgconfig: list[str] = field(default_factory=list)

    # Generated Flatpak module dicts, keyed by package name.
    # Populated by BuildSandboxProber for packages that probed successfully.
    modules: dict[str, dict] = field(default_factory=dict)

    # Names of packages for which a module was successfully generated.
    successful_packages: list[str] = field(default_factory=list)

    # High-level verdicts
    sdk_sufficient: bool = True
    suggested_extensions: list[str] = field(default_factory=list)
    build_possible: bool = True

    # Raw output for audit
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0

    # Whether the probe actually ran (False if flatpak-builder not available)
    ran: bool = False
    skip_reason: str = ""
