"""pfmg.utils.text — text/name normalisation helpers."""
from __future__ import annotations

import re


def normalise_id(name: str) -> str:
    """Convert an arbitrary module/package name to a safe recipe id.

    Lower-cases, replaces spaces and underscores with hyphens.

    Examples::

        normalise_id("libFoo_bar")  → "libfoo-bar"
        normalise_id("My Package")  → "my-package"
    """
    return name.lower().replace(" ", "-").replace("_", "-")


def normalise_pkg(name: str) -> str:
    """Normalise a pkgconfig / recipe id for comparison.

    Lower-cases and replaces hyphens and dots with underscores so that
    ``"libfoo-1.0"``, ``"libfoo_1_0"``, and ``"libfoo.1.0"`` all compare equal.

    Examples::

        normalise_pkg("openssl")     → "openssl"
        normalise_pkg("libfoo-1.0")  → "libfoo_1_0"
    """
    return name.lower().replace("-", "_").replace(".", "_")


def normalise_lib(name: str) -> str:
    """Strip version suffixes from a shared-library filename.

    Examples::

        normalise_lib("libz.so.1.3")   → "libz"
        normalise_lib("libLLVM-17.so") → "libllvm"
        normalise_lib("libssl.so")     → "libssl"
    """
    name = re.sub(r"\.so.*$", "", name)   # drop .so and everything after
    name = re.sub(r"-\d+$", "", name)     # drop trailing version number
    return name.lower()
