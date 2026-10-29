"""Fetch step: ask the catalogue and write every page to the log. No database here.

Each run lives in its own folder, data/runs/<parcel_id>/<fetch_id>/:
- fetch.jsonl  the log: a start line, one line per page, a line per failed attempt, an end line
- status.json  running, complete, failed or interrupted, with the reason
- fetch.lock   exists while a fetch writes the folder; "running" without it means the run was cut off

If a run stops halfway, `resume_dir` goes on from the last whole page, using
the next link the server sent with it. Nothing already fetched is asked again.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cdse_stac.client import CdseClient, FetchError, answer_problem, next_request
from cdse_stac.config import Settings
from cdse_stac.jsonsafe import printable
from cdse_stac.logs import run_logger
from cdse_stac.runlog import (
    TORN_SUFFIX,
    RunLog,
    attempt_entry,
    body_bytes,
    end_entry,
    page_entry,
    start_entry,
    summarize_log,
)

LOCK_NAME = "fetch.lock"


@dataclass(frozen=True)
class FetchResult:
    fetch_id: str
    run_dir: Path
    log_path: Path
    pages: int
    scenes: int
    rate_limited: int
    seconds: float


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_folder(folder: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_status(run_dir: Path, logger, **fields: object) -> None:
    """Replace status.json in one step, so it is never half written.

    status.json only reports; the log is the record. If it cannot be written (a full disk),
    the reason goes to the screen and the fetch goes on.
    """
    path = run_dir / "status.json"
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.unlink(missing_ok=True)  # a left-over file, or a planted link, is removed, never written through
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(temporary, flags, 0o600), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(fields, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_folder(run_dir)
    except OSError as error:
        logger.error("status.json could not be written: %s", error)


@contextmanager
def _run_lock(run_dir: Path, now: Callable[[], datetime]) -> Iterator[None]:
    """Only one fetch may write a run folder at a time."""
    path = run_dir / LOCK_NAME
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            holder = printable(path.read_text(encoding="utf-8").strip(), 200)
        except OSError:
            holder = "unknown"
        raise FetchError(
            f"another fetch is writing {run_dir} ({holder}); if no fetch is running, delete {path}"
        ) from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "since": _utc_text(now())}))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _new_run_dir(settings: Settings, base_id: str) -> tuple[str, Path]:
    """A fresh folder for a new fetch. Two fetches in the same second get -2, -3 and so on."""
    parcel_dir = Path(settings.runs_dir) / settings.query.parcel_id
    parcel_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for number in range(1, 100):
        fetch_id = base_id if number == 1 else f"{base_id}-{number}"
        run_dir = parcel_dir / fetch_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return fetch_id, run_dir
    raise FetchError(f"could not create a new run folder in {parcel_dir}")


def run_fetch(
    settings: Settings,
    *,
    token: str | None = None,
    client: CdseClient | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    resume_dir: str | Path | None = None,
) -> FetchResult:
    query = settings.query
    fingerprint = query.fingerprint()
    if resume_dir is None:
        fetch_id, run_dir = _new_run_dir(settings, f"{now():%Y%m%dT%H%M%SZ}_{fingerprint[:8]}")
    else:
        run_dir = Path(resume_dir)
        if not (run_dir / "fetch.jsonl").is_file():
            raise FetchError(f"{run_dir} holds no fetch log to resume")
        fetch_id = ""
    with _run_lock(run_dir, now):
        return _fetch(settings, run_dir, fetch_id, token, client, now, resume=resume_dir is not None)


def _fetch(settings, run_dir: Path, fetch_id: str, token, client, now, resume: bool) -> FetchResult:
    query = settings.query
    fingerprint = query.fingerprint()
    started = time.perf_counter()
    start: tuple[str, dict] | None = None
    first_number = 1
    pages = scenes = earlier_rate_limited = 0
    log_path = run_dir / "fetch.jsonl"
    run_source = settings.source

    if resume:
        # Checked before the log is opened for writing, so a refused resume leaves the file exactly as it was.
        summary = summarize_log(log_path, keep_last_page=True)
        if summary.start is None:
            raise FetchError(f"{log_path} has no readable start line, so it cannot be resumed")
        if summary.start["query_fingerprint"] != fingerprint:
            raise FetchError("this log belongs to a different question (the query fingerprint differs)")
        torn_tail = (summary.lines, "line is cut short (no line end)")  # a crash; set aside below, not damage
        damaged = [item for item in summary.damaged if item != torn_tail]
        if damaged or summary.problems:
            reasons = [f"line {number}: {problem}" for number, problem in damaged] + summary.problems
            raise FetchError(f"{log_path} is damaged ({'; '.join(reasons[:3])}); start a new fetch")
        # Go on with the page size the run started with, whatever the settings say now: the saved
        # next link asks for that many scenes per page.
        run_source = settings.source.model_copy(update={"page_size": summary.start["page_size"]})

    runlog = RunLog(log_path)
    if not resume:
        runlog.append(
            start_entry(
                fetch_id=fetch_id,
                started_at=_utc_text(now()),
                source_url=settings.source.search_url,
                page_size=settings.source.page_size,
                query=query.model_dump(mode="json"),
                query_fingerprint=fingerprint,
            )
        )
    else:
        fetch_id = summary.start["fetch_id"]
        pages, scenes, earlier_rate_limited = len(summary.page_numbers), summary.scenes, summary.rate_limited
        if summary.complete:
            return FetchResult(fetch_id, run_dir, log_path, pages, scenes, summary.rate_limited, 0.0)
        if summary.last_page is not None:
            last = summary.last_page
            answer, problem = answer_problem(body_bytes(last))
            if problem:
                raise FetchError(f"page {last['page']} in the log is not a search result ({problem}); start a new fetch")
            first_body = query.search_body(run_source.page_size)
            start = next_request(answer, run_source.allowed_hosts, first_body, last["request"]["url"])
            first_number = last["page"] + 1

    log = run_logger("cdse_stac.fetch", fetch_id)
    if runlog.removed_tail:
        log.warning("a line cut short by a crash (%s bytes) was moved to %s%s", runlog.removed_tail, log_path.name, TORN_SUFFIX)

    def keep_attempt(attempt: dict) -> None:
        runlog.append(attempt_entry(fetch_id=fetch_id, at=_utc_text(now()), **attempt))

    client = client or CdseClient(run_source, token=token, logger=run_logger("cdse_stac.client", fetch_id))
    client.source = run_source  # also for a client handed in: the run's own page size
    client.on_attempt = keep_attempt
    status = dict(fetch_id=fetch_id, parcel_id=query.parcel_id, query_fingerprint=fingerprint, log=str(log_path))
    _write_status(run_dir, log, **status, status="running", updated_at=_utc_text(now()))
    log.info("start parcel=%s resume=%s first_page=%s folder=%s", query.parcel_id, resume, first_number, run_dir.resolve())

    try:
        already_done = resume and start is None and pages > 0  # the last saved page had no next link
        if not already_done:
            for page in client.pages(query, start=start, first_number=first_number):
                runlog.append(
                    page_entry(
                        fetch_id=fetch_id,
                        query_fingerprint=fingerprint,
                        page=page.number,
                        fetched_at=_utc_text(now()),
                        url=page.url,
                        request_body=page.request_body,
                        status=page.status,
                        seconds=page.seconds,
                        raw_body=page.raw,
                        attempts=page.attempts,
                        headers=page.headers,
                        scenes=page.scenes,
                    )
                )
                pages += 1
                scenes += page.scenes
                log.info(
                    "page=%s status=%s scenes=%s seconds=%.2f attempts=%s",
                    page.number,
                    page.status,
                    page.scenes,
                    page.seconds,
                    page.attempts,
                )
        rate_limited = earlier_rate_limited + client.rate_limited
        runlog.append(end_entry(fetch_id=fetch_id, pages=pages, finished_at=_utc_text(now()), rate_limited=rate_limited))
    except BaseException as error:  # also Ctrl+C: say why the run stopped, then stop
        state = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        rate_limited = earlier_rate_limited + client.rate_limited
        _write_status(
            run_dir, log, **status, status=state, error=str(error), pages=pages, rate_limited=rate_limited,
            updated_at=_utc_text(now()),
        )
        log.error("fetch %s after %s pages: %s", state, pages, error)
        raise

    seconds = time.perf_counter() - started
    _write_status(
        run_dir,
        log,
        **status,
        status="complete",
        pages=pages,
        scenes=scenes,
        rate_limited=rate_limited,
        updated_at=_utc_text(now()),
    )
    log.info(
        "done pages=%s scenes=%s seconds=%.1f scenes_per_second=%.1f rate_limited=%s",
        pages,
        scenes,
        seconds,
        scenes / seconds if seconds > 0 else 0.0,
        rate_limited,
    )
    return FetchResult(fetch_id, run_dir, log_path, pages, scenes, rate_limited, seconds)
