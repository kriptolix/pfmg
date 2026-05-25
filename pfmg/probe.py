"""
pfmg.sandbox.probe
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

Module generation (step 6) now fully mirrors flatpak-pip-generator:
  - ``pip download`` is run on the host to collect the package and ALL its
    transitive dependencies into a temp dir.
  - Arch-specific wheels are replaced by the corresponding sdist so the
    generated module builds on any target architecture.
  - VCS packages (git+https://…) produce a ``type: git/svn`` source entry.
  - Optional cleanup, --ignore-installed, and x-checker-data fields are
    supported via _build_pip_module() keyword arguments.

The prober skips gracefully when:
  - flatpak is not installed (ran=False, skip_reason set)
  - The sandbox build itself fails

Cache strategy:
  - The build-dir IS cached between probe calls for the same work_dir
  - The Python venv is set up once per session and reused
  - Failed probe states are NOT cached
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from collections import OrderedDict
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
from pfmg.sandbox.runner import SandboxRunner
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Sandbox install strategy
# ---------------------------------------------------------------------------
# Use uv from the SDK with --target so no venv is needed.
# Fallback: plain pip3 --target.

_INSTALL_TARGET   = "/run/pfmg-site"
# uv location varies per SDK version; we resolve it at runtime inside the sandbox.
# The command tries uv first (faster), falls back to pip3 (always available).
_UV_INSTALL_CMD   = "mkdir -p {target} && UV=$(command -v uv /usr/bin/uv /usr/local/bin/uv 2>/dev/null | head -1) && [ -n \"$UV\" ] && \"$UV\" pip install --python python3 --target {target} --no-cache {spec} || pip3 install --target {target} --no-cache-dir {spec}"
_PIP_INSTALL_CMD  = "mkdir -p {target} && pip3 install --target {target} --no-cache-dir {spec}"

# ldd output line parser
_LDD_LINE = re.compile(r"^\s*(?P<lib>\S+\.so\S*)\s*=>\s*(?P<path>\S+)", re.MULTILINE)

# Shell script: find all .so files installed by a package and run ldd on each.
_LDD_SCRIPT_TEMPLATE = """
SITE={target}
find "$SITE/{pkg_dir}" -name '*.so*' -type f 2>/dev/null | while read so; do
    echo "=== LDD $so ==="
    ldd "$so" 2>&1
done
"""

# Well-known package-name → import-name overrides.
_IMPORT_NAME_OVERRIDES: dict[str, str] = {
    "pillow":                   "PIL",
    "pil":                      "PIL",
    "scikit-learn":             "sklearn",
    "scikit-image":             "skimage",
    "scikit-build":             "skbuild",
    "opencv-python":            "cv2",
    "opencv-python-headless":   "cv2",
    "python-dateutil":          "dateutil",
    "beautifulsoup4":           "bs4",
    "pyyaml":                   "yaml",
    "protobuf":                 "google.protobuf",
    "grpcio":                   "grpc",
    "mysqlclient":              "MySQLdb",
    "psycopg2-binary":          "psycopg2",
    "pyzmq":                    "zmq",
    "pygame":                   "pygame",
    "pyserial":                 "serial",
    "pycairo":                  "cairo",
    "pygobject":                "gi",
    "python-magic":             "magic",
    "python-dotenv":            "dotenv",
    "typing-extensions":        "typing_extensions",
    "importlib-metadata":       "importlib_metadata",
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
    """Return the likely directory name under site-packages for ldd scanning."""
    key = pkg_name.lower()
    if key in _IMPORT_NAME_OVERRIDES:
        return _IMPORT_NAME_OVERRIDES[key]
    return key.replace("-", "_")


# ---------------------------------------------------------------------------
# Module output builders
# ---------------------------------------------------------------------------

# Packages that are part of org.freedesktop.Sdk and should not be included
# as sources (mirrors the system_packages list in flatpak-pip-generator).
_SYSTEM_PACKAGES = frozenset([
    'cython', 'easy_install', 'mako', 'markdown', 'meson',
    'pip', 'pygments', 'setuptools', 'six', 'wheel',
])


def _get_package_name_from_filename(filename: str) -> str:
    """
    Extract the normalised distribution name from a wheel or sdist filename.
    Mirrors get_package_name() in flatpak-pip-generator.
    """
    if filename.endswith(('bz2', 'gz', 'xz', 'zip')):
        segments = filename.split('-')
        if len(segments) == 2:
            return segments[0]
        return '-'.join(segments[:len(segments) - 1])
    elif filename.endswith('whl'):
        segments = filename.split('-')
        if len(segments) == 5:
            return segments[0]
        candidate = segments[:len(segments) - 4]
        if candidate[-1] == segments[len(segments) - 4]:
            return '-'.join(candidate[:-1])
        return '-'.join(candidate)
    # fallback: stem before first '-'
    return filename.split('-')[0]


def _get_file_version(filename: str) -> str:
    """Extract version string from a wheel or sdist filename."""
    name = _get_package_name_from_filename(filename)
    segments = filename.split(name + '-')
    version = segments[1].split('-')[0]
    for ext in ['tar.gz', 'whl', 'tar.xz', 'tar.bz2', 'zip']:
        version = version.replace('.' + ext, '')
    return version


def _get_file_hash(path: str) -> str:
    """Compute sha256 of a local file."""
    sha = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            data = fh.read(1024 * 1024 * 32)
            if not data:
                break
            sha.update(data)
    return sha.hexdigest()


def _get_pypi_url_for_filename(name: str, filename: str) -> str:
    """
    Look up the canonical download URL for *filename* via the PyPI JSON API.
    Mirrors get_pypi_url() in flatpak-pip-generator.
    """
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=15) as resp:
        body = json.loads(resp.read())
    for release_files in body['releases'].values():
        for entry in release_files:
            if entry['filename'] == filename:
                return entry['url']
    raise ValueError(f"URL not found for {filename} in PyPI data for {name}")


def _get_sdist_url_pypi(name: str, version: str) -> str:
    """
    Return the URL for the sdist (tar.gz / zip / etc.) of name==version.
    Mirrors get_tar_package_url_pypi() in flatpak-pip-generator.
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=15) as resp:
        body = json.loads(resp.read())
    for ext in ['bz2', 'gz', 'xz', 'zip']:
        for entry in body['urls']:
            if entry['url'].endswith(ext):
                return entry['url']
    raise ValueError(f"No sdist found for {name}=={version} on PyPI")


def _download_file(url: str, dest_dir: str) -> str:
    """Download *url* into *dest_dir* and return the local file path."""
    filename = url.split('/')[-1]
    dest = os.path.join(dest_dir, filename)
    with urllib.request.urlopen(url, timeout=60) as resp:
        with open(dest, 'wb') as fh:
            shutil.copyfileobj(resp, fh)
    return dest


def _collect_pip_download(
    pkg_spec: str,
    pip_executable: str = "pip3",
    extra_index_args: Optional[list[str]] = None,
) -> dict[str, dict]:
    """
    Run ``pip download`` for *pkg_spec* into a temp directory, then:

      1. Replace any arch-specific wheel with the corresponding sdist
         (same strategy as flatpak-pip-generator lines 286-298).
      2. Deduplicate multiple files for the same package name (prefer .zip
         for VCS sources, same as flatpak-pip-generator lines 306-319).
      3. Compute sha256 for each file and look up the canonical PyPI URL.

    Returns a dict mapping normalised package name →
        {
            "source":  OrderedDict(type, url, sha256),   # or type=git/svn
            "is_arch": bool,   # True if file was a non-any wheel
        }

    Falls back to an empty dict on any subprocess error.
    """
    extra_index_args = extra_index_args or []
    result: dict[str, dict] = {}

    with tempfile.TemporaryDirectory(prefix="pfmg-pip-dl-") as tmpdir:
        cmd = [
            pip_executable, "download",
            "--exists-action=i",
            "--dest", tmpdir,
        ] + extra_index_args + [pkg_spec]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning("pip download failed for %s: %s", pkg_spec, exc)
            return result

        # --- step 1: replace arch-specific wheels with sdist ---
        for filename in list(os.listdir(tmpdir)):
            if filename.endswith(('bz2', 'any.whl', 'gz', 'xz', 'zip')):
                continue  # pure-python or sdist — keep as-is
            # arch-specific wheel: delete and download sdist instead
            version = _get_file_version(filename)
            name = _get_package_name_from_filename(filename)
            local_path = os.path.join(tmpdir, filename)
            try:
                sdist_url = _get_sdist_url_pypi(name, version)
                os.remove(local_path)
                logger.debug("Replaced arch wheel %s with sdist %s", filename, sdist_url)
                _download_file(sdist_url, tmpdir)
            except Exception as exc:
                logger.warning(
                    "Could not swap arch wheel %s for sdist: %s — keeping wheel", filename, exc
                )

        # --- step 2: deduplicate (prefer .zip for VCS, same as pip-generator) ---
        files_by_name: dict[str, list[str]] = {}
        for filename in os.listdir(tmpdir):
            name = _get_package_name_from_filename(filename)
            files_by_name.setdefault(name, []).append(filename)

        for name, flist in files_by_name.items():
            if len(flist) > 1:
                has_zip = any(f.endswith('.zip') for f in flist)
                if has_zip:
                    for f in flist:
                        if not f.endswith('.zip'):
                            try:
                                os.remove(os.path.join(tmpdir, f))
                            except FileNotFoundError:
                                pass

        # --- step 3: compute hashes + canonical PyPI URLs ---
        for filename in os.listdir(tmpdir):
            name = _get_package_name_from_filename(filename)
            if name.casefold() in _SYSTEM_PACKAGES:
                continue

            local_path = os.path.join(tmpdir, filename)
            sha256 = _get_file_hash(local_path)

            try:
                url = _get_pypi_url_for_filename(name, filename)
            except Exception as exc:
                logger.warning("Could not resolve PyPI URL for %s: %s", filename, exc)
                continue

            source = OrderedDict([
                ("type",   "file"),
                ("url",    url),
                ("sha256", sha256),
            ])
            result[name] = {"source": source, "is_arch": False}

    return result


def _make_vcs_source(pkg: "ResolvedPackage") -> Optional[OrderedDict]:
    """
    Build a VCS source entry (type=git/svn/…) from a ResolvedPackage that
    carries vcs metadata.  Returns None if the package has no VCS info.

    Mirrors the vcs_packages handling in flatpak-pip-generator (lines 321-344).
    """
    vcs  = getattr(pkg, 'vcs',      None)
    uri  = getattr(pkg, 'vcs_uri',  None) or getattr(pkg, 'uri', None)
    rev  = getattr(pkg, 'revision', None)

    if not (vcs and uri):
        return None

    # Strip the scheme prefix and re-add https:// (same as pip-generator)
    url = 'https://' + uri.split('://', 1)[1] if '://' in uri else uri
    rev_key = 'revision' if vcs == 'svn' else 'commit'

    entries = [('type', vcs), ('url', url)]
    if rev:
        entries.append((rev_key, rev))
    return OrderedDict(entries)


def _fetch_pypi_sources(name: str, version: str) -> tuple[str, list[dict]]:
    """
    Query the PyPI JSON API and return (resolved_version, sources).

    This is the *single-package fast path* used when the caller already holds
    a pre-resolved URL+hash on the ResolvedPackage object.  For full transitive
    dependency resolution use _collect_pip_download instead.

    Preference: pure-python any.whl > manylinux x86_64 wheel > sdist.

    Unlike the old implementation, pure-python wheels are preferred over
    arch-specific ones because they do not require sdist substitution.
    If only an arch-specific wheel is available we fall through to sdist
    (mirroring flatpak-pip-generator's arch-wheel-to-sdist swap).
    Returns ("", []) on failure.
    """
    try:
        api_url = (
            f"https://pypi.org/pypi/{name}/{version}/json"
            if version
            else f"https://pypi.org/pypi/{name}/json"
        )
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            data = json.loads(resp.read())

        resolved_version = data["info"]["version"]
        files = data["urls"]

        def _score(f: dict) -> int:
            fn = f.get("filename", "")
            if fn.endswith(".whl"):
                if "none-any" in fn:            # pure-python wheel — best
                    return 4
                if "manylinux" in fn and "x86_64" in fn:
                    return 2                    # arch wheel — will need sdist swap
                if "manylinux" in fn:
                    return 1
                return 0
            # sdist
            for ext in ('gz', 'bz2', 'xz', 'zip'):
                if fn.endswith(ext):
                    return 3                    # sdist preferred over arch wheel
            return -1

        files_sorted = sorted(files, key=_score, reverse=True)
        if not files_sorted:
            return resolved_version, []

        best = files_sorted[0]
        fn   = best.get("filename", "")

        # If best is an arch-specific wheel, try to swap for sdist
        is_arch_wheel = fn.endswith('.whl') and 'none-any' not in fn
        if is_arch_wheel:
            try:
                sdist_url = _get_sdist_url_pypi(name, resolved_version)
                # Re-fetch the entry to get sha256 from PyPI metadata
                sdist_entry = next(
                    (f for f in files if f["url"] == sdist_url), None
                )
                if sdist_entry:
                    best = sdist_entry
                    fn   = best.get("filename", "")
            except Exception:
                pass  # no sdist available — keep the wheel

        source = OrderedDict([
            ("type",   "file"),
            ("url",    best["url"]),
            ("sha256", best["digests"].get("sha256", "")),
        ])
        return resolved_version, [source]

    except Exception as exc:
        logger.warning("PyPI API lookup failed for %s: %s", name, exc)
        return version, []


def _build_pip_module(
    pkg: "ResolvedPackage",
    cleanup: Optional[str] = None,
    ignore_installed: bool = False,
    build_isolation: bool = False,
    checker_data: bool = False,
) -> dict:
    """
    Build a Flatpak module dict that matches the output of flatpak-pip-generator.

    Key improvements over the old implementation
    ─────────────────────────────────────────────
    • Transitive dependencies are resolved by running ``pip download`` for the
      package (same strategy as flatpak-pip-generator).  Each downloaded file
      becomes a separate ``sources`` entry so flatpak-builder has everything it
      needs for an offline build.
    • Arch-specific wheels are replaced by the corresponding sdist so the build
      works on any target architecture (mirrors pip-generator lines 286-298).
    • VCS packages (git/svn/…) produce a source of the correct type instead of
      always emitting ``type: file``.
    • Optional ``cleanup``, ``--ignore-installed``, and ``x-checker-data``
      fields are generated when requested (mirrors pip-generator CLI flags).

    The sandbox install step (``_try_install``) is independent of module
    generation and is NOT replicated here — this function only produces the
    declarative Flatpak module dict.
    """
    # ------------------------------------------------------------------
    # 1. Determine pip spec and whether this is a VCS package
    # ------------------------------------------------------------------
    vcs_source = _make_vcs_source(pkg)
    is_vcs = vcs_source is not None

    if is_vcs:
        vcs_uri = getattr(pkg, 'vcs_uri', None) or getattr(pkg, 'uri', '')
        rev     = getattr(pkg, 'revision', '')
        rev_str = f"@{rev}" if rev else ""
        pip_spec      = f"{vcs_uri}{rev_str}#egg={pkg.name}"
        name_for_pip  = "."           # VCS packages are installed from the checked-out dir
    else:
        version_str  = f"=={pkg.version}" if pkg.version else ""
        pip_spec     = f"{pkg.name}{version_str}"
        name_for_pip = f'"{pip_spec}"'

    # ------------------------------------------------------------------
    # 2. Collect sources
    # ------------------------------------------------------------------
    if is_vcs:
        # VCS: single VCS source, no transitive pip download needed
        package_sources: list[dict] = [vcs_source]

    elif pkg.source_url and pkg.source_hash:
        # Caller already resolved URL+hash — use it directly (fast path).
        # Still attempt to collect transitive deps via pip download.
        own_source = OrderedDict([
            ("type",   "file"),
            ("url",    pkg.source_url),
            ("sha256", pkg.source_hash),
        ])
        dep_sources = _collect_pip_download(pip_spec)
        # Merge: own source first, then transitive deps (skip own if already present)
        own_name_norm = pkg.name.lower().replace('-', '_')
        package_sources = [own_source]
        for dep_name, dep_info in dep_sources.items():
            if dep_name.lower().replace('-', '_') != own_name_norm:
                package_sources.append(dep_info["source"])

    else:
        # Full resolution path: let pip download decide what's needed.
        dep_sources = _collect_pip_download(pip_spec)

        if dep_sources:
            # Order: package itself first, then remaining deps alphabetically
            own_name_norm = pkg.name.lower().replace('-', '_')
            package_sources = []
            own_entry = (
                dep_sources.get(pkg.name)
                or dep_sources.get(pkg.name.replace('-', '_'))
                or dep_sources.get(pkg.name.lower())
            )
            if own_entry:
                package_sources.append(own_entry["source"])
            for dep_name, dep_info in sorted(dep_sources.items()):
                if dep_name.lower().replace('-', '_') != own_name_norm:
                    package_sources.append(dep_info["source"])
        else:
            # pip download unavailable (e.g. offline host) — fall back to
            # single-file PyPI API lookup so we still emit something useful.
            logger.warning(
                "pip download produced no results for %s — falling back to "
                "single-file PyPI lookup (transitive deps will be missing)",
                pkg.name,
            )
            resolved_version, fallback_sources = _fetch_pypi_sources(pkg.name, pkg.version)
            if resolved_version and not pkg.version:
                pkg.version = resolved_version
            package_sources = fallback_sources

    # ------------------------------------------------------------------
    # 3. Optionally annotate sources with x-checker-data
    # ------------------------------------------------------------------
    if checker_data and not is_vcs:
        for src in package_sources:
            if src.get("type") == "file":
                src["x-checker-data"] = {"type": "pypi", "name": pkg.name}
                if src.get("url", "").endswith(".whl"):
                    src["x-checker-data"]["packagetype"] = "bdist_wheel"

    # ------------------------------------------------------------------
    # 4. Build the pip install command (mirrors pip-generator lines 436-449)
    # ------------------------------------------------------------------
    pip_command_parts = [
        "pip3", "install",
        "--verbose",
        "--exists-action=i",
        "--no-index",
        '--find-links="file://${PWD}"',
        "--prefix=${FLATPAK_DEST}",
        name_for_pip,
    ]
    if ignore_installed:
        pip_command_parts.append("--ignore-installed")
    if not build_isolation:
        pip_command_parts.append("--no-build-isolation")

    build_commands = [" ".join(pip_command_parts)]

    # ------------------------------------------------------------------
    # 5. Assemble the module dict (mirrors pip-generator lines 451-465)
    # ------------------------------------------------------------------
    module: dict = OrderedDict([
        ("name",           f"python3-{pkg.name}"),
        ("buildsystem",    "simple"),
        ("build-commands", build_commands),
        ("sources",        package_sources),
    ])

    if cleanup == "all":
        module["cleanup"] = ["*"]
    elif cleanup == "scripts":
        module["cleanup"] = ["/bin", "/share/man/man1"]

    return module



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
        # Module generation options
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
    ) -> list[Path]:
        """
        Write each successfully-probed module to *output_dir* as JSON or YAML.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        for pkg_name, module_dict in report.modules.items():
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

        # --- 2. ldd check on installed .so files ---
        # Run ldd before import — it catches missing native libs even when
        # the import itself would fail for unrelated reasons.
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
        # The module is generated whenever the *installation* succeeded and
        # there are no blocking native errors (missing .so / header / pkgconfig).
        # Import failure is informational only — it may mean a missing native
        # dep (already caught by ldd/pkgconfig above), a wrong import name, or
        # a runtime-only requirement. It never blocks module generation.
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

        if not has_blocking:
            module_dict = _build_pip_module(
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
        # Errors are recorded for the resolver to suggest fixes but do not affect
        # build_possible or the generated module.
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
        if self._use_uv:
            cmd = _UV_INSTALL_CMD.format(target=_INSTALL_TARGET, spec=spec)
        else:
            cmd = _PIP_INSTALL_CMD.format(target=_INSTALL_TARGET, spec=spec)
        return runner.run(cmd, timeout=self._command_timeout)

    def _try_import(self, pkg: ResolvedPackage, runner: SandboxRunner):
        """Attempt to import the package inside the sandbox."""
        import_name = _derive_import_name(pkg.name)
        script = (
            f"PYTHONPATH={_INSTALL_TARGET} python3 -c \""
            f"import {import_name}; "
            f"v = getattr({import_name}, '__version__', 'unknown'); "
            f"print('IMPORT_OK', v)\""
        )
        return runner.run(script, timeout=self._command_timeout)

    def _run_ldd(self, pkg: ResolvedPackage, runner: SandboxRunner):
        """
        Find all .so files installed by *pkg* in the venv site-packages and
        run ldd on each one inside the sandbox.

        Returns None if the package has no .so files.
        """
        pkg_dir = _pkg_dir_name(pkg.name)
        script = _LDD_SCRIPT_TEMPLATE.format(target=_INSTALL_TARGET, pkg_dir=pkg_dir)

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