"""
pfmg.learn.inspector (ok)
~~~~~~~~~~~~~~~~~~~~~
Inspector — downloads a Flatpak SDK or extension locally, inspects its
contents (pkg-config, shared libraries, executables), generates a static
profile JSON, and then removes the downloaded SDK to free disk space.

Runs on any machine with flatpak installed.

Workflow:
  1. `flatpak install <sdk-id>//<version>` (if not already installed)
  2. Enter a shell via `flatpak run --command=sh --devel <sdk-id>`
     via `flatpak build-init` + `flatpak build`
  3. Execute introspection commands:
       pkg-config --list-all
       find /usr/lib -name '*.so*' -type f
       ls /usr/bin /usr/lib/sdk/*/bin
  4. Parse output → SDKCapability
  5. Write to data/sdk-profiles or data/ext-profiles
  6. Optionally not uninstall the SDK or extension

Usage:

    pfmg learn sdk probe --sdk org.freedesktop.Sdk --sdk-version 24.08
    pfmg learn sdk probe --sdk org.gnome.Sdk --sdk-version 48 --cleanup
    pfmg learn sdk list-available
"""
from __future__ import annotations

import re
import shutil
import subprocess
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from reference.bkp.sandbox import SandboxRunner
from pfmg.utils.logging import get_logger

logger = get_logger(__name__)

_SDK_PROFILES_DIR = Path(__file__).parent.parent / "data" / "sdk-profiles"
_EXT_PROFILES_DIR = Path(__file__).parent.parent / "data" / "ext-profiles"

# ---------------------------------------------------------------------------
# Introspection script run inside the SDK
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

_EXT_INTROSPECT_SH_TEMPLATE = r"""
EXT_PATH={mount}
echo '=== EXT_EXECUTABLES ==='
ls "$EXT_PATH/bin" 2>/dev/null | sort -u
echo '=== EXT_PKGCONFIG ==='
pkg-config --with-path="$EXT_PATH/lib/pkgconfig" --list-all 2>/dev/null \
  | awk '{{print $1}}' | sort -u
echo '=== EXT_LIBRARIES ==='
find "$EXT_PATH/lib" "$EXT_PATH/lib64" -name '*.so*' -type f 2>/dev/null \
  | sed 's|.*/||' | sort -u
echo '=== DONE ==='
"""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _is_extension(ref_id: str) -> bool:
    """Return True if ref_id is a Flatpak SDK Extension (not a base SDK)."""
    return ".Extension." in ref_id


def _base_sdk_from_extension(ext_id: str) -> str:
    """
    Derive the base SDK id from an extension id.

    Examples:
      org.freedesktop.Sdk.Extension.node24  → org.freedesktop.Sdk
      org.gnome.Sdk.Extension.rust-stable   → org.gnome.Sdk
    """
    if ".Extension." in ext_id:
        return ext_id.rsplit(".Extension.", 1)[0]
    return ext_id


# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    sdk_id: str
    sdk_version: str
    pkgconfig: list[str] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    executables: list[str] = field(default_factory=list)
    success: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# SDKProber
# ---------------------------------------------------------------------------

class Prober:
    """
    Downloads a Flatpak SDK, introspects it, and writes a static profile.

    Standalone — no pfmg.pipeline dependency.
    """

    def __init__(
        self,
        output_dir: Optional[Path] = None,       # default: built-in sdk-profiles dir        
        no_cleanup: bool = False,              # uninstall after probing
    ):
        self.sdk_output_dir = output_dir or _SDK_PROFILES_DIR
        self.ext_output_dir = output_dir or _EXT_PROFILES_DIR
        self.previous_installed = None
        self.no_cleanup = no_cleanup
        self._flatpak = shutil.which("flatpak")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._flatpak)

    def probe_sdk(self, sdk_id: str, sdk_version: str) -> ProbeResult:
        """
        Probe a single SDK and write its profile.        
        """        

        result = ProbeResult(sdk_id=sdk_id, sdk_version=sdk_version)

        if not self._flatpak:
            result.error = "flatpak not found"
            return result

        if not self._is_installed(sdk_id, sdk_version):
            logger.info("Installing %s//%s ...", sdk_id, sdk_version)
            ok = self._install(sdk_id, sdk_version)
            
            if not ok:
                result.error = f"flatpak install failed for {sdk_id}//{sdk_version}"
                return result
            self.previous_installed = False

        output = self._run_in_sdk(sdk_id, sdk_version, _INTROSPECT_SH)
        if output is None:
            result.error = f"introspection failed for {sdk_id}//{sdk_version}"
            return result

        result = self._parse_sdk_output(output, sdk_id, sdk_version)
        self._write_sdk_profile(result, sdk_id, sdk_version)

        if not self.no_cleanup and self.previous_installed is False:
            self._uninstall(sdk_id, sdk_version)

        return result

    def probe_ext(self, ext_id: str, sdk_version: str,) -> ProbeResult:
        """
        Probe a Flatpak SDK extension and write/update its profile TOML.

        The extension must be installed on the host (or auto_install=True).
        The base SDK is derived from the extension id if not given:
          org.freedesktop.Sdk.Extension.node24 → org.freedesktop.Sdk
        """

        result = ProbeResult(sdk_id=ext_id, sdk_version=sdk_version)

        if not self._flatpak:
            result.error = "flatpak not found"
            return result

        # Derive base SDK from extension id
        sdk = _base_sdk_from_extension(ext_id)
        logger.info("Using base SDK: %s for extension %s", sdk, ext_id)

        # Install extension if needed
        if not self._is_installed(ext_id, sdk_version):
            logger.info("Installing extension %s//%s ...", ext_id, sdk_version)
            ok = self._install(ext_id, sdk_version)
            if not ok:
                result.error = (
                    f"flatpak install failed for {ext_id}//{sdk_version}. "
                    f"Try: flatpak install flathub {ext_id}//{sdk_version}"
                )
                return result

        # Verify base SDK is available for build-init
        if not self._is_installed(sdk, sdk_version):
            result.error = (
                f"Base SDK {sdk}//{sdk_version} is not installed. "
                f"Install it with: flatpak install flathub {sdk}//{sdk_version}"
            )
            return result

        # Derive mount path: last segment of extension id
        # e.g. org.freedesktop.Sdk.Extension.node24 → node24 → /usr/lib/sdk/node24
        short_name = ext_id.split(".")[-1]
        mount = f"/usr/lib/sdk/{short_name}"
        script = _EXT_INTROSPECT_SH_TEMPLATE.format(mount=mount)

        output = self._run_in_sdk(sdk, sdk_version, script, extra_extensions=[ext_id])
        if output is None:
            result.error = (
                f"Extension introspection failed for {ext_id}. "
                f"Verify the extension is installed: "
                f"flatpak info {ext_id}//{sdk_version}"
            )
            return result

        result = self._parse_ext_output(output, ext_id, sdk_version, mount)

        # Write a new profile TOML (or update existing one)
        self._write_ext_profile(result, ext_id, sdk_version, mount)
        
        if not self.no_cleanup and self.previous_installed is False:
            self._uninstall(ext_id, sdk_version)

        return result

    def probe_list(
        self,
        sdk_list: list[tuple[str, str]],
        ext_list: list[tuple[str, str]],
    ) -> list[ProbeResult]:
        """Probe all SDKs and extensions in the default lists."""
        results: list[ProbeResult] = []
        for sdk_id, version in (sdk_list):
            logger.info("Probing SDK: %s//%s", sdk_id, version)
            results.append(self.probe_sdk(sdk_id, version))

        for ext_id, version in (ext_list ):
            logger.info("Probing extension: %s//%s", ext_id, version)
            results.append(self.probe_ext(ext_id, version))

        return results

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
    ) -> Optional[str]:
        """
        Run a shell script inside the SDK via SandboxRunner (flatpak build-init
        + flatpak build).  The script is passed via stdin so host/sandbox path
        mismatches are avoided entirely — see SandboxRunner.run() for details.

        Returns stdout on success, None on failure.
        """
        import tempfile

        # Derive the Platform ref from the SDK id by replacing the trailing
        # "Sdk" component:
        #   org.freedesktop.Sdk  → org.freedesktop.Platform
        #   org.gnome.Sdk        → org.gnome.Platform
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
    def _parse_sdk_output(output: str, sdk_id: str, sdk_version: str) -> ProbeResult:
        result = ProbeResult(sdk_id=sdk_id, sdk_version=sdk_version, success=True)
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
    def _parse_ext_output(
        output: str, ext_id: str, sdk_version: str, mount: str
    ) -> ProbeResult:
        result = ProbeResult(sdk_id=ext_id, sdk_version=sdk_version, success=True)
        section = None
        for line in output.splitlines():
            line = line.strip()
            if line == "=== EXT_EXECUTABLES ===":
                section = "exes"
            elif line == "=== EXT_PKGCONFIG ===":
                section = "pc"
            elif line == "=== EXT_LIBRARIES ===":
                section = "libs"
            elif line == "=== DONE ===":
                break
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
    self, result, ext_id: str, sdk_version: str, mount: str
    ) -> Path:
        """
        Write a new extension profile JSON file.
        Does not overwrite existing profiles.
        """

        safe_name = f"{ext_id.split('.')[-1]}.{sdk_version}"

        existing = list(self.ext_output_dir.glob(f"*{safe_name}*.json"))
        if existing:
            logger.debug(
                "Extension profile already exists at %s — skipping create",
                existing[0]
            )
            return existing[0]

        self.ext_output_dir.mkdir(parents=True, exist_ok=True)

        profile_path = self.ext_output_dir / f"{safe_name}.json"

        data = {
            "extension_id": ext_id,
            "display_name": safe_name,            
            "mount_path": mount,           
            "provides_executables": sorted(result.executables),
            "provides_pkgconfig": sorted(result.pkgconfig),
            "provides_libraries": sorted(result.libraries),
            "env": {
                "PATH": f"{mount}/bin:$PATH"
            }
        }

        profile_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )

        logger.info("Wrote extension profile: %s", profile_path)
        return profile_path

    def _write_sdk_profile(
        self, result, sdk_id: str, sdk_version: str
    ) -> Path:

        safe_id = f"{sdk_id.split('.')[-2]}.{sdk_version}"

        self.sdk_output_dir.mkdir(parents=True, exist_ok=True)

        profile_path = self.sdk_output_dir / f"{safe_id}.json"

        data = {
            "sdk_id": result.sdk_id,
            "sdk_version": result.sdk_version,
            "pkgconfig": sorted(result.pkgconfig),
            "libraries": sorted(result.libraries),
            "executables": sorted(result.executables),
        }

        profile_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )

        logger.info("Wrote SDK profile: %s", profile_path)
        return profile_path