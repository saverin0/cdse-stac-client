"""Source-aware logging: every line says which source and which run it belongs to.

Example line:
2026-10-28T15:02:11Z INFO source=cdse-stac fetch=20261028T150210Z_3f2a9c1b cdse_stac.fetch page=2 scenes=50
"""

from __future__ import annotations

import logging
import time

SOURCE = "cdse-stac"


class _Defaults(logging.Filter):
    """Fill in source and run for lines that come without them, such as library lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "source"):
            record.source = SOURCE
        if not hasattr(record, "fetch_id"):
            record.fetch_id = "-"
        return True


def resolve_level(text: str | None) -> tuple[int, str | None]:
    """LOG_LEVEL as a logging level, and a note when it had to be changed.

    Names in any case and numbers are accepted. Empty means INFO. A level above ERROR
    would hide the error messages the program exits with, so it becomes ERROR.
    """
    value = (text or "").strip().upper() or "INFO"
    level = int(value) if value.isdigit() else logging.getLevelName(value)
    if not isinstance(level, int):
        return logging.INFO, f"LOG_LEVEL {text!r} is not a level name; using INFO"
    if level > logging.ERROR:
        return logging.ERROR, "LOG_LEVEL above ERROR would hide every error; using ERROR"
    return level, None


def setup_logging(level: int | str = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_Defaults())
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s source=%(source)s fetch=%(fetch_id)s %(name)s %(message)s",
        "%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime  # log times in UTC, like every other time in this project
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("alembic").setLevel(logging.WARNING)  # its start-up lines say nothing about the data


class RunLogger(logging.LoggerAdapter):
    """A logger that stamps every line with the source and the run id."""

    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}
        return msg, kwargs


def run_logger(name: str, fetch_id: str = "-") -> RunLogger:
    return RunLogger(logging.getLogger(name), {"source": SOURCE, "fetch_id": fetch_id})
