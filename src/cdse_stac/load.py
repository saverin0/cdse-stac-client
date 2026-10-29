"""Load step: read one fetch log and write iris_staging. No network here.

- The log alone is enough: its start line holds the full question.
- The log is checked first: every line's seal and chain, and its structure (one start,
  pages 1, 2, 3 without gaps, at most one end). A log with a broken structure is refused.
- The log is read twice, one line at a time: once to check it, once to load it, so a large
  log never has to fit in memory.
- One page is one database transaction. Loading a page again replaces that page's links and
  rejects, so loading a log twice changes nothing, not even a timestamp.
- The newest fetch wins: a scene's content is only replaced by a fetch that started later than
  the one that wrote it, whatever order the logs are loaded in.
- Refused records go to cdse_reject with their reason; duplicates are counted. An unreadable
  page counts as one record seen and refused.
  scenes_seen = inserted + updated + unchanged + duplicates + rejected, always.
- Only one load runs at a time; the run row says "loading" until the load has finished.
"""

from __future__ import annotations

import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from shapely.geometry import box
from sqlalchemy import DateTime, delete, func, literal, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DataError, IntegrityError

from cdse_stac.config import Query
from cdse_stac.db import (
    STAGING_SCHEMA,
    CdseFetchRun,
    CdseReject,
    CdseScene,
    CdseSceneFetch,
    JsonText,
    staging_schemas,
)
from cdse_stac.errors import LoadError
from cdse_stac.jsonsafe import loads, storable_copy
from cdse_stac.logs import run_logger
from cdse_stac.records import CONFLICTING_DUPLICATE, CleanScene, Rejection, clean_scene, split_duplicates
from cdse_stac.runlog import LogSummary, body_bytes, iter_pages, seal_at, summarize_log

UNREADABLE_PAGE = "unreadable_page"
DATABASE_REFUSED = "database_refused"
LOAD_LOCK_KEY = 72_350_001  # one number for "a load is running"; PostgreSQL advisory locks are keyed by numbers
ROWS_PER_STATEMENT = 100  # keeps every statement far below the one-minute statement limit

# Content columns a later fetch may change. id, scene_id and created_at never change.
_CONTENT = [
    field.name for field in fields(CleanScene) if field.name not in ("scene_id", "footprint_wkt", "raw_json", "dropped_fields")
] + ["footprint"]


@dataclass
class LoadStats:
    fetch_id: str
    pages: int = 0
    scenes_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    rejected: int = 0
    damaged_lines: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        """The usual ETL result keys, plus the extra counts this loader keeps.

        processed = inserted + updated + unchanged + duplicates_removed + failed, always.
        """
        return {
            "fetch_id": self.fetch_id,
            "processed": self.scenes_seen,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "duplicates_removed": self.duplicates,
            "failed": self.rejected,
            "pages": self.pages,
            "damaged_lines": self.damaged_lines,
            "execution_time": round(self.seconds, 2),
        }


def assert_staging_only() -> None:
    """Refuse to write if any table points anywhere but iris_staging."""
    schemas = staging_schemas()
    if schemas != {STAGING_SCHEMA}:
        raise LoadError(f"refusing to load: tables point at {sorted(map(str, schemas))}, only {STAGING_SCHEMA} is allowed")


@contextmanager
def _one_load_at_a_time(engine: Engine):
    """Hold a database-wide lock for the whole load, so two loads never count the same rows."""
    with engine.connect() as connection:
        locked = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOAD_LOCK_KEY}).scalar()
        connection.commit()  # the lock belongs to the session, so no transaction has to stay open
        if not locked:
            raise LoadError("another load is running on this database; wait for it to finish and load again")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOAD_LOCK_KEY})
            connection.commit()


def load_log(engine: Engine, log_path: str | Path, *, force: bool = False) -> LoadStats:
    assert_staging_only()
    started = time.perf_counter()
    log_path = Path(log_path)
    if not log_path.is_file():
        raise LoadError(f"{log_path} does not exist; give the run folder of a fetch")
    summary = summarize_log(log_path)
    begin = summary.start
    if begin is None:
        raise LoadError(f"{log_path} has no readable start line")
    if summary.problems:
        raise LoadError(f"{log_path} cannot be loaded: {'; '.join(summary.problems[:5])}; resume it or fetch again")
    try:
        query = Query.model_validate(begin["query"])
    except ValidationError as error:
        raise LoadError(f"the question in the log is not valid:\n{error}") from None
    if query.fingerprint() != begin["query_fingerprint"]:
        raise LoadError("the question in the log does not match its own fingerprint")
    fetch_id = begin["fetch_id"]
    fetched_at = datetime.fromisoformat(begin["started_at"])
    log = run_logger("cdse_stac.load", fetch_id)
    stats = LoadStats(fetch_id=fetch_id, damaged_lines=len(summary.damaged))
    for number, problem in summary.damaged:
        log.warning("damaged log line=%s skipped: %s", number, problem)

    with _one_load_at_a_time(engine):
        _same_log_or_longer(engine, log_path, summary, force)
        _upsert_run(engine, log_path, summary, stats, finished=False)  # first: scenes and rejects point at it
        seen: dict[str, str] = {}
        for page in iter_pages(log_path):
            _load_page(engine, page, query, fetch_id, fetched_at, seen, stats, log)
        stats.seconds = time.perf_counter() - started
        _upsert_run(engine, log_path, summary, stats, finished=True)
    log.info(
        "done pages=%s scenes_seen=%s inserted=%s updated=%s unchanged=%s duplicates=%s rejected=%s "
        "damaged_lines=%s seconds=%.2f",
        stats.pages,
        stats.scenes_seen,
        stats.inserted,
        stats.updated,
        stats.unchanged,
        stats.duplicates,
        stats.rejected,
        stats.damaged_lines,
        stats.seconds,
    )
    return stats


def _same_log_or_longer(engine: Engine, log_path: Path, summary: LogSummary, force: bool) -> None:
    """Refuse a log that changed since it was last loaded, unless it only grew (a resumed fetch)."""
    table = CdseFetchRun.__table__
    with engine.connect() as connection:
        row = connection.execute(
            select(table.c.log_sha256, table.c.log_lines, table.c.log_last_seal).where(
                table.c.fetch_id == summary.start["fetch_id"]
            )
        ).first()
    if row is None or row.log_sha256 == summary.sha256 or force:
        return
    if row.log_lines and row.log_last_seal and summary.sealed_lines >= row.log_lines:
        if seal_at(log_path, row.log_lines) == row.log_last_seal:
            return  # the old log is the start of the new one: a resumed fetch
    raise LoadError(
        f"{log_path} differs from the log loaded before under fetch {summary.start['fetch_id']}, and it is not "
        "the same log grown by a resume; load it with --force only if you know why it changed"
    )


def _load_page(
    engine: Engine,
    page: dict,
    query: Query,
    fetch_id: str,
    fetched_at: datetime,
    seen: dict[str, str],
    stats: LoadStats,
    log,
) -> None:
    stats.pages += 1
    number = page["page"]
    try:
        features = loads(body_bytes(page))["features"]
        if not isinstance(features, list):
            raise ValueError("features is not a list")
    except (ValueError, KeyError, TypeError) as error:
        with engine.begin() as connection:
            _forget_page(connection, fetch_id, number)
            _insert_rejects(connection, [_reject_row(fetch_id, number, -1, None, UNREADABLE_PAGE, str(error), None)])
        stats.scenes_seen += 1  # the page counts as one record, seen and refused
        stats.rejected += 1
        log.warning("page=%s could not be read: %s", number, error)
        return

    clean: list[CleanScene] = []
    position_of: dict[int, int] = {}
    rejects: list[dict] = []
    for position, item in enumerate(features):
        result = clean_scene(item, query)
        if isinstance(result, Rejection):
            rejects.append(
                _reject_row(fetch_id, number, position, result.scene_id, result.reason, result.detail, storable_copy(item))
            )
        else:
            clean.append(result)
            position_of[id(result)] = position
    kept, duplicates, conflicts = split_duplicates(clean, seen)
    for scene in conflicts:
        rejects.append(
            _reject_row(
                fetch_id,
                number,
                position_of[id(scene)],
                scene.scene_id,
                CONFLICTING_DUPLICATE,
                "this fetch returned the same scene id earlier with different content; the first copy was kept",
                scene.raw_item,
            )
        )

    try:
        with engine.begin() as connection:  # one page, one transaction
            _forget_page(connection, fetch_id, number)
            inserted, updated, older = _upsert_scenes(connection, kept, fetch_id, fetched_at)
            _link_scenes(connection, kept, position_of, fetch_id, number)
            _insert_rejects(connection, rejects)
    except (DataError, IntegrityError) as error:
        # The checks above should leave nothing the database refuses. If it still refuses the page,
        # load it scene by scene, so one bad row cannot stop the rest, and keep each refused row as a reject.
        log.warning("page=%s refused by the database (%s); loading it scene by scene", number, _first_line(error))
        inserted, updated, older, kept = _load_scene_by_scene(
            engine, kept, rejects, position_of, fetch_id, number, fetched_at
        )

    unchanged = len(kept) - inserted - updated
    stats.scenes_seen += len(features)
    stats.inserted += inserted
    stats.updated += updated
    stats.unchanged += unchanged
    stats.duplicates += len(duplicates)
    stats.rejected += len(rejects)
    dropped = Counter(name for scene in kept for name in scene.dropped_fields)
    log.info(
        "page=%s scenes=%s inserted=%s updated=%s unchanged=%s duplicates=%s rejected=%s",
        number,
        len(features),
        inserted,
        updated,
        unchanged,
        len(duplicates),
        len(rejects),
    )
    if older:
        log.info("page=%s %s scenes have newer content from a later fetch, which was kept", number, older)
    if dropped:
        log.info("page=%s optional values present but unreadable, left empty: %s", number, dict(dropped))


def _first_line(error: Exception) -> str:
    cause = getattr(error, "orig", None) or error
    return (str(cause).strip().splitlines() or [type(error).__name__])[0]


def _load_scene_by_scene(engine, kept, rejects, position_of, fetch_id, number, fetched_at):
    """The fallback for a page the database refused: each scene in its own transaction.

    Returns (inserted, updated, older, the scenes that were stored). A refused scene becomes a
    reject with reason database_refused, so the counts still add up.
    """
    with engine.begin() as connection:
        _forget_page(connection, fetch_id, number)
    inserted = updated = older = 0
    stored: list[CleanScene] = []
    for scene in kept:
        try:
            with engine.begin() as connection:
                one = _upsert_scenes(connection, [scene], fetch_id, fetched_at)
                _link_scenes(connection, [scene], position_of, fetch_id, number)
        except (DataError, IntegrityError) as error:
            rejects.append(
                _reject_row(
                    fetch_id, number, position_of[id(scene)], scene.scene_id, DATABASE_REFUSED,
                    _first_line(error), storable_copy(scene.raw_item),
                )
            )
            continue
        inserted, updated, older = inserted + one[0], updated + one[1], older + one[2]
        stored.append(scene)
    with engine.begin() as connection:
        _insert_rejects(connection, rejects)
    return inserted, updated, older, stored


def _forget_page(connection: Connection, fetch_id: str, number: int) -> None:
    """Remove what an earlier load of this page wrote, so loading it again gives the same rows."""
    connection.execute(
        delete(CdseReject.__table__).where(CdseReject.fetch_id == fetch_id, CdseReject.page == number)
    )
    connection.execute(
        delete(CdseSceneFetch.__table__).where(CdseSceneFetch.fetch_id == fetch_id, CdseSceneFetch.page == number)
    )


def _scene_row(scene: CleanScene, fetch_id: str, fetched_at: datetime) -> dict:
    row = {name: getattr(scene, name) for name in _CONTENT if name != "footprint"}
    row["scene_id"] = scene.scene_id
    row["raw_item"] = JsonText(scene.raw_json)
    row["footprint"] = f"SRID=4326;{scene.footprint_wkt}"
    row["first_fetch_id"] = row["updated_by_fetch_id"] = row["last_seen_fetch_id"] = fetch_id
    row["first_seen_at"] = row["source_fetched_at"] = row["last_seen_at"] = fetched_at
    return row


def _upsert_scenes(
    connection: Connection, scenes: list[CleanScene], fetch_id: str, fetched_at: datetime
) -> tuple[int, int, int]:
    """Insert new scenes, update scenes whose content changed in a newer fetch, mark the rest as seen.

    Returns (inserted, updated, older): older counts scenes whose stored content comes from a
    later fetch than this one, so this fetch's different content was not applied.
    Only new and changed scenes are sent in full; for the others only the "seen" columns move,
    so a reload does not resend or rewrite the large raw JSON.
    """
    if not scenes:
        return 0, 0, 0
    table = CdseScene.__table__
    me = (fetched_at, fetch_id)
    known = {
        row.scene_id: row
        for row in connection.execute(
            select(table.c.scene_id, table.c.record_sha256, table.c.source_fetched_at, table.c.updated_by_fetch_id).where(
                table.c.scene_id.in_([scene.scene_id for scene in scenes])
            )
        )
    }
    full, seen_only = [], []
    inserted = updated = older = 0
    for scene in scenes:
        stored = known.get(scene.scene_id)
        if stored is None:
            full.append(scene)
            inserted += 1
        elif stored.record_sha256 != scene.record_sha256 and me >= (stored.source_fetched_at, stored.updated_by_fetch_id):
            full.append(scene)
            updated += 1
        else:
            older += stored.record_sha256 != scene.record_sha256
            seen_only.append(scene.scene_id)

    if full:
        statement = pg_insert(table)
        newer = tuple_(statement.excluded.source_fetched_at, statement.excluded.updated_by_fetch_id) >= tuple_(
            table.c.source_fetched_at, table.c.updated_by_fetch_id
        )
        changes = {name: statement.excluded[name] for name in _CONTENT}
        changes |= {
            "updated_at": func.now(),
            "updated_by_fetch_id": statement.excluded.updated_by_fetch_id,
            "source_fetched_at": statement.excluded.source_fetched_at,
        }
        statement = statement.on_conflict_do_update(constraint="uq_cdse_scene_scene_id", set_=changes, where=newer)
        rows = [_scene_row(scene, fetch_id, fetched_at) for scene in full]
        for start in range(0, len(rows), ROWS_PER_STATEMENT):
            connection.execute(statement, rows[start : start + ROWS_PER_STATEMENT])

    ids = [scene.scene_id for scene in scenes]
    this_fetch = tuple_(literal(fetched_at, DateTime(timezone=True)), literal(fetch_id))
    for start in range(0, len(ids), ROWS_PER_STATEMENT):
        chunk = ids[start : start + ROWS_PER_STATEMENT]
        # "Seen" moves forward to the newest fetch and "first seen" back to the earliest, in any load order.
        connection.execute(
            update(table)
            .where(table.c.scene_id.in_(chunk), tuple_(table.c.last_seen_at, table.c.last_seen_fetch_id) < this_fetch)
            .values(last_seen_at=fetched_at, last_seen_fetch_id=fetch_id)
        )
        connection.execute(
            update(table)
            .where(table.c.scene_id.in_(chunk), tuple_(table.c.first_seen_at, table.c.first_fetch_id) > this_fetch)
            .values(first_seen_at=fetched_at, first_fetch_id=fetch_id)
        )
    return inserted, updated, older


def _link_scenes(
    connection: Connection, scenes: list[CleanScene], position_of: dict[int, int], fetch_id: str, number: int
) -> None:
    if scenes:
        statement = pg_insert(CdseSceneFetch.__table__).on_conflict_do_nothing(
            constraint="uq_cdse_scene_fetch_fetch_scene"
        )
        connection.execute(
            statement,
            [
                dict(fetch_id=fetch_id, scene_id=scene.scene_id, page=number, position=position_of[id(scene)])
                for scene in scenes
            ],
        )


def _reject_row(fetch_id, page, position, scene_id, reason, detail, raw) -> dict:
    return dict(
        fetch_id=fetch_id,
        page=page,
        position=position,
        scene_id=scene_id,
        reason=reason,
        detail=detail,
        raw_record=raw,
    )


def _insert_rejects(connection: Connection, rejects: list[dict]) -> None:
    """A reject is stored once per fetch, page and position, however often the log is loaded."""
    if rejects:
        statement = pg_insert(CdseReject.__table__).on_conflict_do_nothing(
            constraint="uq_cdse_reject_fetch_page_position"
        )
        connection.execute(statement, rejects)


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None  # the log check already made sure it has a time zone


def _upsert_run(engine: Engine, log_path: Path, summary: LogSummary, stats: LoadStats, finished: bool) -> None:
    """Write the run row. load_count counts finished loads: the first write sets 0, each finish adds 1."""
    begin, end = summary.start, summary.end
    side = begin["query"]["bbox"]
    counts = {key: value for key, value in asdict(stats).items() if key not in ("fetch_id", "seconds")}
    row = dict(
        fetch_id=begin["fetch_id"],
        parcel_id=begin["query"]["parcel_id"],
        query_fingerprint=begin["query_fingerprint"],
        query=begin["query"],
        parcel_bbox=f"SRID=4326;{box(side['west'], side['south'], side['east'], side['north']).wkt}",
        source_url=begin["source_url"],
        page_size=begin["page_size"],
        log_path=log_path.resolve().as_posix(),
        log_sha256=summary.sha256,
        log_lines=summary.sealed_lines,
        log_last_seal=summary.last_seal,
        fetch_status="complete" if summary.complete else "incomplete",
        load_status="loaded" if finished else "loading",
        started_at=_parse_time(begin.get("started_at")),
        finished_at=_parse_time(end.get("finished_at")) if end else None,
        rate_limited=end.get("rate_limited") if end else summary.rate_limited,
        loaded_at=func.now(),
        load_count=0,
        **counts,
    )
    table = CdseFetchRun.__table__
    statement = pg_insert(table).values(row)
    changes = {name: statement.excluded[name] for name in row if name not in ("fetch_id", "load_count")}
    changes["updated_at"] = func.now()
    changes["load_count"] = table.c.load_count + (1 if finished else 0)
    statement = statement.on_conflict_do_update(index_elements=["fetch_id"], set_=changes)
    with engine.begin() as connection:
        connection.execute(statement)

