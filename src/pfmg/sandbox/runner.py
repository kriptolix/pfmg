"""
pfmg.sandbox.runner
~~~~~~~~~~~~~~~~~~~~
Executes commands inside a Flatpak build sandbox using `flatpak build`.

Workflow:
  1. Initialize a build directory:
       flatpak build-init <build-dir> <app-id> <sdk> <runtime> <version>
  2. Run commands inside it:
       flatpak build [options] <build-dir> <command> [args...]
  3. Optional teardown (the build-dir is just a directory, rm -rf cleans it).

The build-dir is a plain directory on disk containing the mounted SDK/runtime.
It is created once per probe session and reused for all subsequent commands.

For SDK extension support, extensions are activated via --env and PATH
extensions are mounted automatically by flatpak build when installed on
the host. PATH is prepended per extension via --env=.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from pfmg.utils.io import sh_quote
from pfmg.utils.logging import get_logger

if TYPE_CHECKING:
    from pfmg.utils.models import FlatpakManifest

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = 120


@dataclass
class RunResult:
    """Result of a single command execution inside the sandbox."""
    command: str
    stdout: str
    stderr: str
    exit_code: int

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        return self.stdout + "\n" + self.stderr


class SandboxRunner:
    """
    Manages a `flatpak build` sandbox session.    
    """

    APP_ID = "org.pfmg.TestSandbox"

    def __init__(
        self,
        build_dir: Path,
        sdk: str = "org.freedesktop.Sdk",
        runtime: str = "org.freedesktop.Platform",
        runtime_version: str = "24.08",
        sdk_extensions: Optional[list[str]] = None,
        timeout: int = _DEFAULT_TIMEOUT,
        extra_env: Optional[dict[str, str]] = None,
        ext_mount_overrides: Optional[dict[str, str]] = None,
    ):
        
        self.build_dir = build_dir
        self.sdk = sdk
        self.runtime = runtime
        self.runtime_version = runtime_version
        self.sdk_extensions = sdk_extensions or []
        self.timeout = timeout
        self._build_timeout = 600
        self.extra_env = extra_env or {}
        self._flatpak = shutil.which("flatpak")
        self._initialised = False
        # Mount overrides for extensions whose version differs from the SDK's.
        # Keys are sandbox mount points (e.g. /usr/lib/sdk/gcc8),
        # values are host paths (from flatpak info --show-location).
        # These extensions are NOT declared via --sdk-extension in build-init
        # because flatpak would reject them (version mismatch); instead they
        # are bind-mounted directly in every `flatpak build` call.
        self.ext_mount_overrides: dict[str, str] = ext_mount_overrides or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if flatpak is present on the system."""
        return self._flatpak is not None

    def init(self, force: bool = False) -> RunResult:
        """
        Initialise the build directory via `flatpak build-init`.
        Idempotent — skips if already initialised unless force=True.

        SDK extensions are declared via ``--sdk-extension=<id>`` so their
        directories (/usr/lib/sdk/<name>) are mounted inside the sandbox.
        """
        if self._initialised and not force:
            return RunResult(command="(cached)", stdout="", stderr="", exit_code=0)

        if self.build_dir.exists() and force:
            shutil.rmtree(self.build_dir)
        self.build_dir.mkdir(parents=True, exist_ok=True)

        # Extensions handled via bind-mount (independent version) must NOT be
        # declared in build-init — flatpak would reject them due to version mismatch.
        # We match by the short name (last component of the extension ID).
        _override_short_names = {Path(k).name for k in self.ext_mount_overrides}
        ext_flags = [
            f"--sdk-extension={e}"
            for e in self.sdk_extensions
            if e.split(".")[-1] not in _override_short_names
        ]

        cmd = [
            self._flatpak, "build-init",
            *ext_flags,
            str(self.build_dir),
            self.APP_ID,
            self.sdk,
            self.runtime,
            self.runtime_version,
        ]

        logger.info("Initialising sandbox: %s", " ".join(str(c) for c in cmd))
        result = self._exec(cmd)

        if result.succeeded:
            self._initialised = True
            logger.info("Sandbox initialised at %s", self.build_dir)
        else:
            logger.warning(
                "build-init failed (exit %d):\n%s",
                result.exit_code, result.stderr[-1000:],
            )
        return result

    def run(self, shell_command: str, timeout: Optional[int] = None) -> RunResult:
        """
        Execute a shell command inside the sandbox via:
          flatpak build [options] <build-dir> /usr/bin/sh

        The command is passed via stdin to avoid host/sandbox path mismatches.
        init() must be called first.
        """
        if not self._flatpak:
            return RunResult(
                command=shell_command,
                stdout="",
                stderr="flatpak not found",
                exit_code=127,
            )

        env_args = self._extension_env_args()

        bind_args = [
            f"--bind-mount={mount_point}={host_path}"
            for mount_point, host_path in self.ext_mount_overrides.items()
        ]

        cmd = [
            self._flatpak, "build",
            "--with-appdir",
            "--allow=devel",
            "--share=network",
        ] + env_args + bind_args + [
            str(self.build_dir),
            "/usr/bin/sh",
        ]

        logger.debug("Sandbox run: %s", shell_command[:120])

        execution = self._exec(cmd, stdin_data=shell_command, timeout=timeout)
        return execution

    def run_python(self, python_command: str, timeout: Optional[int] = None) -> RunResult:
        """Convenience: run a Python one-liner inside the sandbox venv."""
        
        command = self.run(
            f"/app/venv/bin/python -c {sh_quote(python_command)}",
            timeout=timeout,
        )

        return command

    def run_pip(self, pip_args: str, timeout: Optional[int] = None) -> RunResult:
        """Convenience: run pip inside the sandbox venv."""

        command = self.run(f"/app/venv/bin/pip {pip_args}", timeout=timeout)
        return command

    def build_manifest(
        self,
        manifest: "FlatpakManifest",
        state_dir: Optional[Path] = None,
        repo_dir: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> RunResult:
        """
        Serialise *manifest* to a temporary JSON file and run
        ``flatpak run org.flatpak.Builder`` against it.

        This is the canonical way to validate that a generated module actually
        builds inside a real Flatpak environment — the builder resolves sources,
        runs build-commands, and reports any missing native dependency, header,
        or pkg-config entry via stderr in a format that parse_errors understands.

        Parameters
        ----------
        manifest:
            The FlatpakManifest to build.  Should contain only the module(s)
            being tested; no finish-args needed for a test build.
        state_dir:
            Directory for flatpak-builder's cache/state.  A temporary
            directory is used when not provided.
        repo_dir:
            Output OSTree repo directory.  A temporary directory is used when
            not provided.
        timeout:
            Override the runner's default timeout (in seconds).  Module builds
            that compile native extensions may need more time than the default.
        """
        if not self._flatpak:
            return RunResult(
                command="flatpak run org.flatpak.Builder",
                stdout="",
                stderr="flatpak not found",
                exit_code=127,
            )

        # Directories must be reachable inside the flatpak-builder sandbox.
        # bubblewrap does not mount /tmp from the host, but always exposes the
        # user's home directory, so we place everything under ~/.cache/pfmg.
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", manifest.app_id)
        base = Path.home() / ".cache" / "pfmg" / f"builder-{safe_name}"
        _state_dir    = state_dir or base / "state"
        _repo_dir     = repo_dir  or base / "repo"
        manifest_path = base / "manifest.json"

        base.mkdir(parents=True, exist_ok=True)
        _state_dir.mkdir(parents=True, exist_ok=True)
        _repo_dir.mkdir(parents=True, exist_ok=True)

        manifest_path.write_text(
            _manifest_to_json(manifest),
            encoding="utf-8",
        )
        
        logger.debug("Test manifest written to %s", manifest_path)

        cmd = [
            self._flatpak, "run", "org.flatpak.Builder",
            "--ccache",
            "--force-clean",
            "--disable-updates",
            f"--state-dir={_state_dir}",
            str(_repo_dir),
            str(manifest_path),
        ]
        
        logger.info("Building test manifest for %s", manifest.app_id)
        result = self._exec(cmd, timeout=timeout or self._build_timeout)
        shutil.rmtree(base, ignore_errors=True)
        
        return result

    def teardown(self) -> None:
        """Remove the build directory."""
        if self.build_dir.exists():
            shutil.rmtree(self.build_dir, ignore_errors=True)
            logger.debug("Sandbox teardown: %s removed", self.build_dir)
        self._initialised = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _extension_env_args(self) -> list[str]:
        """
        Build the ``--env=`` args for ``flatpak build`` to activate extensions.

        Prepend each extension's bin/ directory to PATH inside the sandbox.
        Extensions declared via --sdk-extension are mounted automatically by
        flatpak; extensions declared via ext_mount_overrides are bind-mounted
        manually, but their bin/ paths still need to be added to PATH here.
        """
        paths: list[str] = []

        for ext_id in self.sdk_extensions:
            short = ext_id.split(".")[-1]
            paths.append(f"/usr/lib/sdk/{short}/bin")

        # Also add bin/ for bind-mounted extensions (mount point is the key).
        for mount_point in self.ext_mount_overrides:
            bin_path = f"{mount_point}/bin"
            if bin_path not in paths:
                paths.append(bin_path)

        if not paths:
            return []

        # List /usr/bin and /bin explicitly — $PATH is NOT expanded by flatpak
        # --env= inside bubblewrap; the variable would be passed as a literal string.
        extra_path = ":".join(paths) + ":/usr/bin:/bin"
        return [f"--env=PATH={extra_path}"]

    def _exec(
        self,
        cmd: list[str],
        stdin_data: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> RunResult:
        
        env = dict(os.environ)
        env.update(self.extra_env)
        env.pop("DISPLAY", None)   # avoid X11 errors in headless environments

        try:
            proc = subprocess.run(
                cmd,
                input=stdin_data,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
            )

            return RunResult(
                command=" ".join(str(c) for c in cmd),
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )
        
        except subprocess.TimeoutExpired:
            logger.warning(
                "Sandbox command timed out after %ds: %s",
                timeout or self.timeout, cmd,
            )

            return RunResult(
                command=" ".join(str(c) for c in cmd),
                stdout="",
                stderr=f"TIMEOUT after {timeout or self.timeout}s",
                exit_code=-1,
            )
        
        except FileNotFoundError:
            return RunResult(
                command=" ".join(str(c) for c in cmd),
                stdout="",
                stderr=f"flatpak not found: {cmd[0]}",
                exit_code=127,
            )


# ---------------------------------------------------------------------------
# Manifest serialisation
# ---------------------------------------------------------------------------

def _manifest_to_json(manifest: "FlatpakManifest") -> str:
    """
    Serialise a FlatpakManifest dataclass to a JSON string suitable for
    flatpak-builder.

    FlatpakModule uses snake_case field names (build_commands, build_options)
    while the JSON format requires kebab-case (build-commands, build-options).
    FlatpakSource fields map directly to JSON keys of the same name.
    """
    def _source(src) -> dict:
        d: dict = {"type": src.type}
        if src.url:          d["url"]           = src.url
        if src.sha256:       d["sha256"]         = src.sha256
        if src.path:         d["path"]           = src.path
        if src.dest_filename:d["dest-filename"]  = src.dest_filename
        if src.branch:       d["branch"]         = src.branch
        if src.commit:       d["commit"]         = src.commit
        if src.tag:          d["tag"]            = src.tag
        return d

    def _module(mod) -> dict:
        d: dict = {
            "name":       mod.name,
            "buildsystem": mod.buildsystem,
        }
        if mod.build_commands:
            d["build-commands"] = mod.build_commands
        if mod.config_opts:
            d["config-opts"] = mod.config_opts
        if mod.build_options:
            d["build-options"] = mod.build_options
        if mod.cleanup:
            d["cleanup"] = mod.cleanup
        if mod.sources:
            d["sources"] = [_source(s) for s in mod.sources]
        if mod.modules:
            d["modules"] = [_module(m) for m in mod.modules]
        return d

    doc: dict = {
        "app-id":          manifest.app_id,
        "runtime":         manifest.runtime,
        "runtime-version": manifest.runtime_version,
        "sdk":             manifest.sdk,
    }
    if manifest.sdk_extensions:
        doc["sdk-extensions"] = manifest.sdk_extensions
    if manifest.finish_args:
        doc["finish-args"] = manifest.finish_args
    if manifest.modules:
        doc["modules"] = [_module(m) for m in manifest.modules]

    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"