from __future__ import annotations

import os
import re

_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def build_version() -> str:
    """Return a short, display-safe build identifier."""
    value = os.environ.get("BAMBU_CONTROL_VERSION", "dev").strip()
    return value if _SAFE_VERSION.fullmatch(value) else "dev"
