import copy
import json
import re
import threading
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, Table, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from cdse_stac import load as load_module
from cdse_stac import records
from cdse_stac.config import Query
from cdse_stac.db import Base, shared_database_problem
from cdse_stac.load import LOAD_LOCK_KEY, LoadError, assert_staging_only, load_log
from cdse_stac.runlog import RunLog, end_entry, page_entry, start_entry
from cdse_stac.validate import _read_only, coverage, run_checks

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
QUERY = Query(
    parcel_id="nw_bornheim_core",
    collection="sentinel-2-l2a",
    bbox=dict(west=6.966, south=50.761, east=6.994, north=50.779),
    start="2026-04-01T00:00:00Z",
    end="2026-09-30T23:59:59Z",
    max_cloud_cover_pct=30,
)


def answer(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["response"]


def raw(data):
    return json.dumps(data).encode("utf-8")


def write_log(path, bodies, fetch_id="20261028T150000Z_test0001", complete=True, query=QUERY,
              started_at="2026-10-28T15:00:00Z"):
    """A fetch log made from saved answers, as the fetch step would write it."""
    log = RunLog(path)
    log.append(
        start_entry(
            fetch_id=fetch_id,
            started_at=started_at,
            source_url="https://catalogue.dataspace.copernicus.eu/stac/search",
            page_size=3,
            query=query.model_dump(mode="json"),
            query_fingerprint=query.fingerprint(),
        )
    )
    for number, body in enumerate(bodies, start=1):
        log.append(
            page_entry(
                fetch_id=fetch_id,
                query_fingerprint=query.fingerprint(),
                page=number,
                fetched_at="2026-10-28T15:00:01Z",
                url="https://catalogue.dataspace.copernicus.eu/stac/search",
                request_body={"limit": 3},
                status=200,
                seconds=0.1,
                raw_body=body,
            )
        )
    if complete:
        log.append(end_entry(fetch_id=fetch_id, pages=len(bodies), finished_at="2026-10-28T15:00:02Z"))
    return path


def counts(engine):
    with engine.connect() as connection:
        return {
            table: connection.execute(text(f"SELECT count(*) FROM iris_staging.{table}")).scalar_one()
            for table in ("cdse_scene", "cdse_reject", "cdse_fetch_run", "cdse_scene_fetch")
        }


def snapshot(engine):
    with engine.connect() as connection:
        scenes = connection.execute(
            text("SELECT scene_id, record_sha256, created_at, updated_at FROM iris_staging.cdse_scene ORDER BY 1")
        ).all()
        rejects = connection.execute(text("SELECT fetch_id, page, position, reason FROM iris_staging.cdse_reject")).all()
    return scenes, sorted(rejects)


def failed_checks(engine):
    return [check for check in run_checks(engine) if not check.passed]


def scenes_with(**changes):
    """The three clean fixture scenes with some properties changed."""
    data = answer("search_cloud_max30")
    for item in data["features"]:
        item["properties"].update(changes)
    return data


# Checks that need no database.


def test_all_tables_live_in_iris_staging():
    assert {table.schema for table in Base.metadata.tables.values()} == {"iris_staging"}


def test_migrations_name_no_schema_but_iris_staging():
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert 'SCHEMA = "iris_staging"' in source, path
        assert re.findall(r"schema=(\w+)", source) == ["SCHEMA"] * len(re.findall(r"schema=", source)), path
        for schema, _ in re.findall(r"\b(\w+)\.(cdse_\w+)", source):
            assert schema in ("SCHEMA", "iris_staging") or schema.startswith("{"), (path, schema)
        assert "iris_core" not in source, path
        assert not re.search(r"execute\(\s*f?[\"']CREATE EXTENSION", source), path  # installing it writes into public


def test_load_refuses_tables_outside_staging():
    stray = Table("stray", Base.metadata, Column("id", Integer, primary_key=True), schema="iris_core")
    try:
        with pytest.raises(LoadError, match="refusing"):
            assert_staging_only()
    finally:
        Base.metadata.remove(stray)


def test_load_of_a_missing_run_folder_is_a_clear_error(tmp_path):
    with pytest.raises(LoadError, match="does not exist"):
        load_log(None, tmp_path / "nope" / "fetch.jsonl")


def test_load_refuses_a_log_with_a_missing_page(tmp_path):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))] * 3)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:2] + lines[3:]), encoding="utf-8", newline="")  # page 2 deleted
    with pytest.raises(LoadError, match="cannot be loaded"):
        load_log(None, path)


def test_load_reports_an_invalid_question_as_a_log_problem(tmp_path):
    path = tmp_path / "fetch.jsonl"
    log = RunLog(path)
    query = QUERY.model_dump(mode="json") | {"max_cloud_cover_pct": 300}
    log.append(start_entry(fetch_id="f", started_at="2026-10-28T15:00:00Z", source_url="x", page_size=3, query=query,
                           query_fingerprint="0" * 64))
    with pytest.raises(LoadError, match="question in the log is not valid"):
        load_log(None, path)


# Checks against the test database.


@pytest.mark.db
def test_load_writes_scenes_rejects_links_and_the_run(db, tmp_path):
    # Page 1 holds three clean scenes. Page 2 repeats two of them and adds one at 72 percent cloud.
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30")), raw(answer("search_page1"))])
    stats = load_log(db, path)
    assert (stats.scenes_seen, stats.inserted, stats.duplicates, stats.rejected) == (6, 3, 2, 1)
    assert stats.scenes_seen == stats.inserted + stats.updated + stats.unchanged + stats.duplicates + stats.rejected
    assert counts(db) == {"cdse_scene": 3, "cdse_reject": 1, "cdse_fetch_run": 1, "cdse_scene_fetch": 3}
    with db.connect() as connection:
        reason = connection.execute(text("SELECT reason FROM iris_staging.cdse_reject")).scalar_one()
        run = connection.execute(text("SELECT load_status, log_path, log_lines FROM iris_staging.cdse_fetch_run")).one()
    assert reason == "above_cloud_limit"
    assert run.load_status == "loaded" and Path(run.log_path).is_absolute() and run.log_lines == 4


@pytest.mark.db
def test_second_load_of_the_same_log_changes_nothing(db, tmp_path):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30")), raw(answer("search_page1"))])
    load_log(db, path)
    before = snapshot(db), counts(db)
    again = load_log(db, path)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 3)
    assert (snapshot(db), counts(db)) == before
    with db.connect() as connection:
        assert connection.execute(text("SELECT load_count FROM iris_staging.cdse_fetch_run")).scalar_one() == 2


@pytest.mark.db
def test_changed_scene_is_updated_and_stamped(db, tmp_path):
    load_log(db, write_log(tmp_path / "a.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="fetch-a"))
    changed = answer("search_cloud_max30")
    changed["features"][0]["properties"]["eo:cloud_cover"] = 1.0
    stats = load_log(db, write_log(tmp_path / "b.jsonl", [raw(changed)], fetch_id="fetch-b"))
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 1, 2)
    with db.connect() as connection:
        row = connection.execute(
            text(
                "SELECT cloud_cover_pct, first_fetch_id, updated_by_fetch_id, last_seen_fetch_id, "
                "updated_at > created_at FROM iris_staging.cdse_scene WHERE scene_id = :scene_id"
            ),
            {"scene_id": changed["features"][0]["id"]},
        ).one()
    assert tuple(row) == (1.0, "fetch-a", "fetch-b", "fetch-b", True)


@pytest.mark.db
def test_new_fetch_with_the_same_content_only_moves_last_seen(db, tmp_path):
    load_log(db, write_log(tmp_path / "a.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="fetch-a"))
    before = snapshot(db)
    stats = load_log(db, write_log(tmp_path / "b.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="fetch-b"))
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 0, 3)
    assert snapshot(db) == before  # same content, same updated_at
    with db.connect() as connection:
        rows = connection.execute(
            text("SELECT DISTINCT updated_by_fetch_id, last_seen_fetch_id FROM iris_staging.cdse_scene")
        ).all()
    assert [tuple(row) for row in rows] == [("fetch-a", "fetch-b")]


@pytest.mark.db
def test_reloading_an_older_run_does_not_roll_back_newer_content(db, tmp_path):
    older = write_log(tmp_path / "a.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="run-a",
                      started_at="2026-10-01T00:00:00Z")
    newer_data = answer("search_cloud_max30")
    newer_data["features"][0]["properties"]["eo:cloud_cover"] = 1.0
    newer = write_log(tmp_path / "b.jsonl", [raw(newer_data)], fetch_id="run-b", started_at="2026-10-20T00:00:00Z")
    load_log(db, older)
    load_log(db, newer)
    again = load_log(db, older)  # last loaded used to win
    assert again.updated == 0
    with db.connect() as connection:
        row = connection.execute(
            text("SELECT cloud_cover_pct, updated_by_fetch_id, last_seen_fetch_id, first_fetch_id "
                 "FROM iris_staging.cdse_scene WHERE scene_id = :scene_id"),
            {"scene_id": newer_data["features"][0]["id"]},
        ).one()
    assert tuple(row) == (1.0, "run-b", "run-b", "run-a")


@pytest.mark.db
def test_first_fetch_is_the_earliest_whatever_the_load_order(db, tmp_path):
    later = write_log(tmp_path / "b.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="run-b",
                      started_at="2026-10-20T00:00:00Z")
    earlier = write_log(tmp_path / "a.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="run-a",
                        started_at="2026-10-01T00:00:00Z")
    load_log(db, later)
    load_log(db, earlier)
    with db.connect() as connection:
        rows = connection.execute(
            text("SELECT DISTINCT first_fetch_id, last_seen_fetch_id FROM iris_staging.cdse_scene")
        ).all()
    assert [tuple(row) for row in rows] == [("run-a", "run-b")]


@pytest.mark.db
def test_two_overlapping_parcels_each_keep_their_scenes(db, tmp_path):
    neighbour = Query(**{**QUERY.model_dump(), "parcel_id": "nw_bornheim_east",
                         "bbox": dict(west=6.994, south=50.761, east=7.020, north=50.779)})
    load_log(db, write_log(tmp_path / "a.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="run-a"))
    load_log(db, write_log(tmp_path / "b.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="run-b",
                           query=neighbour))
    months = {(row["parcel_id"], row["scenes"]) for row in coverage(db)}
    assert months == {("nw_bornheim_core", 3), ("nw_bornheim_east", 3)}
    assert failed_checks(db) == []


@pytest.mark.db
def test_unreadable_page_keeps_the_counts_adding_up(db, tmp_path):
    stats = load_log(db, write_log(tmp_path / "fetch.jsonl", [b"<html>proxy error</html>",
                                                              raw(answer("search_cloud_max30"))]))
    assert stats.rejected == 1 and stats.scenes_seen == 4
    assert stats.scenes_seen == stats.inserted + stats.updated + stats.unchanged + stats.duplicates + stats.rejected
    with db.connect() as connection:
        row = connection.execute(text("SELECT reason, position, raw_record IS NULL FROM iris_staging.cdse_reject")).one()
    assert tuple(row) == ("unreadable_page", -1, True)  # SQL NULL, not the JSON value null
    assert failed_checks(db) == []


@pytest.mark.db
def test_damaged_log_line_is_counted_and_skipped(db, tmp_path):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))])
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"half a line')
    stats = load_log(db, path)
    assert stats.damaged_lines == 1 and stats.inserted == 3


@pytest.mark.db
def test_scenes_the_database_cannot_store_become_rejects(db, tmp_path):
    data = answer("search_cloud_max30")
    data["features"][0]["properties"]["eo:snow_cover"] = float("nan")
    data["features"][1]["properties"]["platform"] = "sentinel-2b\x00"
    data["features"][2]["id"] = "S" * 300
    stats = load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(data)]))
    assert (stats.inserted, stats.rejected) == (0, 3)
    with db.connect() as connection:
        reasons = sorted(connection.execute(text("SELECT reason FROM iris_staging.cdse_reject")).scalars())
    assert reasons == ["field_too_long", "unstorable_value", "unstorable_value"]
    assert failed_checks(db) == []


@pytest.mark.db
def test_footprint_with_heights_is_stored_flat(db, tmp_path):
    data = answer("search_cloud_max30")
    ring = data["features"][0]["geometry"]["coordinates"][0]
    data["features"][0]["geometry"]["coordinates"][0] = [[lon, lat, 120.0] for lon, lat in ring]
    assert load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(data)])).inserted == 3


@pytest.mark.db
def test_a_row_the_database_still_refuses_does_not_stop_the_page(db, tmp_path, monkeypatch):
    monkeypatch.setitem(records.TEXT_LIMITS, "platform", 1000)  # let a too-long value slip past the checks
    data = answer("search_cloud_max30")
    data["features"][0]["properties"]["platform"] = "p" * 60
    stats = load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(data)]))
    assert (stats.inserted, stats.rejected) == (2, 1)
    with db.connect() as connection:
        row = connection.execute(text("SELECT reason, detail FROM iris_staging.cdse_reject")).one()
    assert row.reason == "database_refused" and "too long" in row.detail
    assert failed_checks(db) == []


@pytest.mark.db
def test_same_id_with_different_content_is_kept_as_a_reject(db, tmp_path):
    first = answer("search_cloud_max30")
    second = answer("search_cloud_max30")
    second["features"][0]["properties"]["eo:cloud_cover"] = 2.0
    stats = load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(first), raw(second)]))
    assert (stats.inserted, stats.duplicates, stats.rejected) == (3, 2, 1)
    with db.connect() as connection:
        assert connection.execute(text("SELECT reason FROM iris_staging.cdse_reject")).scalar_one() == "conflicting_duplicate"


@pytest.mark.db
def test_more_scenes_than_one_statement_takes_load_in_batches(db, tmp_path):
    template = answer("search_cloud_max30")["features"][0]
    features = []
    for number in range(250):
        item = copy.deepcopy(template)
        item["id"] = f"{template['id']}_{number:03d}"
        features.append(item)
    stats = load_log(db, write_log(tmp_path / "fetch.jsonl", [raw({"type": "FeatureCollection", "features": features})]))
    assert stats.inserted == 250 and counts(db)["cdse_scene_fetch"] == 250


@pytest.mark.db
def test_reload_with_new_rules_replaces_old_rejects(db, tmp_path, monkeypatch):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_page1"))])
    assert load_log(db, path).rejected == 1
    lenient = QUERY.model_copy(update={"max_cloud_cover_pct": 100.0})
    monkeypatch.setattr(load_module, "clean_scene", lambda item, query: records.clean_scene(item, lenient))
    stats = load_log(db, path)
    assert (stats.rejected, stats.inserted) == (0, 1)
    assert counts(db)["cdse_reject"] == 0
    failed = [check.name for check in failed_checks(db)]
    assert failed == ["cloud cover is within 0 and the query limit"]  # the log still asks for 30 percent at most


@pytest.mark.db
def test_changed_log_is_refused_but_a_resumed_one_loads(db, tmp_path):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))], complete=False)
    load_log(db, path)
    RunLog(path).append(end_entry(fetch_id="20261028T150000Z_test0001", pages=1, finished_at="2026-10-28T15:00:09Z"))
    assert load_log(db, path).unchanged == 3  # the same log, grown by a resume
    other = write_log(tmp_path / "other.jsonl", [raw(answer("search_page1"))])  # same fetch id, other content
    with pytest.raises(LoadError, match="differs from the log loaded before"):
        load_log(db, other)
    forced = load_log(db, other, force=True)
    assert (forced.inserted, forced.unchanged, forced.rejected) == (0, 2, 1)


@pytest.mark.db
def test_a_second_load_while_one_runs_is_refused(db, tmp_path):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))])
    with db.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOAD_LOCK_KEY})
        try:
            with pytest.raises(LoadError, match="another load is running"):
                load_log(db, path)
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOAD_LOCK_KEY})


@pytest.mark.db
def test_load_cut_off_halfway_fails_validation_until_loaded_again(db, tmp_path, monkeypatch):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30")), raw(answer("search_page1"))])
    real_load_page = load_module._load_page
    calls = []

    def stop_on_page_two(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt  # Ctrl+C, a crash or a lost connection
        return real_load_page(*args, **kwargs)

    monkeypatch.setattr(load_module, "_load_page", stop_on_page_two)
    with pytest.raises(KeyboardInterrupt):
        load_log(db, path)
    assert "every load ran to the end" in [check.name for check in failed_checks(db)]
    monkeypatch.setattr(load_module, "_load_page", real_load_page)
    load_log(db, path)
    assert failed_checks(db) == []


@pytest.mark.db
def test_correct_empty_result_passes_validation(db, tmp_path):
    load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(answer("search_empty"))]))
    assert failed_checks(db) == []


@pytest.mark.db
def test_an_old_incomplete_run_does_not_fail_validation_once_a_newer_one_is_complete(db, tmp_path):
    load_log(db, write_log(tmp_path / "a.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="run-a",
                           complete=False, started_at="2026-10-01T00:00:00Z"))
    assert any(check.name.startswith("the newest fetch") for check in failed_checks(db))
    load_log(db, write_log(tmp_path / "b.jsonl", [raw(answer("search_cloud_max30"))], fetch_id="run-b",
                           started_at="2026-10-20T00:00:00Z"))
    assert failed_checks(db) == []


@pytest.mark.db
def test_stored_rows_are_counted_independently_of_the_loader(db, tmp_path):
    load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(answer("search_page1"))]))
    with db.begin() as connection:
        connection.execute(text("DELETE FROM iris_staging.cdse_reject"))
    assert [check.name for check in failed_checks(db)] == ["stored rows match the counts of every run"]


@pytest.mark.db
def test_load_result_uses_the_usual_etl_keys_and_adds_up(db, tmp_path):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30")), raw(answer("search_page1"))])
    result = load_log(db, path).as_dict()
    for key in ("processed", "inserted", "updated", "duplicates_removed", "failed", "execution_time"):
        assert key in result
    parts = result["inserted"] + result["updated"] + result["unchanged"] + result["duplicates_removed"] + result["failed"]
    assert result["processed"] == parts == 6


@pytest.mark.db
def test_footprints_are_multipolygons_in_wgs84(db, tmp_path):
    load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))]))
    with db.connect() as connection:
        kinds = connection.execute(
            text("SELECT DISTINCT GeometryType(footprint), ST_SRID(footprint) FROM iris_staging.cdse_scene")
        ).all()
    assert [tuple(kind) for kind in kinds] == [("MULTIPOLYGON", 4326)]


@pytest.mark.db
def test_the_table_itself_refuses_values_outside_their_range(db, tmp_path):
    load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))]))
    with pytest.raises(IntegrityError, match="ck_cdse_scene_cloud_cover_pct"):
        with db.begin() as connection:
            connection.execute(text("UPDATE iris_staging.cdse_scene SET cloud_cover_pct = 150"))


@pytest.mark.db
def test_validation_cannot_write(db):
    with pytest.raises(Exception, match="read-only"):
        _read_only(db, "UPDATE iris_staging.cdse_scene SET cloud_cover_pct = 1")


@pytest.mark.db
def test_a_database_with_iris_core_is_recognised_as_shared(db, admin_engine):
    assert shared_database_problem(db)[1] is None
    with admin_engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA iris_core"))
    try:
        assert "iris_core" in shared_database_problem(db)[1]
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text("DROP SCHEMA iris_core"))


@pytest.mark.db
def test_the_client_role_cannot_write_outside_iris_staging(db):
    with db.connect() as connection:
        if connection.exec_driver_sql("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").scalar():
            pytest.skip("the tests connect as a superuser here; set CDSE_TEST_DB_NAME to test as the client's role")
    for statement in ("CREATE TABLE public.cdse_stray (id int)", "CREATE SCHEMA iris_core"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with db.begin() as connection:
                connection.exec_driver_sql(statement)


@pytest.mark.db
def test_validation_checks_pass_and_coverage_is_reported(db, tmp_path):
    load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30")), raw(answer("search_page1"))]))
    assert failed_checks(db) == []
    months = coverage(db)
    assert [row["month"] for row in months] == ["2026-04"]
    assert months[0]["scenes"] == 3


@pytest.mark.db
def test_concurrent_loads_of_different_runs_do_not_double_count(db, tmp_path):
    paths = [write_log(tmp_path / f"{name}.jsonl", [raw(answer("search_cloud_max30"))], fetch_id=name)
             for name in ("run-a", "run-b")]
    results, refused = [], []

    def load(path):
        try:
            results.append(load_log(db, path))
        except LoadError:
            refused.append(path)  # the other load held the lock

    threads = [threading.Thread(target=load, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    results += [load_log(db, path) for path in refused]  # a refused load is simply run again, as its message says
    assert sum(result.inserted for result in results) == 3  # never 6: the same scenes are not counted as new twice
    assert failed_checks(db) == []


@pytest.mark.db
def test_reload_of_the_same_log_applies_new_cleaning_rules(db, tmp_path, monkeypatch):
    data = answer("search_cloud_max30")
    path = write_log(tmp_path / "fetch.jsonl", [raw(data)])
    load_log(db, path)
    monkeypatch.setattr(records, "CLEAN_RULES_VERSION", records.CLEAN_RULES_VERSION + 1)
    stats = load_log(db, path)
    assert stats.updated == 3
    with db.connect() as connection:
        stored = dict(connection.execute(text("SELECT scene_id, record_sha256 FROM iris_staging.cdse_scene")).all())
    assert stored == {item["id"]: records.record_fingerprint(item) for item in data["features"]}


@pytest.mark.db
def test_log_loaded_with_a_torn_tail_still_loads_after_a_resume(db, tmp_path):
    path = write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))], complete=False)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"half a line')
    load_log(db, path)
    log = RunLog(path)  # the resume sets the torn line aside and goes on
    log.append(page_entry(fetch_id="20261028T150000Z_test0001", query_fingerprint=QUERY.fingerprint(), page=2,
                          fetched_at="2026-10-28T15:00:05Z", url="https://stac.dataspace.copernicus.eu/v1/search",
                          request_body={"limit": 3}, status=200, seconds=0.1, raw_body=raw(answer("search_page1"))))
    log.append(end_entry(fetch_id="20261028T150000Z_test0001", pages=2, finished_at="2026-10-28T15:00:06Z"))
    assert load_log(db, path).pages == 2


@pytest.mark.db
def test_validation_details_print_utc_whatever_the_session_zone(db, tmp_path):
    load_log(db, write_log(tmp_path / "fetch.jsonl", [raw(answer("search_cloud_max30"))]))
    sql = (ROOT / "sql" / "validation.sql").read_text(encoding="utf-8")
    with db.connect() as connection:
        connection.exec_driver_sql("SET TimeZone = 'Europe/Berlin'")
        rows = connection.execute(text(sql)).all()
    detail = next(row.detail for row in rows if row.check_name == "every scene was taken inside its date window")
    assert detail.endswith("Z") and "+02" not in detail
