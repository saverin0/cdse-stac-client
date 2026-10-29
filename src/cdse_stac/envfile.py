"""Secrets from the environment, or from a file named by NAME_FILE (where Docker secrets put them)."""

from __future__ import annotations

import os
from pathlib import Path


def secret(name: str) -> str | None:
    """The value of NAME, or the content of the file named by NAME_FILE. Empty or absent means not set.

    Docker compose creates no secret file for an empty value, so a missing file counts as not set too.
    """
    path = os.environ.get(f"{name}_FILE")
    if path:
        file = Path(path)
        value = file.read_text(encoding="utf-8").rstrip("\r\n") if file.is_file() else ""  # spaces may belong to it
    else:
        value = os.environ.get(name, "")
    return value or None
