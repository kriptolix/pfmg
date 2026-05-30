"""
pfmg.probe.probe
~~~~~~~~~~~~~~~~~~~

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

Module generation (step 2e) is delegated to ``pfmg.probe.module.build_pip_module``
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
import shutil
import tempfile
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml

from pfmg.utils.models import (
    FlatpakManifest,
    FlatpakModule,
    FlatpakSource,
    ResolvedPackage,    
    SandboxError,
    SandboxErrorType,
    SandboxProbeReport,
)

from pfmg.sandbox.runner import SandboxRunner
from pfmg.sandbox.parser import parse_errors
from pfmg.generation.collector import build_pip_module, resolve_import_to_pypi_name
from pfmg.utils.logging import get_logger

if TYPE_CHECKING:
    from pfmg.sandbox.runner import RunResult

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

def _pkg_dir_name(pkg_name: str) -> str:
    """Return the likely directory name under site-packages for ldd scanning."""
    key = pkg_name.lower()
    return key 


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
        # Explicit build-time dependencies supplied by the user via --build-dep.
        # Each entry is a PyPI package name (e.g. "meson-python").
        # These are probed and added as modules above the main package.
        build_deps: Optional[list[str]] = None,
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
        # build_deps: list of PyPI package names supplied explicitly by the user
        # via --build-dep (e.g. ["meson-python", "ninja"]).  These are probed
        # and added as modules above the main package in the generated manifest.
        self._build_deps: list[str] = build_deps or []
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

        # --- probe build-time dependencies first ---
        # Packages supplied via --build-dep (or auto-resolved in a previous
        # run) must be probed before the main packages so their modules are
        # present in report.modules when _try_build_module assembles the test
        # manifest.  We skip any dep whose name matches one of the main
        # packages to avoid double-probing.
        main_names = {p.name.lower().replace("-", "_") for p in packages}
        for dep_pypi_name in list(self._build_deps):
            norm = dep_pypi_name.lower().replace("-", "_")
            if norm in main_names:
                continue
            dep_pkg = ResolvedPackage(name=dep_pypi_name, version="")
            logger.info("Probing build dep: %s", dep_pypi_name)
            self._probe_package(dep_pkg, runner, report)

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

        # --- 5. Build test ---
        # Run the generated module through flatpak-builder to catch errors that
        # a silent pip install hides: missing compilers, headers, native libs,
        # and build-time Python dependencies (PEP 517 backend packages).
        # Only runs when a module was actually generated (no blocking errors).
        if not has_blocking:
            # Collect any build-deps already known (from --build-dep or a
            # previous auto-resolution pass stored in self._build_deps).
            known_build_dep_modules = self._collect_build_dep_modules(report)

            build_result = self._try_build_module(
                pkg, module_dict, runner,
                extra_modules=known_build_dep_modules,
            )
            report.stdout += build_result.stdout
            report.stderr += build_result.stderr

            if not build_result.succeeded:
                build_errors = parse_errors(
                    build_result.stderr,
                    build_result.stdout,
                    context=f"{pkg.name} build-test",
                )
                if not build_errors:
                    build_errors = [SandboxError(
                        error_type=SandboxErrorType.BUILD_FAILURE,
                        missing=pkg.name,
                        source="stderr",
                        context=f"{pkg.name} build-test",
                        raw_line=build_result.stderr[-400:].strip(),
                    )]
                report.errors.extend(build_errors)
                _apply_errors_to_report(report, build_errors)

                # --- 5a. Auto-resolve missing Python build deps ---
                # For each MISSING_PYTHON_PKG, IMPORT_ERROR, or MISSING_EXECUTABLE
                # error, try to find the PyPI package name automatically.
                # MISSING_EXECUTABLE covers tools like 'pythran' that meson looks
                # for via Program() — they are often pure-Python packages on PyPI.
                # Resolved deps get their module prepended above the main package
                # in report.modules.  Unresolved ones are reported with a hint.
                missing_py = [
                    e for e in build_errors
                    if e.error_type in (
                        SandboxErrorType.MISSING_PYTHON_PKG,
                        SandboxErrorType.IMPORT_ERROR,
                        SandboxErrorType.MISSING_EXECUTABLE,
                    )
                ]
                for err in missing_py:
                    self._try_resolve_build_dep(err.missing, pkg.name, report)

                logger.info(
                    "Build test failed for %s: %s",
                    pkg.name, build_result.stderr[-120:].strip(),
                )

        # --- 6. Reorder modules: build-deps first, target package last ---
        # Ensures the final report.modules dict is ordered so that any
        # build-time dependency module appears before the package that needs it.
        _reorder_modules(report, pkg.name)

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

    def _try_build_module(
        self,
        pkg: ResolvedPackage,
        module_dict: dict,
        runner: SandboxRunner,
        extra_modules: Optional[list[dict]] = None,
    ) -> RunResult:
        """
        Validate a generated module by building it inside a real Flatpak
        environment via ``flatpak run org.flatpak.Builder``.

        A minimal FlatpakManifest containing only the module under test is
        serialised to a temporary JSON file and passed to the builder.  This
        catches errors that silent pip installs hide — missing compilers,
        headers, native libraries — because the builder runs the full
        build-commands pipeline and reports every failure via stderr in the
        same format that parse_errors already understands.

        ``extra_modules``, when provided, are prepended to the manifest
        modules list so that build-time dependencies are available when
        the main package is built.

        Returns the RunResult from the builder invocation.
        """
        flatpak_module = _module_dict_to_flatpak(module_dict)
        dep_modules = [_module_dict_to_flatpak(m) for m in (extra_modules or [])]
        manifest = FlatpakManifest(
            app_id=f"org.pfmg.Test.{pkg.name}",
            runtime=self.runtime,
            runtime_version=self.runtime_version,
            sdk=self.sdk,
            sdk_extensions=self.sdk_extensions,
            modules=dep_modules + [flatpak_module],
        )
        return runner.build_manifest(manifest, timeout=self._build_timeout)

    def _collect_build_dep_modules(self, report: SandboxProbeReport) -> list[dict]:
        """
        Return module dicts for all build-deps that have already been
        successfully generated in this probe session (either from a previous
        auto-resolution or from --build-dep supplied by the user at startup).
        These are passed as ``extra_modules`` to ``_try_build_module`` so the
        builder can find them.
        """
        result = []
        for pypi_name in self._build_deps:
            # Normalise: package names in report.modules use the original
            # ResolvedPackage.name, which may differ in casing/hyphenation.
            normalised = pypi_name.lower().replace("-", "_")
            for mod_name, mod_dict in report.modules.items():
                if mod_name.lower().replace("-", "_") == normalised:
                    result.append(mod_dict)
                    break
        return result

    def _try_resolve_build_dep(
        self,
        import_name: str,
        context_pkg: str,
        report: SandboxProbeReport,
    ) -> None:
        """
        Attempt to resolve a missing Python build-time dependency.

        1. If ``import_name`` is already covered by a user-supplied
           ``--build-dep``, skip — the module will be present via
           ``_collect_build_dep_modules`` on the next run.
        2. Try the PyPI JSON API: GET /pypi/{import_name}/json.
           On success, record the canonical name and note it in the report.
        3. On failure (404 / network error), annotate the relevant
           SandboxError with a hint pointing the user to ``--build-dep``.
        """
        # Normalise for comparison
        norm = import_name.lower().replace("-", "_")

        # Already covered by a user-supplied --build-dep?
        for dep in self._build_deps:
            if dep.lower().replace("-", "_") == norm:
                logger.debug(
                    "Build dep '%s' already supplied via --build-dep, skipping auto-resolve",
                    import_name,
                )
                return

        # Already resolved automatically in a previous call?
        if any(
            k.lower().replace("-", "_") == norm
            for k in report.modules
        ):
            return

        logger.debug("Attempting PyPI auto-resolve for build dep '%s'", import_name)
        pypi_name = resolve_import_to_pypi_name(import_name)

        if pypi_name:
            logger.info(
                "Auto-resolved build dep '%s' → PyPI '%s' for %s",
                import_name, pypi_name, context_pkg,
            )
            # Record so _collect_build_dep_modules picks it up on reruns
            # within the same session and for report display.
            if pypi_name not in self._build_deps:
                self._build_deps.append(pypi_name)
            # Annotate the report so the user sees it was resolved.
            report.resolved_build_deps = getattr(report, "resolved_build_deps", {})
            report.resolved_build_deps[import_name] = pypi_name
        else:
            # Could not resolve — annotate matching errors with the hint.
            hint = (
                f"If you know the name of the Python package that provides "
                f"'{import_name}', use: --build-dep <package-name>"
            )
            for err in report.errors:
                if (
                    err.missing == import_name
                    and err.error_type in (
                        SandboxErrorType.MISSING_PYTHON_PKG,
                        SandboxErrorType.IMPORT_ERROR,
                    )
                    and hint not in err.context
                ):
                    err.context = (err.context + "\n" + hint).strip()
            logger.info(
                "Could not auto-resolve build dep '%s' for %s — hint added to report",
                import_name, context_pkg,
            )

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reorder_modules(report: SandboxProbeReport, target_pkg_name: str) -> None:
    """
    Reorder ``report.modules`` so that build-dep modules appear before the
    target package module.  This preserves the correct installation order
    when the caller writes the modules into a Flatpak manifest.

    The target package's module is moved to the end; all other modules
    (build-deps resolved automatically or via --build-dep) stay at the front
    in the order they were inserted.
    """
    if target_pkg_name not in report.modules:
        return
    target_module = report.modules.pop(target_pkg_name)
    # Re-insert at the end (dict preserves insertion order in Python 3.7+)
    report.modules[target_pkg_name] = target_module

def _module_dict_to_flatpak(module_dict: dict) -> FlatpakModule:
    """
    Convert a module dict produced by build_pip_module() into a FlatpakModule
    dataclass suitable for FlatpakManifest.

    build_pip_module uses kebab-case keys (build-commands, x-checker-data)
    and plain dicts for sources.  FlatpakModule uses snake_case fields and
    FlatpakSource dataclasses.  This function bridges the two.
    """
    sources: list[FlatpakSource] = []
    for s in module_dict.get("sources", []):
        sources.append(FlatpakSource(
            type=s.get("type", "file"),
            url=s.get("url"),
            sha256=s.get("sha256"),
            path=s.get("path"),
            dest_filename=s.get("dest-filename"),
            branch=s.get("branch"),
            commit=s.get("commit"),
            tag=s.get("tag"),
        ))

    return FlatpakModule(
        name=module_dict.get("name", ""),
        buildsystem=module_dict.get("buildsystem", "simple"),
        build_commands=module_dict.get("build-commands", []),
        sources=sources,
        cleanup=module_dict.get("cleanup", []),
        build_options=module_dict.get("build-options", {}),
        config_opts=module_dict.get("config-opts", []),
    )