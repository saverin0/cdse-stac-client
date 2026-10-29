"""The fetch log: every server page is written here first, before any table.

One JSON object per line (the "JSON Lines" format). The file is only ever
appended to, and each line is pushed to the disk before the next request goes
out. Every line carries its own sha256 and the sha256 of the line before it,
so a line cut short by a crash, changed, removed or moved is found and
reported instead of being loaded. The chain catches accidents and careless
edits; someone who rewrites every line after a change can still forge it,
which is why the load step also stores the last line's sha256 and the number
of lines, and refuses a log that no longer extends what it loaded before.

Log first, data second: if the program or the machine dies halfway, the pages
already fetched are safe on disk, and the load step can rebuild the tables
from this file alone.

Line kinds: start (the question), page (one answer, exactly as received),
attempt (a failed try, with the reason), end (the fetch finished).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cdse_stac.jsonsafe import loads

LOG_FORMAT_VERSION = 2  # version 1 had no chain and sealed the body twice; it is still read
DURABLE = True  # push every line to the disk; the tests switch this off to run faster
TORN_SUFFIX = ".torn"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sealed(entry: dict, prev: str | None) -> tuple[str, str]:
    """The sealed line and its fingerprint."""
    if "line_sha256" in entry or "prev" in entry:
        raise ValueError("entry is already sealed")
    stamped = {**entry, "prev": prev}
    fingerprint = _sha(_canonical({key: value for key, value in stamped.items() if key != "body"}))
    return _canonical({**stamped, "line_sha256": fingerprint}), fingerprint


def seal(entry: dict, prev: str | None = None) -> str:
    """The entry as one line of text, chained to the line before it and with its own fingerprint.

    A page's body is covered by its body_sha256, so the body is not hashed a second time here.
    """
    return _sealed(entry, prev)[0]


def body_bytes(entry: dict) -> bytes:
    """A page's answer as the exact bytes the server sent."""
    if entry.get("body_encoding") == "base64":
        return base64.b64decode(entry["body"], validate=True)
    return entry["body"].encode("utf-8")


def unseal(line: str) -> tuple[dict, str, str | None]:
    """Read one line and check it. Returns (entry, its seal, the seal it claims for the line before).

    Raises ValueError if the line is damaged.
    """
    entry = loads(line)  # a broken line raises a ValueError, also when it is nested too deeply
    if not isinstance(entry, dict):
        raise ValueError("line is not a JSON object")
    claimed = entry.pop("line_sha256", None)
    if not isinstance(claimed, str):
        raise ValueError("line has no fingerprint")
    if entry.get("v") == 1:  # the first format sealed the whole line, body included, and had no chain
        actual, prev = _sha(_canonical(entry)), None
    else:
        actual = _sha(_canonical({key: value for key, value in entry.items() if key != "body"}))
        prev = entry.pop("prev", None)
    if actual != claimed:
        raise ValueError("fingerprint does not match the line")
    if entry.get("kind") == "page":
        try:
            raw = body_bytes(entry)
        except (KeyError, TypeError, ValueError, AttributeError):  # a body that is not text at all
            raise ValueError("the saved answer cannot be read") from None
        if hashlib.sha256(raw).hexdigest() != entry.get("body_sha256"):
            raise ValueError("the saved answer does not match its fingerprint")
    return entry, claimed, prev


def page_entry(
    *,
    fetch_id: str,
    query_fingerprint: str,
    page: int,
    fetched_at: str,
    url: str,
    request_body: dict,
    status: int,
    seconds: float,
    raw_body: bytes,
    attempts: int = 1,
    headers: dict | None = None,
    scenes: int | None = None,
) -> dict:
    """One server answer, exactly as received, with the request that produced it.

    An answer that is not UTF-8 text is kept as base64, so not a single byte is lost.
    """
    try:
        body, encoding = raw_body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        body, encoding = base64.b64encode(raw_body).decode("ascii"), "base64"
    return {
        "v": LOG_FORMAT_VERSION,
        "kind": "page",
        "fetch_id": fetch_id,
        "query_fingerprint": query_fingerprint,
        "page": page,
        "fetched_at": fetched_at,
        "request": {"url": url, "body": request_body},
        "status": status,
        "seconds": round(seconds, 3),
        "attempts": attempts,
        "headers": headers or {},
        "scenes": scenes,
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "body_encoding": encoding,
        "body": body,
    }


def attempt_entry(
    *,
    fetch_id: str,
    at: str,
    page: int,
    attempt: int,
    status: int | None,
    problem: str,
    seconds: float,
    retry_in: float | None,
    server_text: str = "",
) -> dict:
    """A failed try at a page: what went wrong and what the server said. Nothing here is loaded."""
    return {
        "v": LOG_FORMAT_VERSION,
        "kind": "attempt",
        "fetch_id": fetch_id,
        "at": at,
        "page": page,
        "attempt": attempt,
        "status": status,
        "problem": problem,
        "seconds": seconds,
        "retry_in": retry_in,
        "server_text": server_text,
    }


def start_entry(
    *,
    fetch_id: str,
    started_at: str,
    source_url: str,
    page_size: int,
    query: dict,
    query_fingerprint: str,
) -> dict:
    """The first line of a fetch: the full question, so the log can be replayed on its own."""
    return {
        "v": LOG_FORMAT_VERSION,
        "kind": "start",
        "fetch_id": fetch_id,
        "started_at": started_at,
        "source_url": source_url,
        "page_size": page_size,
        "query": query,
        "query_fingerprint": query_fingerprint,
    }


def end_entry(*, fetch_id: str, pages: int, finished_at: str, rate_limited: int = 0) -> dict:
    """The last line of a finished fetch. Without it, the log is incomplete."""
    return {
        "v": LOG_FORMAT_VERSION,
        "kind": "end",
        "fetch_id": fetch_id,
        "pages": pages,
        "finished_at": finished_at,
        "rate_limited": rate_limited,
    }


def _fsync_folder(folder: Path) -> None:
    """Make a new file's folder entry durable too. Only possible, and only needed, on POSIX systems."""
    if os.name != "posix":
        return
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def last_seal(path: str | Path) -> str | None:
    """The seal of the last whole line, so the next line chains to it. Reads only."""
    path = Path(path)
    if not path.exists():
        return None
    last = None
    with open(path, "rb") as handle:
        for raw in handle:
            if raw.endswith(b"\n"):
                last = raw
    return _claimed_seal(last.decode("utf-8", errors="replace")) if last is not None else None


class RunLog:
    """Append-only writer for one fetch log."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.is_symlink():
            raise OSError(f"{self.path} is a symbolic link; refusing to write the log through it")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.removed_tail = self._set_aside_torn_line()
        self._prev = last_seal(self.path)

    def append(self, entry: dict) -> None:
        line, fingerprint = _sealed(entry, self._prev)
        is_new = not self.path.exists()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags, 0o600)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(line.encode("utf-8") + b"\n")
            handle.flush()
            if DURABLE:
                os.fsync(handle.fileno())  # make the operating system put it on the disk now
        if is_new and DURABLE:
            _fsync_folder(self.path.parent)
        self._prev = fingerprint  # known already, so the multi-megabyte line is not parsed again

    def _set_aside_torn_line(self) -> int:
        """If a crash left half a line at the end, move it to fetch.jsonl.torn and cut it off.

        A line without its end can never be read, so keeping it would only mark the log as
        damaged for ever. It is kept next to the log, so nothing disappears without a trace.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0
        with open(self.path, "rb+") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) == b"\n":
                return 0
            size = handle.seek(0, os.SEEK_END)
            position = size
            while position > 0:  # find where the last whole line ends
                step = min(64 * 1024, position)
                handle.seek(position - step)
                block = handle.read(step)
                cut = block.rfind(b"\n")
                if cut >= 0:
                    position = position - step + cut + 1
                    break
                position -= step
            handle.seek(position)
            tail = handle.read()
            torn_path = Path(str(self.path) + TORN_SUFFIX)
            if torn_path.is_symlink():
                raise OSError(f"{torn_path} is a symbolic link; refusing to write through it")
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(torn_path, flags, 0o600), "ab") as torn:
                torn.write(tail + b"\n")
                torn.flush()
                os.fsync(torn.fileno())
            handle.truncate(position)
            handle.flush()
            os.fsync(handle.fileno())
        return len(tail)


@dataclass(frozen=True)
class LogLine:
    number: int
    entry: dict | None
    problem: str | None = None
    seal: str | None = None


def _iso_with_zone(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _whole(value: object, low: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= low


def entry_problem(entry: dict) -> str | None:
    """Which field of a log entry is missing or of the wrong type, or None."""
    kind = entry.get("kind")
    if (
        not isinstance(entry.get("fetch_id"), str)
        or not 0 < len(entry["fetch_id"]) <= 64
        or not entry["fetch_id"].isprintable()
    ):
        return "fetch_id is missing or not plain text of at most 64 characters"
    if kind == "start":
        checks = {
            "started_at": _iso_with_zone(entry.get("started_at")),
            "source_url": isinstance(entry.get("source_url"), str),
            "page_size": _whole(entry.get("page_size"), 1),
            "query": isinstance(entry.get("query"), dict),
            "query_fingerprint": isinstance(entry.get("query_fingerprint"), str)
            and bool(_SHA256.match(entry["query_fingerprint"])),
        }
    elif kind == "page":
        request = entry.get("request")
        checks = {
            "page": _whole(entry.get("page"), 1),
            "fetched_at": _iso_with_zone(entry.get("fetched_at")),
            "request": isinstance(request, dict) and isinstance(request.get("url"), str)
            and isinstance(request.get("body"), dict),
            "status": _whole(entry.get("status")),
            "body": isinstance(entry.get("body"), str),
        }
    elif kind == "attempt":
        checks = {"page": _whole(entry.get("page"), 1), "attempt": _whole(entry.get("attempt"), 1)}
    elif kind == "end":
        checks = {
            "pages": _whole(entry.get("pages")),
            "finished_at": _iso_with_zone(entry.get("finished_at")),
            "rate_limited": entry.get("rate_limited") is None or _whole(entry.get("rate_limited")),
        }
    else:
        return f"unknown line kind {kind!r}"
    wrong = [name for name, ok in checks.items() if not ok]
    return f"{kind} line: {', '.join(wrong)} missing or of the wrong type" if wrong else None


def iter_log(path: str | Path) -> Iterator[LogLine]:
    """Every line of a log, checked: seal, chain and fields. One line in memory at a time."""
    expected_prev: str | None = None
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.endswith(b"\n"):
                yield LogLine(number, None, "line is cut short (no line end)")
                continue
            try:
                text = raw.rstrip(b"\r\n").decode("utf-8")
            except UnicodeDecodeError:
                yield LogLine(number, None, "line is not UTF-8 text")
                continue
            if not text.strip():
                continue
            try:
                entry, own_seal, prev = unseal(text)
            except ValueError as error:
                yield LogLine(number, None, str(error))
                expected_prev = _claimed_seal(text)
                continue
            if entry.get("v") != 1 and prev != expected_prev:
                yield LogLine(number, None, "the chain is broken: a line before this one was changed, removed or moved")
                expected_prev = own_seal
                continue
            expected_prev = own_seal
            problem = entry_problem(entry)
            yield LogLine(number, None if problem else entry, problem, own_seal)


def _claimed_seal(text: str) -> str | None:
    """The seal a damaged line claims for itself, so the line after it can still be checked."""
    try:
        value = json.loads(text).get("line_sha256")
    except (ValueError, AttributeError, RecursionError):
        return None
    return value if isinstance(value, str) else None


@dataclass
class LogSummary:
    """What a log holds, without keeping its pages in memory."""

    start: dict | None = None
    end: dict | None = None
    page_numbers: list[int] = field(default_factory=list)
    scenes: int = 0
    rate_limited: int = 0
    damaged: list[tuple[int, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    lines: int = 0
    sealed_lines: int = 0  # the number of the last whole, readable line; a torn last line is not counted
    last_seal: str | None = None
    last_page: dict | None = None
    sha256: str = ""

    @property
    def complete(self) -> bool:
        return self.end is not None and not self.problems


def summarize_log(path: str | Path, keep_last_page: bool = False) -> LogSummary:
    """Read a log once: its start and end, page numbers, counts, and every structural problem.

    The structure must be: one start line first, pages numbered 1, 2, 3 without gaps or
    repeats, attempt lines anywhere between, and at most one end line, which comes last and
    names the right number of pages. Every line must belong to the same fetch.
    """
    summary = LogSummary()
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    summary.sha256 = digest.hexdigest()

    for line in iter_log(path):
        summary.lines = line.number
        if line.seal is not None:
            summary.sealed_lines = line.number
        entry = line.entry
        if entry is None:
            summary.damaged.append((line.number, line.problem or "damaged"))
            continue
        kind = entry["kind"]
        if summary.start is None and kind != "start":
            summary.problems.append(f"line {line.number} comes before the start line")
            continue
        if kind == "start":
            if summary.start is not None:
                summary.problems.append(f"line {line.number} is a second start line")
            else:
                summary.start = entry
            continue
        if entry["fetch_id"] != summary.start["fetch_id"]:
            summary.problems.append(f"line {line.number} belongs to another fetch ({entry['fetch_id']})")
            continue
        if summary.end is not None:
            summary.problems.append(f"line {line.number} comes after the end line")
            continue
        if kind == "page":
            expected = len(summary.page_numbers) + 1
            if entry["page"] != expected:
                summary.problems.append(f"line {line.number} holds page {entry['page']}, expected page {expected}")
            summary.page_numbers.append(entry["page"])
            scenes = entry.get("scenes")
            summary.scenes += scenes if _whole(scenes) else _count_scenes(entry)
            if keep_last_page:
                summary.last_page = entry
        elif kind == "attempt":
            summary.rate_limited += entry.get("status") == 429
        elif kind == "end":
            summary.end = entry
            if entry["pages"] != len(summary.page_numbers):
                summary.problems.append(
                    f"the end line names {entry['pages']} pages, the log holds {len(summary.page_numbers)}"
                )
    if summary.start is None and summary.lines:
        summary.problems.append("the log has no readable start line")
    summary.last_seal = last_seal(path)
    return summary


def _count_scenes(entry: dict) -> int:
    try:
        features = loads(body_bytes(entry)).get("features")
    except (ValueError, AttributeError):
        return 0
    return len(features) if isinstance(features, list) else 0


def seal_at(path: str | Path, line_number: int) -> str | None:
    """The seal of a given line, if that line is whole and undamaged."""
    for line in iter_log(path):
        if line.number == line_number:
            return line.seal
        if line.number > line_number:
            break
    return None


def iter_pages(path: str | Path) -> Iterator[dict]:
    """The readable page lines of a log, one at a time."""
    for line in iter_log(path):
        if line.entry is not None and line.entry["kind"] == "page":
            yield line.entry


@dataclass
class LogContents:
    """Everything read back from one small log file, kept in memory. Used by tests and tools."""

    entries: list[dict] = field(default_factory=list)
    damaged: list[tuple[int, str]] = field(default_factory=list)  # (line number, problem)

    @property
    def start(self) -> dict | None:
        return next((entry for entry in self.entries if entry.get("kind") == "start"), None)

    @property
    def pages(self) -> list[dict]:
        return [entry for entry in self.entries if entry.get("kind") == "page"]

    @property
    def complete(self) -> bool:
        return bool(self.entries) and self.entries[-1].get("kind") == "end"


def read_log(path: str | Path) -> LogContents:
    """Read a log, keep the whole lines, and list the damaged ones with the reason."""
    contents = LogContents()
    for line in iter_log(path):
        if line.entry is None:
            contents.damaged.append((line.number, line.problem or "damaged"))
        else:
            contents.entries.append(line.entry)
    return contents
