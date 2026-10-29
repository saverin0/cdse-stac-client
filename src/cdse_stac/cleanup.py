"""Remove old run folders: keep the newest runs of each parcel, remove only finished ones.

Each run folder holds about 5 MB per 100 scenes. A folder is removed only when it is older
than the newest `keep` runs of its parcel, its status says complete, and no fetch holds its lock.
Unfinished runs stay, so they can still be resumed. Without `yes`, nothing is removed; the
folders that would go are only listed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from cdse_stac.fetch import LOCK_NAME


def _finished(run_dir: Path) -> bool:
    try:
        status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(status, dict) and status.get("status") == "complete" and not (run_dir / LOCK_NAME).exists()


def old_runs(runs_dir: Path, keep: int) -> list[Path]:
    """Finished run folders beyond the newest `keep` of each parcel. Folder names start with the time, newest last."""
    found: list[Path] = []
    if not runs_dir.is_dir():
        return found
    for parcel_dir in sorted(path for path in runs_dir.iterdir() if path.is_dir()):
        runs = sorted((path for path in parcel_dir.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
        found += [run for run in runs[keep:] if _finished(run)]
    return found


def clean_up(runs_dir: Path, keep: int, yes: bool) -> list[Path]:
    """The folders removed, or with yes=False the folders that would be removed."""
    runs = old_runs(runs_dir, keep)
    if yes:
        for run in runs:
            shutil.rmtree(run)
    return runs
