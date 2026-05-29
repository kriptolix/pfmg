"""
pfmg.learn.inspector
~~~~~~~~~~~~~~~~~~~~~
Inspector — downloads a Flatpak SDK or extension locally, inspects its
contents (pkg-config, shared libraries, executables), generates a static
profile JSON, and then optionally removes the downloaded SDK.

Workflow:
  1. `flatpak install <sdk-id>//<version>` (if not already installed)
  2. Enter a build environment via `flatpak build-init` + `flatpak build`
  3. Execute introspection commands:
       pkg-config --list-all
       find /usr/lib -name '*.so*' -type f
       ls /usr/bin /usr/lib/sdk/*/bin
  4. Parse output → InspectionResult
  5. Write to data/sdk-profiles/ or data/ext-profiles/
  6. Optionally leave the SDK installed (--nocleanup)

Usage:
    pfmg inspect org.freedesktop.Sdk --sdk-version 24.08
    pfmg inspect org.freedesktop.Sdk.Extension.node24 --sdk-version 25.08
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pfmg.sandbox.runner import SandboxRunner
from pfmg.utils.io import write_json
from pfmg.utils.logging import get_logger
from pfmg.utils.text import base_sdk_from_extension

logger = get_logger(__name__)

_DATA_DIR         = Path(__file__).parent.parent / "data"
_SDK_PROFILES_DIR = _DATA_DIR / "sdk-profiles"
_EXT_PROFILES_DIR = _DATA_DIR / "ext-profiles"

# ---------------------------------------------------------------------------
# Introspection scripts run inside the SDK
# ---------------------------------------------------------------------------

_INTROSPECT_SH = r"""
echo '=== PKGCONFIG ==='
pkg-config --list-all 2>/dev/null | awk '{print $1}' | sort -u
echo '=== LIBRARIES ==='
find /usr/lib /usr/lib64 /lib /lib64 -name '*.so*' -type f 2>/dev/null \
  | sed 's|.*/||' | sort -u
echo '=== EXECUTABLES ==='
ls /usr/bin /usr/local/bin 2>/dev/null | sort -u
echo '=== DONE ==='
"""

# Not a raw string: {mount} is substituted by .format() at call time.
# {{print $1}} becomes {print $1} after .format(), which is valid awk.
_EXT_INTROSPECT_SH_TEMPLATE = """
EXT_PATH={mount}
echo '=== EXT_MOUNT_CHECK ==='
ls "$EXT_PATH" 2>/dev/null || echo "MOUNT_MISSING: $EXT_PATH"
echo '=== EXT_EXECUTABLES ==='
ls "$EXT_PATH/bin" 2>/dev/null | sort -u
echo '=== EXT_PKGCONFIG ==='
PC_PATH="$EXT_PATH/lib/pkgconfig:$EXT_PATH/lib64/pkgconfig:$EXT_PATH/share/pkgconfig"
PKG_CONFIG_PATH="$PC_PATH" pkg-config --list-all 2>/dev/null | awk '{{print $1}}' | sort -u
echo '=== EXT_LIBRARIES ==='
find "$EXT_PATH/lib" "$EXT_PATH/lib64" -name '*.so*' -type f 2>/dev/null \
  | sed 's|.*/||' | sort -u
echo '=== DONE ==='
"""

# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------

@dataclass
class InspectionResult:
    sdk_id: str
    sdk_version: str          # version of the base SDK (e.g. "24.08")
    ext_version: Optional[str] = None  # own version of the extension when it
                                       # differs from sdk_version (e.g. "1.6");
                                       # None means the extension uses sdk_version
    pkgconfig: list[str] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    executables: list[str] = field(default_factory=list)
    success: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Prober
# ---------------------------------------------------------------------------

class RuntimeInspector:
    """
    Downloads a Flatpak SDK or extension, if needed, introspects it, and writes a static
    profile JSON to data/sdk-profiles/ or data/ext-profiles/.    
    """

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        no_cleanup: bool = False,
    ):
        self.sdk_output_dir = output_dir or _SDK_PROFILES_DIR
        self.ext_output_dir = output_dir or _EXT_PROFILES_DIR
        self.no_cleanup = no_cleanup
        self._flatpak = shutil.which("flatpak")
        # Tracks whether the SDK/extension was already installed before probing
        # so we only uninstall what we installed ourselves.
        self._installed_by_us: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._flatpak)

    def probe_sdk(self, sdk_id: str, sdk_version: str) -> InspectionResult:
        """Probe a single SDK and write its profile."""
        result = InspectionResult(sdk_id=sdk_id, sdk_version=sdk_version)

        if not self._flatpak:
            result.error = "flatpak not found"
            return result

        ref = f"{sdk_id}//{sdk_version}"
        if not self._is_installed(sdk_id, sdk_version):
            logger.info("Installing %s ...", ref)
            if not self._install(sdk_id, sdk_version):
                result.error = f"flatpak install failed for {ref}"
                return result
            self._installed_by_us.add(ref)

        output = self._run_in_sdk(sdk_id, sdk_version, _INTROSPECT_SH)
        if output is None:
            result.error = f"introspection failed for {ref}"
            return result

        result = self._parse_sdk_output(output, sdk_id, sdk_version)
        self._write_sdk_profile(result, sdk_id, sdk_version)

        if not self.no_cleanup and ref in self._installed_by_us:
            self._uninstall(sdk_id, sdk_version)

        return result

    def probe_ext(
        self,
        ext_id: str,
        sdk_version: str,
        ext_version: Optional[str] = None,
    ) -> InspectionResult:
        """
        Probe a Flatpak SDK extension and write/update its profile JSON.

        ``ext_version`` is the version tag used to *install* the extension.
        It defaults to ``sdk_version`` when omitted, which is correct for
        most modern extensions.  Older or independently-versioned extensions
        (e.g. ``org.freedesktop.Sdk.Extension.gcc8//1.6``) require an
        explicit ``ext_version``.

        The base SDK is resolved in order:
          1. Derived from the extension ID via ``base_sdk_from_extension``.
          2. Queried from ``flatpak info`` (field ``SDK:``) when derivation
             returns the extension ID unchanged (i.e. the utility gave up).
          3. Falls back to ``org.freedesktop.Sdk`` as a last resort.
        """
        effective_ext_version = ext_version or sdk_version
        result = InspectionResult(
            sdk_id=ext_id,
            sdk_version=sdk_version,        # base SDK version, used for profile naming
            ext_version=ext_version,        # own version (None when equal to sdk_version)
        )

        if not self._flatpak:
            result.error = "flatpak not found"
            return result

        ext_ref = f"{ext_id}//{effective_ext_version}"
        if not self._is_installed(ext_id, effective_ext_version):
            logger.info("Installing extension %s ...", ext_ref)
            if not self._install(ext_id, effective_ext_version):
                result.error = (
                    f"flatpak install failed for {ext_ref}. "
                    f"Try: flatpak install flathub {ext_ref}"
                )
                return result
            self._installed_by_us.add(ext_ref)

        sdk = self._resolve_base_sdk(ext_id, effective_ext_version)
        logger.info("Using base SDK: %s for extension %s", sdk, ext_id)

        if not self._is_installed(sdk, sdk_version):
            result.error = (
                f"Base SDK {sdk}//{sdk_version} is not installed. "
                f"Install it with: flatpak install flathub {sdk}//{sdk_version}"
            )
            return result

        short_name = ext_id.split(".")[-1]
        mount = f"/usr/lib/sdk/{short_name}"
        script = _EXT_INTROSPECT_SH_TEMPLATE.format(mount=mount)

        # When the extension has its own version (e.g. gcc8//1.6 with SDK//25.08),
        # flatpak build-init rejects --sdk-extension because it looks for the
        # extension at the *runtime* version and won't find it.  We work around
        # this by discovering the real install path on the host and passing it
        # as a --bind-mount to every `flatpak build` call instead.
        ext_mount_overrides: dict[str, str] = {}
        if effective_ext_version != sdk_version:
            host_path = self._get_ext_install_path(ext_id, effective_ext_version)
            if not host_path:
                result.error = (
                    f"Could not determine install path for {ext_ref}. "
                    f"Verify with: flatpak info --show-location {ext_ref}"
                )
                return result
            ext_mount_overrides[mount] = host_path
            logger.info(
                "Extension %s has independent version %s; "
                "will bind-mount host path %s → sandbox %s",
                ext_id, effective_ext_version, host_path, mount,
            )

        output = self._run_in_sdk(
            sdk,
            sdk_version,
            script,
            extra_extensions=[] if ext_mount_overrides else [ext_id],
            ext_mount_overrides=ext_mount_overrides,
        )
        if output is None:
            result.error = (
                f"Extension introspection failed for {ext_id}. "
                f"Verify the extension is installed: flatpak info {ext_id}"
            )
            return result

        result = self._parse_ext_output(output, ext_id, sdk_version)
        result.ext_version = ext_version
        self._write_ext_profile(result, ext_id, sdk_version, mount)

        if not self.no_cleanup and ext_ref in self._installed_by_us:
            self._uninstall(ext_id, effective_ext_version)

        return result

    def probe_list(
        self,
        sdk_list: list[tuple[str, str]],
        ext_list: list[tuple[str, str]] | list[tuple[str, str, str]],
    ) -> list[InspectionResult]:
        """Probe all SDKs and extensions in the given lists.

        Each entry in ``ext_list`` may be a 2-tuple ``(ext_id, sdk_version)``
        or a 3-tuple ``(ext_id, sdk_version, ext_version)`` for extensions
        whose version tag differs from the SDK's.
        """
        results: list[InspectionResult] = []
        for sdk_id, version in sdk_list:
            logger.info("Probing SDK: %s//%s", sdk_id, version)
            results.append(self.probe_sdk(sdk_id, version))
        for entry in ext_list:
            ext_id, sdk_version = entry[0], entry[1]
            ext_version = entry[2] if len(entry) == 3 else None  # type: ignore[misc]
            logger.info(
                "Probing extension: %s//%s (sdk %s)",
                ext_id, ext_version or sdk_version, sdk_version,
            )
            results.append(self.probe_ext(ext_id, sdk_version, ext_version))
        return results

    # ------------------------------------------------------------------
    # Base-SDK resolution
    # ------------------------------------------------------------------

    def _get_ext_install_path(self, ext_id: str, ext_version: str) -> Optional[str]:
        """
        Return the host filesystem path where the extension's files are mounted.

        ``flatpak info --show-location`` returns the deploy directory
        (e.g. /var/lib/flatpak/runtime/<id>/x86_64/<ver>/active).
        The actual SDK files live under ``<deploy>/files``, which is what
        flatpak bind-mounts inside the sandbox at ``/usr/lib/sdk/<name>``.
        """
        result = subprocess.run(
            [self._flatpak, "info", "--show-location",
             f"{ext_id}//{ext_version}"],
            capture_output=True, timeout=10, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "flatpak info --show-location failed for %s//%s: %s",
                ext_id, ext_version, result.stderr.strip(),
            )
            return None

        deploy_dir = result.stdout.strip()
        files_path = Path(deploy_dir) / "files"
        if files_path.exists():
            return str(files_path)

        # Some builds put files directly in the deploy dir — fall back.
        logger.debug(
            "Expected files/ subdir not found under %s, using deploy dir directly",
            deploy_dir,
        )
        return deploy_dir

    def _resolve_base_sdk(self, ext_id: str, ext_version: str) -> str:
        """Return the base SDK id for an extension.

        Resolution order:
          1. ``base_sdk_from_extension(ext_id)`` — works for the standard
             ``org.X.Sdk.Extension.Y`` naming convention.
          2. ``flatpak info --show-metadata`` — reads the ``[ExtensionOf]``
             or ``[Runtime]`` section to find the SDK field directly.
          3. Hard fallback to ``org.freedesktop.Sdk``.
        """
        candidate = base_sdk_from_extension(ext_id)
        if candidate and candidate != ext_id:
            return candidate

        # The utility gave up — ask flatpak directly.
        logger.debug(
            "base_sdk_from_extension could not derive SDK from %s, "
            "falling back to flatpak info",
            ext_id,
        )
        sdk = self._sdk_from_flatpak_info(ext_id, ext_version)
        if sdk:
            logger.info("Resolved base SDK via flatpak info: %s", sdk)
            return sdk

        fallback = "org.freedesktop.Sdk"
        logger.warning(
            "Cannot determine base SDK for %s — using fallback %s",
            ext_id, fallback,
        )
        return fallback

    def _sdk_from_flatpak_info(
        self, ref_id: str, version: str
    ) -> Optional[str]:
        """Parse 'flatpak info --show-metadata' to extract the SDK field."""
        result = subprocess.run(
            [self._flatpak, "info", "--show-metadata", f"{ref_id}//{version}"],
            capture_output=True, timeout=15, text=True,
        )
        if result.returncode != 0:
            return None

        # The metadata is an INI-style file.  The SDK line looks like:
        #   sdk=org.freedesktop.Sdk//24.08   or   sdk=org.freedesktop.Sdk
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("sdk="):
                value = stripped.split("=", 1)[1].strip()
                # Drop any branch suffix (e.g. "org.freedesktop.Sdk//24.08")
                sdk_id = value.split("//")[0]
                if sdk_id:
                    return sdk_id
        return None

    # ------------------------------------------------------------------
    # Flatpak helpers
    # ------------------------------------------------------------------

    def _is_installed(self, ref_id: str, version: str) -> bool:
        result = subprocess.run(
            [self._flatpak, "info", f"{ref_id}//{version}"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0

    def _install(self, ref_id: str, version: str) -> bool:
        result = subprocess.run(
            [self._flatpak, "install", "--noninteractive", "--assumeyes",
             "flathub", f"{ref_id}//{version}"],
            capture_output=True, timeout=600,
        )
        if result.returncode != 0:
            logger.warning("Install failed: %s", result.stderr.decode()[-500:])
        return result.returncode == 0

    def _uninstall(self, ref_id: str, version: str) -> None:
        subprocess.run(
            [self._flatpak, "uninstall", "--noninteractive", f"{ref_id}//{version}"],
            capture_output=True, timeout=60,
        )
        logger.info("Uninstalled %s//%s", ref_id, version)

    def _run_in_sdk(
        self,
        sdk_id: str,
        sdk_version: str,
        script: str,
        extra_extensions: Optional[list[str]] = None,
        ext_mount_overrides: Optional[dict[str, str]] = None,
    ) -> Optional[str]:
        """
        Run a shell script inside the SDK via SandboxRunner.
        Returns stdout on success, None on failure.
        """
        parts = sdk_id.split(".")
        platform = ".".join(
            "Platform" if (p == "Sdk" and i == len(parts) - 1) else p
            for i, p in enumerate(parts)
        )
        if platform == sdk_id:
            platform = "org.freedesktop.Platform"
            logger.warning(
                "Could not derive Platform from %s, falling back to %s",
                sdk_id, platform,
            )

        with tempfile.TemporaryDirectory(prefix="pfmg-probe-") as tmp:
            build_dir = Path(tmp) / "build"

            runner = SandboxRunner(
                build_dir=build_dir,
                sdk=sdk_id,
                runtime=platform,
                runtime_version=sdk_version,
                sdk_extensions=extra_extensions or [],
                ext_mount_overrides=ext_mount_overrides or {},
            )

            init_result = runner.init()
            if not init_result.succeeded:
                logger.warning(
                    "build-init failed (exit %d): %s",
                    init_result.exit_code, init_result.stderr[-400:],
                )
                return None

            run_result = runner.run(script)
            if run_result.succeeded:
                return run_result.stdout

            logger.warning(
                "flatpak build failed (exit %d): %s",
                run_result.exit_code, run_result.stderr[-600:],
            )
            return None

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sdk_output(output: str, sdk_id: str, sdk_version: str) -> InspectionResult:
        result = InspectionResult(sdk_id=sdk_id, sdk_version=sdk_version, success=True)
        section = None
        for line in output.splitlines():
            line = line.strip()
            if line == "=== PKGCONFIG ===":
                section = "pc"
            elif line == "=== LIBRARIES ===":
                section = "libs"
            elif line == "=== EXECUTABLES ===":
                section = "exes"
            elif line == "=== DONE ===":
                break
            elif line and section == "pc":
                result.pkgconfig.append(line)
            elif line and section == "libs":
                result.libraries.append(line)
            elif line and section == "exes":
                result.executables.append(line)
        return result

    @staticmethod
    def _parse_ext_output(output: str, ext_id: str, sdk_version: str) -> InspectionResult:
        result = InspectionResult(sdk_id=ext_id, sdk_version=sdk_version, success=True)
        section = None
        for line in output.splitlines():
            line = line.strip()
            if line == "=== EXT_MOUNT_CHECK ===":
                section = "mount_check"
            elif line == "=== EXT_EXECUTABLES ===":
                section = "exes"
            elif line == "=== EXT_PKGCONFIG ===":
                section = "pc"
            elif line == "=== EXT_LIBRARIES ===":
                section = "libs"
            elif line == "=== DONE ===":
                break
            elif line and section == "mount_check":
                if "MOUNT_MISSING" in line:
                    logger.warning(
                        "Extension mount not present in sandbox: %s\n"
                        "  The extension may not be installed for the correct arch/version.\n"
                        "  Try: flatpak install flathub %s//%s",
                        line, ext_id, sdk_version,
                    )
                else:
                    logger.debug("Extension mount OK — contents: %s", line[:120])
            elif line and section == "exes":
                result.executables.append(line)
            elif line and section == "pc":
                result.pkgconfig.append(line)
            elif line and section == "libs":
                result.libraries.append(line)
        return result

    # ------------------------------------------------------------------
    # Profile writers
    # ------------------------------------------------------------------

    def _write_ext_profile(
        self, result: InspectionResult, ext_id: str, sdk_version: str, mount: str
    ) -> Path:
        """Write (or overwrite) an extension profile JSON."""
        safe_name = f"{ext_id.split('.')[-1]}.{sdk_version}"
        profile_path = self.ext_output_dir / f"{safe_name}.json"
        data = {
            "extension_id":         ext_id,
            "display_name":         safe_name,
            "sdk_version":          sdk_version,
            "ext_version":          result.ext_version or sdk_version,
            "mount_path":           mount,
            "provides_executables": sorted(result.executables),
            "provides_pkgconfig":   sorted(result.pkgconfig),
            "provides_libraries":   sorted(result.libraries),
            "env": {
                "PATH": f"{mount}/bin:$PATH"
            },
        }
        write_json(profile_path, data, mkdir=True)
        logger.info("Wrote extension profile: %s", profile_path)
        return profile_path

    def _write_sdk_profile(
        self, result: InspectionResult, sdk_id: str, sdk_version: str
    ) -> Path:
        """Write (or overwrite) an SDK profile JSON."""
        
        # "org.freedesktop.Sdk" → file is "freedesktop.24.08.json"
        parts = sdk_id.split(".")
        short = parts[-2] if len(parts) >= 2 else parts[-1]
        safe_id = f"{short}.{sdk_version}"

        profile_path = self.sdk_output_dir / f"{safe_id}.json"
        data = {
            "sdk_id":      result.sdk_id,
            "sdk_version": result.sdk_version,
            "pkgconfig":   sorted(result.pkgconfig),
            "libraries":   sorted(result.libraries),
            "executables": sorted(result.executables),
        }
        write_json(profile_path, data, mkdir=True)
        logger.info("Wrote SDK profile: %s", profile_path)
        return profile_path