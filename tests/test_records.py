import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import String

from cdse_stac import records
from cdse_stac.config import Query
from cdse_stac.db import CdseScene
from cdse_stac.jsonsafe import loads, storable_copy, unstorable
from cdse_stac.records import CleanScene, Rejection, clean_scene, split_duplicates

FIXTURES = Path(__file__).resolve().parent / "fixtures"

QUERY = Query(
    parcel_id="nw_bornheim_core",
    collection="sentinel-2-l2a",
    bbox=dict(west=6.966, south=50.761, east=6.994, north=50.779),
    start="2026-04-01T00:00:00Z",
    end="2026-09-30T23:59:59Z",
    max_cloud_cover_pct=30,
)


def load_scenes(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["response"]["features"]


def refused(item):
    result = clean_scene(item, QUERY)
    assert isinstance(result, Rejection), result
    return result


def kept(item):
    result = clean_scene(item, QUERY)
    assert isinstance(result, CleanScene), result
    return result


@pytest.fixture
def scene():
    """A fresh copy of a real scene that passes every check."""
    return copy.deepcopy(load_scenes("search_cloud_max30")[0])


def test_real_scenes_under_the_limit_are_clean():
    results = [clean_scene(item, QUERY) for item in load_scenes("search_cloud_max30")]
    assert all(isinstance(result, CleanScene) and result.dropped_fields == () for result in results)


def test_clean_scene_fields_are_normalised(scene):
    clean = kept(scene)
    assert clean.scene_id == "S2B_MSIL2A_20260401T103619_N0512_R008_T31UGS_20260401T132331"
    assert clean.mgrs_tile == "31UGS"
    assert clean.acquired_at.isoformat() == "2026-04-01T10:36:19.024000+00:00"
    assert clean.cloud_cover_pct == 14.22
    assert clean.gsd_m == 10
    assert clean.footprint_wkt.startswith("MULTIPOLYGON")
    assert clean.raw_item == scene and clean.raw_item["assets"] == scene["assets"]
    assert json.loads(clean.raw_json) == scene


def test_text_limits_match_the_database_columns():
    for name, limit in records.TEXT_LIMITS.items():
        column_type = CdseScene.__table__.c[name].type
        assert isinstance(column_type, String) and column_type.length == limit, name


def test_missing_id_is_refused(scene):
    del scene["id"]
    assert refused(scene).reason == records.MISSING_ID


def test_id_of_the_wrong_type_says_so(scene):
    scene["id"] = 12345
    assert refused(scene).detail == "the id is a int, not text"


@pytest.mark.parametrize("scene_id", ["   ", " S2B_X ", "S2B_X\n"])
def test_ids_with_spaces_or_control_characters_are_refused_and_not_stored_in_rejects(scene, scene_id):
    scene["id"] = scene_id
    rejection = refused(scene)
    assert rejection.reason in (records.MISSING_ID, records.BAD_ID)
    assert rejection.scene_id is None


@pytest.mark.parametrize(
    "where, value",
    [
        ("id", "S" * 256),
        ("platform", "p" * 51),
        ("product:type", "t" * 51),
        ("processing:version", "9" * 21),
        ("grid:code", "MGRS-" + "T" * 11),
        ("sat:orbit_state", "o" * 21),
    ],
)
def test_text_longer_than_its_column_is_refused(scene, where, value):
    if where == "id":
        scene["id"] = value
    else:
        scene["properties"][where] = value
    rejection = refused(scene)
    assert rejection.reason == records.FIELD_TOO_LONG
    assert rejection.scene_id is None or len(rejection.scene_id) <= 255


@pytest.mark.parametrize(
    "path, value, problem",
    [
        (("properties", "eo:snow_cover"), float("nan"), "nan"),
        (("properties", "view:sun_azimuth"), float("inf"), "inf"),
        (("properties", "platform"), "sentinel-2b\x00", "NUL"),
        (("properties", "constellation"), "\ud800", "lone surrogate"),
    ],
)
def test_values_postgresql_cannot_store_are_refused(scene, path, value, problem):
    scene[path[0]][path[1]] = value
    rejection = refused(scene)
    assert rejection.reason == records.UNSTORABLE_VALUE and problem in rejection.detail
    copy_for_reject = storable_copy(scene)
    assert unstorable(copy_for_reject) is None  # the reject row can hold the evidence


@pytest.mark.parametrize("value", [float("nan"), "sentinel-2b\x00"], ids=["NaN", "NUL"])
def test_values_read_from_json_text_are_refused_too(scene, value):
    scene["properties"]["platform"] = value
    text = json.dumps(scene)  # written as NaN and \u0000, which Python's JSON reader accepts again
    assert "NaN" in text or "\\u0000" in text
    assert refused(loads(text)).reason == records.UNSTORABLE_VALUE


def test_huge_integers_do_not_crash(scene):
    scene["properties"]["eo:cloud_cover"] = 10**400
    assert refused(scene).reason == records.BAD_CLOUD_COVER
    scene["properties"]["eo:cloud_cover"] = 10
    scene["properties"]["gsd"] = 10**400
    assert kept(scene).gsd_m is None and "gsd_m" in kept(scene).dropped_fields


@pytest.mark.parametrize("value", ["9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00"])
def test_times_at_the_edge_of_the_calendar_are_refused_not_crashed(scene, value):
    scene["properties"]["datetime"] = value
    assert refused(scene).reason == records.BAD_DATETIME
    scene["properties"]["datetime"] = "2026-04-01T10:36:19.024Z"
    scene["properties"]["published"] = value
    assert kept(scene).published_at is None


def test_relative_orbit_outside_32_bits_is_left_empty(scene):
    scene["properties"]["sat:relative_orbit"] = 2**40
    clean = kept(scene)
    assert clean.relative_orbit is None and "relative_orbit" in clean.dropped_fields


@pytest.mark.parametrize(
    "key, value, field_name",
    [("eo:snow_cover", 150, "snow_cover_pct"), ("view:sun_elevation", 120, "sun_elevation_deg"), ("gsd", -10, "gsd_m")],
)
def test_measurements_outside_their_range_are_left_empty_and_named(scene, key, value, field_name):
    scene["properties"][key] = value
    clean = kept(scene)
    assert getattr(clean, field_name) is None and field_name in clean.dropped_fields


def test_optional_text_of_the_wrong_type_is_named(scene):
    scene["properties"]["platform"] = 42
    assert "platform" in kept(scene).dropped_fields


def test_datetime_null_with_start_and_end_is_accepted(scene):
    scene["properties"]["start_datetime"] = scene["properties"]["datetime"]
    scene["properties"]["end_datetime"] = scene["properties"]["datetime"]
    scene["properties"]["datetime"] = None
    assert kept(scene).acquired_at.isoformat() == "2026-04-01T10:36:19.024000+00:00"


def test_wrong_collection_is_refused(scene):
    scene["collection"] = "sentinel-1-grd"
    assert refused(scene).reason == records.WRONG_COLLECTION


def test_missing_properties_is_refused(scene):
    del scene["properties"]
    assert refused(scene).reason == records.MISSING_PROPERTIES


def test_missing_footprint_is_refused(scene):
    scene["geometry"] = None
    assert refused(scene).reason == records.MISSING_GEOMETRY


def test_footprint_that_is_not_a_polygon_is_refused(scene):
    scene["geometry"] = {"type": "Point", "coordinates": [6.98, 50.77]}
    assert refused(scene).reason == records.BAD_GEOMETRY


def test_self_crossing_footprint_is_refused(scene):
    # A bow-tie shape: its edges cross, so it is not a valid polygon.
    bow_tie = [[6.9, 50.7], [7.1, 50.9], [7.1, 50.7], [6.9, 50.9], [6.9, 50.7]]
    scene["geometry"] = {"type": "Polygon", "coordinates": [bow_tie]}
    assert refused(scene).reason == records.BAD_GEOMETRY


def test_footprint_with_swapped_coordinates_is_refused(scene):
    # GeoJSON is longitude, latitude. Swapped pairs move the scene to East Africa.
    ring = scene["geometry"]["coordinates"][0]
    scene["geometry"]["coordinates"][0] = [[lat, lon] for lon, lat in ring]
    rejection = refused(scene)
    assert rejection.reason == records.OUTSIDE_QUERY_BBOX and "swapped" in rejection.detail


def test_footprint_far_away_does_not_blame_swapped_coordinates(scene):
    ring = scene["geometry"]["coordinates"][0]
    scene["geometry"]["coordinates"][0] = [[lon + 20, lat] for lon, lat in ring]
    del scene["bbox"]
    rejection = refused(scene)
    assert rejection.reason == records.OUTSIDE_QUERY_BBOX and "swapped" not in rejection.detail


def test_footprint_with_heights_is_kept_flat(scene):
    ring = scene["geometry"]["coordinates"][0]
    scene["geometry"]["coordinates"][0] = [[lon, lat, 100.0] for lon, lat in ring]
    clean = kept(scene)
    assert "Z" not in clean.footprint_wkt and "footprint height" in clean.dropped_fields


def test_coordinates_written_as_text_are_refused(scene):
    ring = scene["geometry"]["coordinates"][0]
    scene["geometry"]["coordinates"][0] = [[str(lon), lat] for lon, lat in ring]
    assert refused(scene).reason == records.BAD_GEOMETRY


def test_footprint_crossing_the_date_line_unsplit_is_refused():
    query = Query(**{**QUERY.model_dump(), "bbox": dict(west=179.9, south=50.0, east=180.0, north=50.1)})
    item = copy.deepcopy(load_scenes("search_cloud_max30")[0])
    ring = [[179.5, 49.5], [-179.5, 49.5], [-179.5, 50.5], [179.5, 50.5], [179.5, 49.5]]
    item["geometry"] = {"type": "Polygon", "coordinates": [ring]}
    del item["bbox"]
    result = clean_scene(item, query)
    assert isinstance(result, Rejection) and "date line" in result.detail


def test_item_bbox_must_contain_the_footprint(scene):
    scene["bbox"] = [0, 0, 1, 1]
    assert refused(scene).reason == records.BAD_GEOMETRY
    scene["bbox"] = "not a box"
    assert refused(scene).reason == records.BAD_GEOMETRY


def test_missing_assets_are_refused(scene):
    scene["assets"] = {}
    assert refused(scene).reason == records.MISSING_ASSETS


def test_assets_without_an_href_are_refused(scene):
    scene["assets"]["thumbnail"] = "not an object"
    assert refused(scene).reason == records.BAD_ASSETS


def test_date_without_time_zone_is_refused(scene):
    scene["properties"]["datetime"] = "2026-04-01T10:36:19"
    assert refused(scene).reason == records.BAD_DATETIME


def test_unreadable_date_is_refused(scene):
    scene["properties"]["datetime"] = "first of April"
    assert refused(scene).reason == records.BAD_DATETIME


def test_date_of_the_wrong_type_says_so(scene):
    scene["properties"]["datetime"] = 1712000000
    assert "not a time written as text" in refused(scene).detail


def test_date_outside_the_window_is_refused(scene):
    scene["properties"]["datetime"] = "2025-04-01T10:36:19Z"
    assert refused(scene).reason == records.OUTSIDE_DATE_WINDOW


@pytest.mark.parametrize("value", [150, -5, "14.2", None, True])
def test_bad_or_missing_cloud_cover_is_refused(scene, value):
    scene["properties"]["eo:cloud_cover"] = value
    expected = records.MISSING_CLOUD_COVER if value is None else records.BAD_CLOUD_COVER
    assert refused(scene).reason == expected


def test_cloud_above_the_limit_is_refused_even_after_the_server_filter():
    cloudy = [item for item in load_scenes("search_page1") if item["properties"]["eo:cloud_cover"] > 30]
    assert cloudy, "page 1 holds cloudy scenes"
    assert {refused(item).reason for item in cloudy} == {records.ABOVE_CLOUD_LIMIT}


def test_something_that_is_not_a_scene_is_refused():
    assert refused("not a scene").reason == records.NOT_A_SCENE


def test_rejection_keeps_the_scene_id_when_known(scene):
    scene["properties"]["eo:cloud_cover"] = 150
    assert refused(scene).scene_id == scene["id"]


def test_fingerprint_ignores_key_order(scene):
    reordered = dict(reversed(list(scene.items())))
    assert kept(scene).record_sha256 == kept(reordered).record_sha256


def test_fingerprint_changes_when_the_content_changes(scene):
    before = kept(scene).record_sha256
    scene["properties"]["eo:cloud_cover"] = 15.0
    assert kept(scene).record_sha256 != before


def test_fingerprint_changes_when_the_cleaning_rules_change(scene, monkeypatch):
    before = kept(scene).record_sha256
    monkeypatch.setattr(records, "CLEAN_RULES_VERSION", records.CLEAN_RULES_VERSION + 1)
    assert kept(scene).record_sha256 != before


def test_duplicates_are_counted_not_refused():
    page = [clean_scene(item, QUERY) for item in load_scenes("search_cloud_max30")]
    kept_scenes, duplicates, conflicts = split_duplicates(page + page[:1])
    assert len(kept_scenes) == len(page)
    assert [d.scene_id for d in duplicates] == [page[0].scene_id] and conflicts == []


def test_duplicates_are_found_across_pages():
    seen = {}
    page = [clean_scene(item, QUERY) for item in load_scenes("search_cloud_max30")]
    split_duplicates(page, seen)
    kept_scenes, duplicates, _ = split_duplicates(page[:2], seen)
    assert kept_scenes == [] and len(duplicates) == 2


def test_same_id_with_different_content_is_a_conflict(scene):
    first = kept(scene)
    scene["properties"]["eo:cloud_cover"] = 1.0
    second = kept(scene)
    kept_scenes, duplicates, conflicts = split_duplicates([first, second])
    assert kept_scenes == [first] and duplicates == [] and conflicts == [second]


def test_item_bbox_with_a_huge_integer_is_refused_not_crashed(scene):
    scene["bbox"] = [10**400, 50.3, 7.5, 51.5]
    scene = loads(json.dumps(scene))
    assert refused(scene).reason == records.BAD_GEOMETRY


def test_any_unexpected_error_in_the_cleaning_becomes_a_rejection(scene, monkeypatch):
    def broken(*args, **kwargs):
        raise ZeroDivisionError("a rule that was not written for this input")

    monkeypatch.setattr(records, "_assets", broken)
    rejection = refused(scene)
    assert rejection.reason == records.BAD_RECORD and "ZeroDivisionError" in rejection.detail
