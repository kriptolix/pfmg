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

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pfmg.utils.io import sh_quote
from pfmg.utils.logging import get_logger

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

    Usage::

        runner = SandboxRunner(
            build_dir=Path("/tmp/pfmg-probe"),
            sdk="org.freedesktop.Sdk",
            runtime="org.freedesktop.Platform",
            runtime_version="24.08",
        )
        if not runner.is_available():
            # flatpak not installed
            ...
        runner.init()               # flatpak build-init
        result = runner.run("python3 --version")
        result = runner.run("pkg-config --list-all")
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
    ):
        self.build_dir = build_dir
        self.sdk = sdk
        self.runtime = runtime
        self.runtime_version = runtime_version
        self.sdk_extensions = sdk_extensions or []
        self.timeout = timeout
        self.extra_env = extra_env or {}
        self._flatpak = shutil.which("flatpak")
        self._initialised = False

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

        ext_flags = [f"--sdk-extension={e}" for e in self.sdk_extensions]

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

        cmd = [
            self._flatpak, "build",
            "--with-appdir",
            "--allow=devel",
            "--share=network",
        ] + env_args + [
            str(self.build_dir),
            "/usr/bin/sh",
        ]
        logger.debug("Sandbox run: %s", shell_command[:120])
        return self._exec(cmd, stdin_data=shell_command, timeout=timeout)

    def run_python(self, python_command: str, timeout: Optional[int] = None) -> RunResult:
        """Convenience: run a Python one-liner inside the sandbox venv."""
        return self.run(
            f"/app/venv/bin/python -c {sh_quote(python_command)}",
            timeout=timeout,
        )

    def run_pip(self, pip_args: str, timeout: Optional[int] = None) -> RunResult:
        """Convenience: run pip inside the sandbox venv."""
        return self.run(f"/app/venv/bin/pip {pip_args}", timeout=timeout)

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
        Extensions are mounted automatically by flatpak when installed on the host.
        """
        if not self.sdk_extensions:
            return []

        paths: list[str] = []
        for ext_id in self.sdk_extensions:
            short = ext_id.split(".")[-1]
            paths.append(f"/usr/lib/sdk/{short}/bin")

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