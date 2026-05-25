"""pfmg.utils.io — file I/O helpers shared across the package."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json_or_yaml(path: Path) -> Any:
    """
    Load a JSON or YAML file and return its parsed contents.

    Supports .json, .yaml, .yml.  Raises ValueError for unknown extensions.
    Callers should catch exceptions from json/yaml parsing as needed.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml  # optional dependency — only imported when needed
        return yaml.safe_load(text) or {}
    if path.suffix == ".json":
        return json.loads(text)
    raise ValueError(f"Unsupported file extension: {path.suffix!r}")


def write_json(path: Path, data: Any, *, mkdir: bool = True) -> None:
    """Serialise *data* to *path* as indented JSON (UTF-8, no ASCII escapes)."""
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def sh_quote(s: str) -> str:
    """Wrap *s* in single quotes, escaping any internal single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"
