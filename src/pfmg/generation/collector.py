"""
pfmg.probe.module
~~~~~~~~~~~~~~~~~~~~
Flatpak module generation from Python packages.

Responsible for producing the declarative ``module`` dict that
flatpak-builder consumes to install a Python package offline.  The logic
mirrors flatpak-pip-generator exactly:

  - ``pip download`` resolves the package and ALL transitive dependencies.
  - Arch-specific wheels are replaced by the corresponding sdist so the
    generated module builds on any target architecture.
  - VCS packages (git+https://…) produce a ``type: git/svn`` source entry
    instead of ``type: file``.
  - Optional ``cleanup``, ``--ignore-installed``, and ``x-checker-data``
    fields are supported.

This module has no dependency on SandboxRunner or any flatpak tooling —
it only needs network access (PyPI JSON API + pip download on the host).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from collections import OrderedDict
from typing import Optional, TYPE_CHECKING

from pfmg.utils.logging import get_logger

if TYPE_CHECKING:
    from pfmg.utils.models import ResolvedPackage

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Packages shipped with org.freedesktop.Sdk — must not appear in sources.
# Mirrors the system_packages list in flatpak-pip-generator.
SYSTEM_PACKAGES: frozenset[str] = frozenset([
    'cython', 'easy_install', 'mako', 'markdown', 'meson',
    'pip', 'pygments', 'setuptools', 'six', 'wheel',
])

# ---------------------------------------------------------------------------
# Filename utilities
# ---------------------------------------------------------------------------

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
    if filename.endswith('whl'):
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
    """Extract the version string from a wheel or sdist filename."""
    name = _get_package_name_from_filename(filename)
    segments = filename.split(name + '-')
    version = segments[1].split('-')[0]
    for ext in ['tar.gz', 'whl', 'tar.xz', 'tar.bz2', 'zip']:
        version = version.replace('.' + ext, '')
    return version


# ---------------------------------------------------------------------------
# PyPI API helpers
# ---------------------------------------------------------------------------

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


def _get_file_hash(path: str) -> str:
    """Compute the sha256 digest of a local file."""
    sha = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            data = fh.read(1024 * 1024 * 32)
            if not data:
                break
            sha.update(data)
    return sha.hexdigest()


def _download_file(url: str, dest_dir: str) -> str:
    """Download *url* into *dest_dir* and return the local file path."""
    filename = url.split('/')[-1]
    dest = os.path.join(dest_dir, filename)
    with urllib.request.urlopen(url, timeout=60) as resp:
        with open(dest, 'wb') as fh:
            shutil.copyfileobj(resp, fh)
    return dest


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

def collect_pip_download(
    pkg_spec: str,
    pip_executable: str = "pip3",
    extra_index_args: Optional[list[str]] = None,
) -> dict[str, dict]:
    """
    Run ``pip download`` for *pkg_spec* and return one source entry per file.

    Steps performed (mirroring flatpak-pip-generator):

      1. ``pip download <spec>`` fetches the package and all transitive deps.
      2. Arch-specific wheels are replaced by the corresponding sdist so the
         build works on any target architecture (mirrors pip-generator L286-298).
      3. Duplicate files for the same package name are deduplicated, preferring
         ``.zip`` for VCS sources (mirrors pip-generator L306-319).
      4. sha256 is computed for each file; canonical PyPI URL is resolved.

    Returns a dict mapping normalised package name →
        ``{"source": OrderedDict(type, url, sha256), "is_arch": bool}``

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
            subprocess.run(
                cmd, check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning("pip download failed for %s: %s", pkg_spec, exc)
            return result

        # --- step 1: replace arch-specific wheels with sdist ---
        for filename in list(os.listdir(tmpdir)):
            if filename.endswith(('bz2', 'any.whl', 'gz', 'xz', 'zip')):
                continue  # pure-python wheel or sdist — keep as-is
            # arch-specific wheel: delete and download the sdist instead
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
                    "Could not swap arch wheel %s for sdist: %s — keeping wheel",
                    filename, exc,
                )

        # --- step 2: deduplicate (prefer .zip for VCS sources) ---
        files_by_name: dict[str, list[str]] = {}
        for filename in os.listdir(tmpdir):
            name = _get_package_name_from_filename(filename)
            files_by_name.setdefault(name, []).append(filename)

        for name, flist in files_by_name.items():
            if len(flist) > 1 and any(f.endswith('.zip') for f in flist):
                for f in flist:
                    if not f.endswith('.zip'):
                        try:
                            os.remove(os.path.join(tmpdir, f))
                        except FileNotFoundError:
                            pass

        # --- step 3: compute hashes + canonical PyPI URLs ---
        for filename in os.listdir(tmpdir):
            name = _get_package_name_from_filename(filename)
            if name.casefold() in SYSTEM_PACKAGES:
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


def fetch_pypi_sources(name: str, version: str) -> tuple[str, list[dict]]:
    """
    Offline-friendly fallback: query the PyPI JSON API for a single package.

    Used only when ``collect_pip_download`` produces no results (e.g. when
    the host has no network access to run pip download, or pip is unavailable).
    Returns ``(resolved_version, [source])`` — a single source entry with no
    transitive dependencies.

    File preference: pure-python any.whl > sdist > manylinux wheel.
    Arch-specific wheels that have no sdist alternative are kept as-is.
    Returns ``("", [])`` on failure.
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
                if "none-any" in fn:
                    return 4        # pure-python wheel — best
                if "manylinux" in fn and "x86_64" in fn:
                    return 2        # arch wheel — will need sdist swap
                if "manylinux" in fn:
                    return 1
                return 0
            for ext in ('gz', 'bz2', 'xz', 'zip'):
                if fn.endswith(ext):
                    return 3        # sdist preferred over arch wheel
            return -1

        files_sorted = sorted(files, key=_score, reverse=True)
        if not files_sorted:
            return resolved_version, []

        best = files_sorted[0]
        fn = best.get("filename", "")

        # If best is an arch-specific wheel, attempt to swap for sdist
        if fn.endswith('.whl') and 'none-any' not in fn:
            try:
                sdist_url = _get_sdist_url_pypi(name, resolved_version)
                sdist_entry = next((f for f in files if f["url"] == sdist_url), None)
                if sdist_entry:
                    best = sdist_entry
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


def make_vcs_source(pkg: "ResolvedPackage") -> Optional[OrderedDict]:
    """
    Build a VCS source entry (``type: git`` / ``type: svn`` / …) from a
    ResolvedPackage that carries VCS metadata.  Returns ``None`` if the
    package has no VCS info.

    Mirrors the vcs_packages handling in flatpak-pip-generator (L321-344).
    """
    vcs = getattr(pkg, 'vcs',     None)
    uri = getattr(pkg, 'vcs_uri', None) or getattr(pkg, 'uri', None)
    rev = getattr(pkg, 'revision', None)

    if not (vcs and uri):
        return None

    # Strip scheme and re-add https:// (same as pip-generator)
    url = 'https://' + uri.split('://', 1)[1] if '://' in uri else uri
    rev_key = 'revision' if vcs == 'svn' else 'commit'

    entries = [('type', vcs), ('url', url)]
    if rev:
        entries.append((rev_key, rev))
    return OrderedDict(entries)


# ---------------------------------------------------------------------------
# Build-dependency auto-resolution
# ---------------------------------------------------------------------------

def resolve_import_to_pypi_name(import_name: str) -> Optional[str]:
    """
    Try to resolve a Python import name to a PyPI package name by querying
    the PyPI JSON API directly.

    Strategy: GET /pypi/{import_name}/json.  If the package exists the API
    returns 200 and ``data["info"]["name"]`` is the canonical PyPI name
    (e.g. "ninja" → "ninja", "mesonpy" → 404 → None).

    Returns the canonical PyPI name on success, ``None`` when the package is
    not found (404) or the request fails for any reason.  The caller is
    responsible for falling back to a user-supplied ``--build-dep`` mapping.
    """
    url = f"https://pypi.org/pypi/{import_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        canonical = data["info"]["name"]
        logger.debug(
            "Resolved import '%s' → PyPI package '%s'", import_name, canonical
        )
        return canonical
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.debug("PyPI lookup for '%s' returned 404 — not found", import_name)
        else:
            logger.warning(
                "PyPI lookup for '%s' failed with HTTP %d", import_name, exc.code
            )
        return None
    except Exception as exc:
        logger.warning("PyPI lookup for '%s' failed: %s", import_name, exc)
        return None


# ---------------------------------------------------------------------------
# Module builder
# ---------------------------------------------------------------------------

def build_pip_module(
    pkg: "ResolvedPackage",
    cleanup: Optional[str] = None,
    ignore_installed: bool = False,
    build_isolation: bool = False,
    checker_data: bool = False,
) -> dict:
    """
    Build a Flatpak module dict that matches the output of flatpak-pip-generator.

    Sources
    -------
    For regular packages ``collect_pip_download`` is called so that ALL
    transitive dependencies appear as individual source entries — exactly what
    flatpak-builder needs for a fully offline build.  If pip download is
    unavailable (offline host), ``fetch_pypi_sources`` is used as a fallback
    (single file only, no transitive deps).

    For VCS packages a single ``type: git/svn`` source is emitted; no pip
    download step is performed.

    Parameters
    ----------
    pkg:
        Resolved package to generate a module for.
    cleanup:
        ``"all"`` → ``cleanup: ["*"]``,
        ``"scripts"`` → ``cleanup: ["/bin", "/share/man/man1"]``,
        ``None`` → field omitted.
    ignore_installed:
        Append ``--ignore-installed`` to the pip install command.
    build_isolation:
        When ``False`` (default) append ``--no-build-isolation``.
    checker_data:
        Annotate each file source with ``x-checker-data`` for the Flatpak
        External Data Checker.
    """
    # ------------------------------------------------------------------
    # 1. Determine pip spec and VCS status
    # ------------------------------------------------------------------
    vcs_source = make_vcs_source(pkg)
    is_vcs = vcs_source is not None

    if is_vcs:
        vcs_uri    = getattr(pkg, 'vcs_uri', None) or getattr(pkg, 'uri', '')
        rev        = getattr(pkg, 'revision', '')
        rev_str    = f"@{rev}" if rev else ""
        pip_spec   = f"{vcs_uri}{rev_str}#egg={pkg.name}"
        name_for_pip = "."          # installed from the checked-out directory
    else:
        version_str  = f"=={pkg.version}" if pkg.version else ""
        pip_spec     = f"{pkg.name}{version_str}"
        name_for_pip = f'"{pip_spec}"'

    # ------------------------------------------------------------------
    # 2. Collect sources
    # ------------------------------------------------------------------
    if is_vcs:
        package_sources: list[dict] = [vcs_source]

    elif pkg.source_url and pkg.source_hash:
        # Fast path: caller already has URL+hash; still collect transitive deps.
        own_source = OrderedDict([
            ("type",   "file"),
            ("url",    pkg.source_url),
            ("sha256", pkg.source_hash),
        ])
        dep_sources = collect_pip_download(pip_spec)
        own_norm = pkg.name.lower().replace('-', '_')
        package_sources = [own_source]
        for dep_name, dep_info in dep_sources.items():
            if dep_name.lower().replace('-', '_') != own_norm:
                package_sources.append(dep_info["source"])

    else:
        # Full resolution: let pip download decide what is needed.
        dep_sources = collect_pip_download(pip_spec)

        if dep_sources:
            own_norm = pkg.name.lower().replace('-', '_')
            # Own package first, remaining deps alphabetically
            own_entry = (
                dep_sources.get(pkg.name)
                or dep_sources.get(pkg.name.replace('-', '_'))
                or dep_sources.get(pkg.name.lower())
            )
            package_sources = []
            if own_entry:
                package_sources.append(own_entry["source"])
            for dep_name, dep_info in sorted(dep_sources.items()):
                if dep_name.lower().replace('-', '_') != own_norm:
                    package_sources.append(dep_info["source"])
        else:
            logger.warning(
                "pip download produced no results for %s — falling back to "
                "single-file PyPI lookup (transitive deps will be missing)",
                pkg.name,
            )
            resolved_version, fallback_sources = fetch_pypi_sources(pkg.name, pkg.version)
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
    # 4. Build the pip install command (mirrors pip-generator L436-449)
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
    # 5. Assemble the module dict (mirrors pip-generator L451-465)
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