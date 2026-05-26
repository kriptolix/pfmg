"""
pfmg.sandbox.probe
~~~~~~~~~~~~~~~~~~~
BuildSandboxProber — Phase 3 core component.

Orchestrates the full sandbox probe sequence for a set of Python packages:

  1. Initialise the Flatpak build directory (flatpak build-init via SandboxRunner)
  2. For each package:
       a. Attempt ``uv pip install <pkg>`` (or pip3) inside the sandbox
       b. If install fails → parse errors, record missing deps
       c. Run ldd on any .so files found under the install target → record missing libs
       d. Run pkg-config checks for declared native_deps
       e. If no blocking native errors → generate a Flatpak module dict
       f. Attempt ``python -c "import <pkg>"`` inside the sandbox (informational)
  3. Collate all errors into a SandboxProbeReport with high-level verdicts

Module generation (step 2e) is delegated to ``pfmg.sandbox.module.build_pip_module``
which fully mirrors flatpak-pip-generator (transitive deps, sdist swap, VCS sources).

The prober skips gracefully when:
  - flatpak is not installed (ran=False, skip_reason set)
  - The sandbox build-init itself fails

Cache strategy:
  - The build-dir IS cached between probe calls for the same work_dir
  - Failed probe states are NOT cached
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from pfmg.models import (
    ResolvedPackage,
    ResolutionResult,
    SandboxError,
    SandboxErrorType,
    SandboxProbeReport,
)
from pfmg.errors import parse_errors
from pfmg.module import build_pip_module
from pfmg.sandbox.runner import SandboxRunner
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Sandbox install strategy
# ---------------------------------------------------------------------------
# Use uv from the SDK with --target so no venv is needed.
# Fallback: plain pip3 --target.

_INSTALL_TARGET  = "/run/pfmg-site"
_UV_INSTALL_CMD  = (
    "mkdir -p {target} && "
    "UV=$(command -v uv /usr/bin/uv /usr/local/bin/uv 2>/dev/null | head -1) && "
    '[ -n "$UV" ] && "$UV" pip install --python python3 --target {target} --no-cache {spec} '
    "|| pip3 install --target {target} --no-cache-dir {spec}"
)
_PIP_INSTALL_CMD = "mkdir -p {target} && pip3 install --target {target} --no-cache-dir {spec}"

# Shell script: find all .so files installed by a package and run ldd on each.
_LDD_SCRIPT_TEMPLATE = """
SITE={target}
find "$SITE/{pkg_dir}" -name '*.so*' -type f 2>/dev/null | while read so; do
    echo "=== LDD $so ==="
    ldd "$so" 2>&1
done
"""

# ---------------------------------------------------------------------------
# Package-name → import-name resolution
# ---------------------------------------------------------------------------

_IMPORT_NAME_OVERRIDES: dict[str, str] = {
    "pillow":                  "PIL",
    "pil":                     "PIL",
    "scikit-learn":            "sklearn",
    "scikit-image":            "skimage",
    "scikit-build":            "skbuild",
    "opencv-python":           "cv2",
    "opencv-python-headless":  "cv2",
    "python-dateutil":         "dateutil",
    "beautifulsoup4":          "bs4",
    "pyyaml":                  "yaml",
    "protobuf":                "google.protobuf",
    "grpcio":                  "grpc",
    "mysqlclient":             "MySQLdb",
    "psycopg2-binary":         "psycopg2",
    "pyzmq":                   "zmq",
    "pygame":                  "pygame",
    "pyserial":                "serial",
    "pycairo":                 "cairo",
    "pygobject":               "gi",
    "python-magic":            "magic",
    "python-dotenv":           "dotenv",
    "typing-extensions":       "typing_extensions",
    "importlib-metadata":      "importlib_metadata",
}


def _derive_import_name(pkg_name: str) -> str:
    """
    Best-effort derivation of the Python import name from a PyPI package name.

    Resolution order:
      1. Explicit override table (_IMPORT_NAME_OVERRIDES)
      2. Normalise: lower-case, hyphens → underscores
    """
    key = pkg_name.lower()
    return _IMPORT_NAME_OVERRIDES.get(key, key.replace("-", "_"))


def _pkg_dir_name(pkg_name: str) -> str:
    """Return the likely directory name under site-packages for ldd scanning."""
    key = pkg_name.lower()
    return _IMPORT_NAME_OVERRIDES.get(key, key.replace("-", "_"))


# ---------------------------------------------------------------------------
# Report helpers
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
        work_dir: Optional[Path] = None,
        keep_work_dir: bool = False,
        command_timeout: int = 120,
        build_timeout: int = 600,
        use_uv: bool = True,
        # Module generation options (mirror flatpak-pip-generator CLI flags)
        module_cleanup: Optional[str] = None,
        module_ignore_installed: bool = False,
        module_build_isolation: bool = False,
        module_checker_data: bool = False,
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
        self._owned_work_dir: Optional[Path] = None
        # Module generation options forwarded to build_pip_module()
        self._module_cleanup = module_cleanup
        self._module_ignore_installed = module_ignore_installed
        self._module_build_isolation = module_build_isolation
        self._module_checker_data = module_checker_data

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
        Run the full probe sequence for *packages*.
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
    ) -> list[Path]:
        """
        Write each successfully-probed module to *output_dir* as JSON or YAML.
        Returns a list of paths to the written files.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for module_dict in report.modules.values():
            path = self._write_module(module_dict, output_dir, fmt)
            written.append(path)
            logger.info("Wrote module: %s", path)
        return written

    # ------------------------------------------------------------------
    # Internal — orchestration
    # ------------------------------------------------------------------

    def _make_work_dir(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="pfmg-probe-"))
        self._owned_work_dir = d
        return d

    def _probe(
        self,
        packages: list[ResolvedPackage],
        work_dir: Path,
    ) -> SandboxProbeReport:

        report = SandboxProbeReport(probed_packages=[p.name for p in packages])

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
            logger.warning("Install failed for %s (exit %d)", pkg.name, install_result.exit_code)
            return

        # --- 2. ldd check on installed .so files ---
        # Run before import — catches missing native libs even when the import
        # would fail for unrelated reasons.
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

        # --- 3. pkg-config checks for declared native deps ---
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

        # --- 4. Module generation ---
        # Generated whenever installation succeeded and there are no blocking
        # native errors.  Import failure is informational only — it never
        # blocks module generation.
        _BLOCKING = {
            SandboxErrorType.MISSING_NATIVE_DEP,
            SandboxErrorType.MISSING_HEADER,
            SandboxErrorType.MISSING_PKGCONFIG,
            SandboxErrorType.BUILD_FAILURE,
        }
        pkg_errors = [e for e in report.errors if e.context and pkg.name in e.context]
        has_blocking = any(e.error_type in _BLOCKING for e in pkg_errors)

        if not has_blocking:
            module_dict = build_pip_module(
                pkg,
                cleanup=self._module_cleanup,
                ignore_installed=self._module_ignore_installed,
                build_isolation=self._module_build_isolation,
                checker_data=self._module_checker_data,
            )
            report.modules[pkg.name] = module_dict
            report.successful_packages.append(pkg.name)
            logger.info("Module generated for %s", pkg.name)
        else:
            logger.info("Skipping module for %s: blocking native errors", pkg.name)

        # --- 5. Import test (informational) ---
        # Runs after module generation so a failed import never suppresses output.
        import_result = self._try_import(pkg, runner)
        report.stdout += import_result.stdout
        report.stderr += import_result.stderr

        if not import_result.succeeded:
            import_errors = parse_errors(
                import_result.stderr,
                import_result.stdout,
                context=f"{pkg.name} import",
            )
            if not import_errors:
                import_errors = [SandboxError(
                    error_type=SandboxErrorType.IMPORT_ERROR,
                    missing=pkg.name,
                    source="import",
                    context=f"{pkg.name} import",
                    raw_line=import_result.stderr[-200:].strip(),
                )]
            report.errors.extend(import_errors)
            _apply_errors_to_report(report, import_errors)
            logger.info(
                "Import check failed for %s (informational): %s",
                pkg.name, import_result.stderr[-120:].strip(),
            )

    # ------------------------------------------------------------------
    # Internal — individual probe actions
    # ------------------------------------------------------------------

    def _try_install(self, pkg: ResolvedPackage, runner: SandboxRunner):
        """Attempt to install *pkg* into the sandbox via uv or pip3 --target."""
        spec = f"{pkg.name}=={pkg.version}" if pkg.version else pkg.name
        cmd = (
            _UV_INSTALL_CMD if self._use_uv else _PIP_INSTALL_CMD
        ).format(target=_INSTALL_TARGET, spec=spec)
        return runner.run(cmd, timeout=self._command_timeout)

    def _try_import(self, pkg: ResolvedPackage, runner: SandboxRunner):
        import_name = _derive_import_name(pkg.name)
        # Heredoc evita todos os problemas de quoting: o Python recebe o script
        # exatamente como escrito, sem interferência do sh.
        script = "\n".join([
            f"export PYTHONPATH={_INSTALL_TARGET}:$PYTHONPATH",
            f"python3 << 'PYEOF'",
            f"import {import_name}",
            f"v = getattr({import_name}, '__version__', 'unknown')",
            f"print('IMPORT_OK', v)",
            f"PYEOF",
        ])
        return runner.run(script, timeout=self._command_timeout)

    def _run_ldd(self, pkg: ResolvedPackage, runner: SandboxRunner):
        """
        Find all .so files installed by *pkg* and run ldd on each inside the
        sandbox.  Returns None if the package has no .so files.
        """
        script = _LDD_SCRIPT_TEMPLATE.format(
            target=_INSTALL_TARGET,
            pkg_dir=_pkg_dir_name(pkg.name),
        )
        result = runner.run(script, timeout=self._command_timeout)
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
            content = yaml.dump(
                module_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            path = output_dir / f"{name}.yaml"
        else:
            content = json.dumps(module_dict, indent=2, ensure_ascii=False) + "\n"
            path = output_dir / f"{name}.json"
        path.write_text(content, encoding="utf-8")
        return path