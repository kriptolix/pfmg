from pfmg.sandbox.errors import (
    SandboxError, SandboxErrorType,
    _P_BACKEND_FAILED, _P_BACKEND_UNAVAILABLE, _P_BUILD_DEP_MISSING,
    _P_CMAKE_NOT_FOUND, _P_FLATPAK_MODULE_FAILED, _P_IMPORT_ERROR,
    _P_LINKER_NOT_FOUND, _P_LDD_NOT_FOUND, _P_MESON_DEP, _P_MISSING_HEADER,
    _P_PKGCONFIG_NOT_FOUND, _P_PIP_NOT_FOUND, _P_EXEC_NOT_FOUND)


def parse_errors(
    stderr: str,
    stdout: str = "",
    ldd_output: str = "",
    context: str = "",
) -> list[SandboxError]:
    """
    Parse all available output and return a deduplicated list of SandboxError.
    """
    errors: list[SandboxError] = []
    seen: set[tuple[str, str]] = set()   # (error_type, missing)

    def _add(error: SandboxError) -> None:
        key = (error.error_type.value, error.missing.lower())
        if key not in seen:
            errors.append(error)
            seen.add(key)

    # --- ldd output ---
    for m in _P_LDD_NOT_FOUND.finditer(ldd_output):
        _add(SandboxError(
            error_type=SandboxErrorType.MISSING_NATIVE_DEP,
            missing=m.group("lib"),
            source="ldd",
            context=context,
            raw_line=m.group(0).strip(),
        ))

    # --- stderr + stdout (combined for patterns that appear in either) ---
    combined = stderr + "\n" + stdout

    for m in _P_LINKER_NOT_FOUND.finditer(combined):
        lib = m.group("lib1") or m.group("lib2") or ""
        if lib:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_NATIVE_DEP,
                missing=lib,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_MISSING_HEADER.finditer(combined):
        _add(SandboxError(
            error_type=SandboxErrorType.MISSING_HEADER,
            missing=m.group("header").strip(),
            source="stderr",
            context=context,
            raw_line=m.group(0).strip(),
        ))

    for m in _P_PKGCONFIG_NOT_FOUND.finditer(combined):
        pkg = m.group("pkg1") or m.group("pkg2") or m.group("pkg3") or ""
        if pkg:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PKGCONFIG,
                missing=pkg.strip(),
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_MESON_DEP.finditer(combined):
        _add(SandboxError(
            error_type=SandboxErrorType.MISSING_PKGCONFIG,
            missing=m.group("dep").strip(),
            source="stderr",
            context=context,
            raw_line=m.group(0).strip(),
        ))

    for m in _P_CMAKE_NOT_FOUND.finditer(combined):
        dep = m.group("dep").strip()
        # Skip generic cmake internal deps (uppercase, short)
        if len(dep) > 2:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PKGCONFIG,
                missing=dep,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # Build-time missing Python dep — ModuleNotFoundError inside a pip
    # subprocess (metadata generation, setup.py, etc.).  Must run BEFORE
    # _P_IMPORT_ERROR so the same text is classified as MISSING_PYTHON_PKG
    # and the import-error handler skips it via the `seen` guard.
    for m in _P_BUILD_DEP_MISSING.finditer(combined):
        mod = m.group("mod").strip().split(".")[0]
        if mod:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=mod,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # Build backend unavailable — pip could not import the backend module.
    # Same classification as a missing build dep: the package needs to be
    # added as a python3-<backend> module before this one in the manifest.
    for m in _P_BACKEND_UNAVAILABLE.finditer(combined):
        mod = m.group("mod").strip().split(".")[0]
        if mod:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=mod,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # Build backend subprocess failure — backend exists but failed at import.
    for m in _P_BACKEND_FAILED.finditer(combined):
        mod = m.group("mod")
        if mod:
            mod = mod.strip().split(".")[0]
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=mod,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_IMPORT_ERROR.finditer(combined):
        mod = m.group("mod").strip().split(".")[0]  # top-level module only
        # Skip if already captured as a build-time missing dep — same
        # ModuleNotFoundError pattern, different root cause and classification.
        if mod and (SandboxErrorType.MISSING_PYTHON_PKG.value, mod.lower()) not in seen:
            _add(SandboxError(
                error_type=SandboxErrorType.IMPORT_ERROR,
                missing=mod,
                source="import",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    # flatpak-builder top-level module failure line — tells us which module
    # failed when the specific error was already captured above.
    for m in _P_FLATPAK_MODULE_FAILED.finditer(combined):
        module_name = m.group("module").strip()
        # Only add if no more specific error was recorded for this module.
        key = (SandboxErrorType.BUILD_FAILURE.value, module_name.lower())
        if key not in seen:
            _add(SandboxError(
                error_type=SandboxErrorType.BUILD_FAILURE,
                missing=module_name,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_PIP_NOT_FOUND.finditer(combined):
        pkg = m.group("dist") or m.group("pkg") or m.group("req") or ""
        if pkg:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_PYTHON_PKG,
                missing=pkg.strip(),
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    for m in _P_EXEC_NOT_FOUND.finditer(combined):
        cmd = m.group("cmd").strip()
        # Filter noise — single-char tokens and common shell words
        if len(cmd) > 2 and "/" not in cmd:
            _add(SandboxError(
                error_type=SandboxErrorType.MISSING_EXECUTABLE,
                missing=cmd,
                source="stderr",
                context=context,
                raw_line=m.group(0).strip(),
            ))

    return errors
