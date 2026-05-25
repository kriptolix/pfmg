"""
pfmr.sandbox.probe
~~~~~~~~~~~~~~~~~~~
BuildSandboxProber — Phase 3 core component.

Orchestrates the full sandbox probe sequence for a set of Python packages:

  1. Write test manifest + infoscript.sh to a temp work directory
  2. Initialise the build directory (flatpak build-init)
  3. Set up a Python venv inside the sandbox
  4. For each package:
       a. Attempt `uv pip install <pkg>` (or pip)
       b. If install fails → parse errors, record missing deps
       c. If install succeeds → attempt `python -c "import <pkg>"`
       d. If import fails → record ImportError
       e. Run ldd on any .so files found in site-packages → record missing libs
       f. Run pkg-config checks for declared native_deps
  5. Collate all errors into a SandboxProbeReport with high-level verdicts
  6. If import succeeds → generate a .json (or .yaml) module for each package

The prober skips gracefully when:
  - flatpak is not installed (ran=False, skip_reason set)
  - The sandbox build itself fails

Cache strategy (spec §18.4):
  - The build-dir IS cached between probe calls for the same work_dir
    (flatpak build-init creates a persistent build-dir on disk)
  - The Python venv is set up once per session and reused
  - Failed probe states are NOT cached: if errors were found, the
    work-dir is left intact for debugging but not reused as a "clean" base
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from reference.bkp.models import (
    ResolvedPackage,
    ResolutionResult,
    SandboxError,
    SandboxErrorType,
    SandboxProbeReport,
)

from reference.bkp.errors import parse_errors
from reference.bkp.sandbox import SandboxRunner
from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Python venv setup commands (run once after base build)
# ---------------------------------------------------------------------------

_VENV_SETUP_CMDS = [
    "python3 -m venv /app/venv",
    "/app/venv/bin/pip install --quiet --upgrade pip",
    "/app/venv/bin/pip install --quiet uv",
]

_VENV_SETUP_SH = " && ".join(_VENV_SETUP_CMDS)

# ldd output line parser — captures resolved path (used for "not found" detection)
_LDD_LINE = re.compile(r"^\s*(?P<lib>\S+\.so\S*)\s*=>\s*(?P<path>\S+)", re.MULTILINE)

# Shell script: find all .so files installed by a package and run ldd on each.
# The package name is interpolated at call time.
_LDD_SCRIPT_TEMPLATE = r"""
SITE=$(/app/venv/bin/python -c "import site; print(site.getsitepackages()[0])")
find "$SITE/{pkg_dir}" -name '*.so*' -type f 2>/dev/null | while read so; do
    echo "=== LDD $so ==="
    ldd "$so" 2>&1
done
"""

# Well-known package-name → import-name overrides.
# Covers the most common cases where PyPI name ≠ top-level module name.
_IMPORT_NAME_OVERRIDES: dict[str, str] = {
    "pillow":           "PIL",
    "pil":              "PIL",
    "scikit-learn":     "sklearn",
    "scikit-image":     "skimage",
    "scikit-build":     "skbuild",
    "opencv-python":    "cv2",
    "opencv-python-headless": "cv2",
    "python-dateutil":  "dateutil",
    "beautifulsoup4":   "bs4",
    "pyyaml":           "yaml",
    "protobuf":         "google.protobuf",
    "grpcio":           "grpc",
    "mysqlclient":      "MySQLdb",
    "psycopg2-binary":  "psycopg2",
    "pyzmq":            "zmq",
    "pygame":           "pygame",
    "pyserial":         "serial",
    "pycairo":          "cairo",
    "pygobject":        "gi",
    "python-magic":     "magic",
    "python-dotenv":    "dotenv",
    "typing-extensions": "typing_extensions",
    "importlib-metadata": "importlib_metadata",
}


def _derive_import_name(pkg_name: str) -> str:
    """
    Best-effort derivation of the Python import name from a PyPI package name.

    Resolution order:
      1. Explicit override table
      2. Normalise: lower-case, hyphens → underscores
    """
    key = pkg_name.lower()
    if key in _IMPORT_NAME_OVERRIDES:
        return _IMPORT_NAME_OVERRIDES[key]
    return key.replace("-", "_")


def _pkg_dir_name(pkg_name: str) -> str:
    """
    Return the likely directory name under site-packages for ldd scanning.
    Falls back to the normalised import name if no better guess is possible.
    """
    key = pkg_name.lower()
    if key in _IMPORT_NAME_OVERRIDES:
        return _IMPORT_NAME_OVERRIDES[key]
    return key.replace("-", "_")


# ---------------------------------------------------------------------------
# Module output builders
# ---------------------------------------------------------------------------

# Template for a pip-installed Python package module.
# Follows the Flatpak manifest "modules" schema.
def _build_pip_module(pkg: ResolvedPackage) -> dict:
    """
    Build a Flatpak module dict for a pure-pip package.

    Generates:
      {
        "name": "<pkg>",
        "buildsystem": "simple",
        "build-commands": ["pip install --no-index --find-links=... <pkg>==<ver>"],
        "sources": [{ "type": "file", "url": "...", "sha256": "..." }]
      }

    When pkg.wheel_url is available the source points directly at the wheel;
    otherwise a pip download source is used.
    """
    spec = f"{pkg.name}=={pkg.version}"
    build_commands = [
        f"/app/venv/bin/pip install --no-build-isolation --no-deps {spec}"
    ]

    sources: list[dict] = []
    if hasattr(pkg, "wheel_url") and pkg.wheel_url:
        source: dict = {"type": "file", "url": pkg.wheel_url}
        if hasattr(pkg, "wheel_sha256") and pkg.wheel_sha256:
            source["sha256"] = pkg.wheel_sha256
        sources.append(source)
    else:
        sources.append({
            "type": "shell",
            "commands": [
                f"/app/venv/bin/pip download --no-deps --dest /app/wheels {spec}"
            ],
        })

    return {
        "name": pkg.name,
        "buildsystem": "simple",
        "build-commands": build_commands,
        "sources": sources,
    }


def _build_venv_module(sdk: str, runtime_version: str) -> dict:
    """
    Build the mandatory venv-setup module that must precede all pip modules.

    This is emitted once as the first module in the generated manifest section.
    """
    return {
        "name": "python-venv-setup",
        "buildsystem": "simple",
        "build-commands": [
            "python3 -m venv /app/venv",
            "/app/venv/bin/pip install --quiet --upgrade pip",
            "/app/venv/bin/pip install --quiet uv",
        ],
        "sources": [],
    }


# ---------------------------------------------------------------------------
# Main prober
# ---------------------------------------------------------------------------

class BuildSandboxProber:
    """
    Probes a set of Python packages inside a real Flatpak build environment.

    Usage::

        prober = BuildSandboxProber(
            runtime="org.freedesktop.Platform",
            runtime_version="24.08",
            sdk="org.freedesktop.Sdk",
        )
        report = prober.probe(packages)
        if report.ran:
            for err in report.errors:
                print(err)

    After a successful probe, ``report.modules`` contains ready-to-use
    Flatpak module dicts.  Call ``prober.write_modules(report, output_dir)``
    to persist them as JSON (or YAML) files.
    """

    def __init__(
        self,
        runtime: str = "org.freedesktop.Platform",
        runtime_version: str = "24.08",
        sdk: str = "org.freedesktop.Sdk",
        sdk_extensions: Optional[list[str]] = None,
        # Working directory for the sandbox (auto-created if None)
        work_dir: Optional[Path] = None,
        # Keep work_dir after probe (useful for debugging)
        keep_work_dir: bool = False,
        # Timeout per individual sandbox command
        command_timeout: int = 120,
        # Timeout for the initial sandbox build step
        build_timeout: int = 600,
        # Use uv instead of pip for installation tests
        use_uv: bool = True,
    ):
        self.runtime = runtime
        self.runtime_version = runtime_version
        self.sdk = sdk
        self.sdk_extensions = sdk_extensions or []
        self._work_dir = work_dir
        self._keep_work_dir = keep_work_dir
        self._command_timeout = command_timeout
        self._build_timeout = build_timeout
        self._use_uv = use_uv
        self._owned_work_dir: Optional[Path] = None   # temp dir we created

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if flatpak is available on the host."""
        return shutil.which("flatpak") is not None

    def probe(
        self,
        packages: list[ResolvedPackage],
        work_dir: Optional[Path] = None,
    ) -> SandboxProbeReport:
        """
        Run the full probe sequence for the given packages.
        Returns a SandboxProbeReport regardless of outcome.
        """
        effective_work_dir = work_dir or self._work_dir or self._make_work_dir()
        try:
            return self._probe(packages, effective_work_dir)
        finally:
            if self._owned_work_dir and not self._keep_work_dir:
                shutil.rmtree(self._owned_work_dir, ignore_errors=True)
                self._owned_work_dir = None

    def probe_result(
        self,
        result: ResolutionResult,
        work_dir: Optional[Path] = None,
    ) -> SandboxProbeReport:
        """Convenience: probe all packages in a ResolutionResult."""
        return self.probe(result.packages, work_dir=work_dir)

    def write_modules(
        self,
        report: SandboxProbeReport,
        output_dir: Path,
        fmt: str = "json",
        include_venv_module: bool = True,
    ) -> list[Path]:
        """
        Write each successfully-probed module to *output_dir* as JSON or YAML.

        Parameters
        ----------
        report:
            A report returned by ``probe()``.  Only packages in
            ``report.successful_packages`` get a file written.
        output_dir:
            Destination directory (created if absent).
        fmt:
            ``"json"`` (default) or ``"yaml"``.
        include_venv_module:
            When True (default), prepend a ``python-venv-setup`` module file
            that callers can include first in their manifest.

        Returns
        -------
        List of paths written (venv module first if included).
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        if include_venv_module:
            venv_mod = _build_venv_module(self.sdk, self.runtime_version)
            venv_path = self._write_module(venv_mod, output_dir, fmt)
            written.append(venv_path)
            logger.info("Wrote venv module: %s", venv_path)

        for pkg_name, module_dict in report.modules.items():
            path = self._write_module(module_dict, output_dir, fmt)
            written.append(path)
            logger.info("Wrote module: %s", path)

        return written

    # ------------------------------------------------------------------
    # Internal — orchestration
    # ------------------------------------------------------------------

    def _make_work_dir(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="pfmr-probe-"))
        self._owned_work_dir = d
        return d

    def _probe(
        self,
        packages: list[ResolvedPackage],
        work_dir: Path,
    ) -> SandboxProbeReport:

        report = SandboxProbeReport(
            probed_packages=[p.name for p in packages],
        )

        # --- preflight ---
        if not self.is_available():
            report.ran = False
            report.skip_reason = (
                "flatpak not found. "
                "Install it with your distribution package manager (e.g. apt install flatpak)"
            )
            logger.warning("Probe skipped: %s", report.skip_reason)
            return report

        if not packages:
            report.ran = True
            report.build_possible = True
            return report

        build_dir = work_dir / "build"
        runner = SandboxRunner(
            build_dir=build_dir,
            sdk=self.sdk,
            runtime=self.runtime,
            runtime_version=self.runtime_version,
            sdk_extensions=self.sdk_extensions,
            timeout=self._command_timeout,
        )

        # --- initialise sandbox ---
        init_result = runner.init()
        report.stdout += init_result.stdout
        report.stderr += init_result.stderr

        if not init_result.succeeded:
            report.ran = True
            report.exit_code = init_result.exit_code
            report.build_possible = False
            errors = parse_errors(
                init_result.stderr, init_result.stdout, context="sandbox-init"
            )
            report.errors.extend(errors)
            _apply_errors_to_report(report, errors)
            logger.error("Sandbox init failed — probe aborted")
            return report

        # --- set up Python venv (once per session) ---
        venv_result = runner.run(_VENV_SETUP_SH, timeout=180)
        report.stdout += venv_result.stdout
        report.stderr += venv_result.stderr
        if not venv_result.succeeded:
            logger.warning("venv setup failed:\n%s", venv_result.stderr[-500:])

        # --- probe each package ---
        for pkg in packages:
            self._probe_package(pkg, runner, report)

        # --- high-level verdicts ---
        report.ran = True
        report.exit_code = 0 if not report.errors else 1
        report.build_possible = not any(
            e.error_type in (
                SandboxErrorType.MISSING_NATIVE_DEP,
                SandboxErrorType.MISSING_HEADER,
                SandboxErrorType.MISSING_PKGCONFIG,
                SandboxErrorType.BUILD_FAILURE,
            )
            for e in report.errors
        )
        report.sdk_sufficient = (
            len(report.missing_native_libs) == 0
            and len(report.missing_headers) == 0
            and len(report.missing_pkgconfig) == 0
        )

        logger.info(
            "Probe complete: %d errors, sdk_sufficient=%s, build_possible=%s, "
            "modules_generated=%d",
            len(report.errors),
            report.sdk_sufficient,
            report.build_possible,
            len(report.modules),
        )
        return report

    # ------------------------------------------------------------------
    # Internal — per-package probe steps
    # ------------------------------------------------------------------

    def _probe_package(
        self,
        pkg: ResolvedPackage,
        runner: SandboxRunner,
        report: SandboxProbeReport,
    ) -> None:
        logger.info("Probing package: %s==%s", pkg.name, pkg.version)

        # --- 1. Installation attempt ---
        install_result = self._try_install(pkg, runner)
        report.stdout += install_result.stdout
        report.stderr += install_result.stderr

        if not install_result.succeeded:
            errors = parse_errors(
                install_result.stderr,
                install_result.stdout,
                context=f"{pkg.name} install",
            )
            if not errors:
                errors = [SandboxError(
                    error_type=SandboxErrorType.BUILD_FAILURE,
                    missing=pkg.name,
                    source="stderr",
                    context=f"{pkg.name} install",
                    raw_line=install_result.stderr[-400:].strip(),
                )]
            report.errors.extend(errors)
            _apply_errors_to_report(report, errors)
            logger.warning(
                "Install failed for %s (exit %d)", pkg.name, install_result.exit_code
            )
            return

        # --- 2. Import test ---
        import_result = self._try_import(pkg, runner)
        report.stdout += import_result.stdout
        report.stderr += import_result.stderr

        import_ok = import_result.succeeded
        if not import_ok:
            errors = parse_errors(
                import_result.stderr,
                import_result.stdout,
                context=f"{pkg.name} import",
            )
            if not errors:
                errors = [SandboxError(
                    error_type=SandboxErrorType.IMPORT_ERROR,
                    missing=pkg.name,
                    source="import",
                    context=f"{pkg.name} import",
                    raw_line=import_result.stderr[-200:].strip(),
                )]
            report.errors.extend(errors)
            _apply_errors_to_report(report, errors)

        # --- 3. ldd check on installed .so files ---
        ldd_result = self._run_ldd(pkg, runner)
        if ldd_result:
            report.stdout += ldd_result.stdout
            ldd_errors = parse_errors(
                ldd_result.stderr,
                ldd_output=ldd_result.stdout,
                context=f"{pkg.name} ldd",
            )
            report.errors.extend(ldd_errors)
            _apply_errors_to_report(report, ldd_errors)

        # --- 4. pkg-config checks for declared native deps ---
        for dep in pkg.native_deps:
            if not dep.endswith(".so") and not dep.endswith(".h"):
                pc_result = runner.run(
                    f"pkg-config --exists {dep} && echo OK || echo MISSING"
                )
                if "MISSING" in pc_result.stdout or pc_result.exit_code != 0:
                    err = SandboxError(
                        error_type=SandboxErrorType.MISSING_PKGCONFIG,
                        missing=dep,
                        source="pkg-config",
                        context=f"{pkg.name} dep-check",
                    )
                    if not any(
                        e.missing == dep
                        and e.error_type == SandboxErrorType.MISSING_PKGCONFIG
                        for e in report.errors
                    ):
                        report.errors.append(err)
                        if dep not in report.missing_pkgconfig:
                            report.missing_pkgconfig.append(dep)

        # --- 5. Generate module (only when import succeeded and no blocking errors) ---
        blocking = {
            SandboxErrorType.MISSING_NATIVE_DEP,
            SandboxErrorType.MISSING_HEADER,
            SandboxErrorType.MISSING_PKGCONFIG,
            SandboxErrorType.BUILD_FAILURE,
        }
        pkg_errors = [
            e for e in report.errors
            if e.context and pkg.name in e.context
        ]
        has_blocking = any(e.error_type in blocking for e in pkg_errors)

        if import_ok and not has_blocking:
            module_dict = _build_pip_module(pkg)
            report.modules[pkg.name] = module_dict
            report.successful_packages.append(pkg.name)
            logger.info("Module generated for %s", pkg.name)
        else:
            reason = "import failed" if not import_ok else "blocking native errors"
            logger.info("Skipping module generation for %s: %s", pkg.name, reason)

    # ------------------------------------------------------------------
    # Internal — individual probe actions
    # ------------------------------------------------------------------

    def _try_install(self, pkg: ResolvedPackage, runner: SandboxRunner):
        """Attempt to install *pkg* into the sandbox venv via uv or pip."""
        spec = f"{pkg.name}=={pkg.version}"
        if self._use_uv:
            cmd = f"/app/venv/bin/uv pip install --no-cache {spec}"
        else:
            cmd = f"/app/venv/bin/pip install --no-cache-dir {spec}"
        return runner.run(cmd, timeout=self._command_timeout)

    def _try_import(self, pkg: ResolvedPackage, runner: SandboxRunner):
        """
        Attempt to import the package inside the sandbox.

        The import name is derived via _derive_import_name() which consults a
        well-known override table before falling back to normalisation
        (lower-case, hyphens → underscores).  The test script also prints the
        installed version so callers can verify the expected version was used.
        """
        import_name = _derive_import_name(pkg.name)
        script = (
            f"/app/venv/bin/python -c \""
            f"import {import_name}; "
            f"v = getattr({import_name}, '__version__', 'unknown'); "
            f"print('IMPORT_OK', v)"
            f"\""
        )
        return runner.run(script, timeout=self._command_timeout)

    def _run_ldd(self, pkg: ResolvedPackage, runner: SandboxRunner):
        """
        Find all .so files installed by *pkg* in the venv site-packages and
        run ``ldd`` on each one inside the sandbox.

        Returns the RunResult whose stdout contains concatenated ldd output
        (sectioned by ``=== LDD <path> ===`` markers), or None if the package
        has no .so files.

        The output is parsed by ``parse_errors(..., ldd_output=...)`` in the
        caller.
        """
        pkg_dir = _pkg_dir_name(pkg.name)
        script = _LDD_SCRIPT_TEMPLATE.format(pkg_dir=pkg_dir)

        result = runner.run(script, timeout=self._command_timeout)

        # If stdout is empty or contains no ldd sections, the package has no
        # .so files — return None to skip ldd error parsing.
        if not result.stdout.strip() or "=== LDD" not in result.stdout:
            return None

        return result

    # ------------------------------------------------------------------
    # Internal — output serialisation
    # ------------------------------------------------------------------

    def _write_module(self, module_dict: dict, output_dir: Path, fmt: str) -> Path:
        """Serialise a single module dict to *output_dir* in *fmt* format."""
        name = module_dict.get("name", "module")
        if fmt == "yaml":
            ext = ".yaml"
            content = yaml.dump(
                module_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        else:
            ext = ".json"
            content = json.dumps(module_dict, indent=2, ensure_ascii=False) + "\n"

        path = output_dir / f"{name}{ext}"
        path.write_text(content, encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_errors_to_report(report: SandboxProbeReport, errors: list[SandboxError]) -> None:
    """Distribute parsed errors into the typed lists on the report."""
    for err in errors:
        if err.error_type == SandboxErrorType.MISSING_NATIVE_DEP:
            if err.missing not in report.missing_native_libs:
                report.missing_native_libs.append(err.missing)
                report.sdk_sufficient = False
        elif err.error_type == SandboxErrorType.MISSING_HEADER:
            if err.missing not in report.missing_headers:
                report.missing_headers.append(err.missing)
                report.sdk_sufficient = False
        elif err.error_type == SandboxErrorType.MISSING_PKGCONFIG:
            if err.missing not in report.missing_pkgconfig:
                report.missing_pkgconfig.append(err.missing)
        elif err.error_type in (
            SandboxErrorType.MISSING_PYTHON_PKG, SandboxErrorType.IMPORT_ERROR
        ):
            if err.missing not in report.missing_python_packages:
                report.missing_python_packages.append(err.missing)