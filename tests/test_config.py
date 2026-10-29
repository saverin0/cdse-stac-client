import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from cdse_stac.config import BBox, Query, Settings, SettingsError, SourceSettings, load_settings

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "bornheim.toml"
FIXTURES = ROOT / "tests" / "fixtures"
GOOD_BOX = dict(west=6.966, south=50.761, east=6.994, north=50.779)


def make_query(**changes):
    values = dict(
        parcel_id="nw_bornheim_core",
        collection="sentinel-2-l2a",
        bbox=GOOD_BOX,
        start="2026-04-01T00:00:00Z",
        end="2026-09-30T23:59:59Z",
        max_cloud_cover_pct=30,
    )
    values.update(changes)
    return Query(**values)


def example_source(**changes):
    return SourceSettings(**{**load_settings(EXAMPLE).source.model_dump(), **changes})


def test_example_config_loads():
    settings = load_settings(EXAMPLE)
    assert settings.query.parcel_id == "nw_bornheim_core"
    assert settings.query.bbox.as_stac_list() == [6.966, 50.761, 6.994, 50.779]
    assert settings.query.start.isoformat() == "2026-04-01T00:00:00+00:00"


def test_stac_bbox_is_longitude_first():
    # STAC wants west, south, east, north: longitude, latitude, longitude, latitude.
    west, south, east, north = make_query().search_body(page_size=50)["bbox"]
    assert (west, east) == (6.966, 6.994)
    assert (south, north) == (50.761, 50.779)


@pytest.mark.parametrize(
    "box, side",
    [
        (dict(GOOD_BOX, west=6.994, east=6.966), "west"),  # longitudes swapped
        (dict(GOOD_BOX, south=50.779, north=50.761), "south"),  # latitudes swapped
        (dict(GOOD_BOX, east=6.966), "west"),  # zero width
        (dict(GOOD_BOX, north=91.0), "north"),  # beyond the pole
        (dict(GOOD_BOX, west=-181.0), "west"),  # longitude out of range
        (dict(GOOD_BOX, west=math.nan), "west"),  # not a number
        (dict(GOOD_BOX, north=True), "north"),  # true would be read as 1.0
        (dict(GOOD_BOX, north="50.779"), "north"),  # a number in quotes
    ],
)
def test_invalid_boxes_are_refused(box, side):
    with pytest.raises(ValidationError, match=side):
        BBox(**box)


def test_box_must_name_its_sides():
    # A plain list hides which number is which; named sides make a mix-up visible.
    with pytest.raises(ValidationError):
        make_query(bbox=[6.966, 50.761, 6.994, 50.779])


def test_typo_in_a_setting_is_refused():
    with pytest.raises(ValidationError):
        BBox(**GOOD_BOX, nort=50.8)


def test_window_must_run_forward():
    with pytest.raises(ValidationError, match="before"):
        make_query(start="2026-09-30T00:00:00Z", end="2026-04-01T00:00:00Z")


def test_dates_need_a_time_zone():
    with pytest.raises(ValidationError, match="time zone"):
        make_query(start="2026-04-01T00:00:00")


@pytest.mark.parametrize("value", [1711929600, 1711929600.0, True])
def test_dates_written_as_numbers_are_refused(value):
    # A number would be read as seconds since 1970, so the window would silently start elsewhere.
    with pytest.raises(ValidationError, match="write the time as text"):
        make_query(start=value)


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_dates_at_the_edge_of_the_calendar_are_refused_not_crashed(value):
    with pytest.raises(ValidationError, match="range of dates"):
        make_query(end=value)


def test_dates_are_turned_into_utc():
    query = make_query(start="2026-04-01T02:00:00+02:00")
    assert query.stac_datetime() == "2026-04-01T00:00:00Z/2026-09-30T23:59:59Z"


@pytest.mark.parametrize("value", [-1, 100.5, True, "30", math.inf])
def test_cloud_limit_must_be_a_percentage(value):
    with pytest.raises(ValidationError):
        make_query(max_cloud_cover_pct=value)


@pytest.mark.parametrize("parcel_id", ["p" * 101, "con", "lpt1", "Upper", "with space"])
def test_parcel_id_must_fit_its_column_and_be_a_usable_folder_name(parcel_id):
    with pytest.raises(ValidationError):
        make_query(parcel_id=parcel_id)


@pytest.mark.parametrize("collection", [" sentinel-2-l2a", "sentinel-2-l2a ", "", "c" * 101, "a/b"])
def test_collection_must_be_a_plain_id(collection):
    with pytest.raises(ValidationError):
        make_query(collection=collection)


def test_search_body_equals_the_request_the_server_accepted():
    fixture = json.loads((FIXTURES / "search_cloud_max30.json").read_text(encoding="utf-8"))
    accepted = fixture["request"]["body"]
    assert make_query().search_body(page_size=accepted["limit"]) == accepted


def test_fingerprint_is_stable_and_follows_the_question():
    assert make_query().fingerprint() == make_query().fingerprint()
    assert make_query().fingerprint() != make_query(max_cloud_cover_pct=20).fingerprint()


def test_fingerprint_of_the_example_question_is_unchanged():
    # Logs fetched before this version still match their question.
    assert load_settings(EXAMPLE).query.fingerprint().startswith("952f5d4d")


def test_search_url_must_be_an_allowed_https_host():
    with pytest.raises(ValidationError, match="allowed_hosts"):
        example_source(search_url="https://example.com/stac/search")
    with pytest.raises(ValidationError, match="https"):
        example_source(search_url="http://catalogue.dataspace.copernicus.eu/stac/search")


def test_search_url_with_a_backslash_trick_is_refused():
    with pytest.raises(ValidationError, match="backslash"):
        example_source(search_url="https://evil.example\\@catalogue.dataspace.copernicus.eu/stac/search")


def test_search_url_on_another_port_is_refused():
    with pytest.raises(ValidationError, match="port"):
        example_source(search_url="https://catalogue.dataspace.copernicus.eu:8443/stac/search")


def test_allowed_hosts_are_normalised():
    source = example_source(allowed_hosts=["Catalogue.Dataspace.Copernicus.EU.", " stac.dataspace.copernicus.eu "])
    assert source.allowed_hosts == ("catalogue.dataspace.copernicus.eu", "stac.dataspace.copernicus.eu")


@pytest.mark.parametrize(
    "host, problem",
    [
        ("https://stac.dataspace.copernicus.eu", "plain host name"),
        ("stac.dataspace.copernicus.eu:443", "plain host name"),
        ("evil.example", "not a Copernicus"),
        ("dataspace.copernicus.eu.evil.example", "not a Copernicus"),
    ],
)
def test_allowed_hosts_must_be_plain_copernicus_hosts(host, problem):
    with pytest.raises(ValidationError, match=problem):
        example_source(allowed_hosts=["catalogue.dataspace.copernicus.eu", host])


@pytest.mark.parametrize(
    "name, value",
    [
        ("connect_timeout_s", math.inf),
        ("read_timeout_s", 301),
        ("total_timeout_s", 0),
        ("requests_per_second", 1e-300),
        ("page_size", 1001),
        ("page_size", True),
        ("max_attempts", "5"),
        ("max_pages", 0),
    ],
)
def test_source_numbers_have_bounds_and_types(name, value):
    with pytest.raises(ValidationError, match=name):
        example_source(**{name: value})


def test_box_larger_than_max_box_deg_is_refused():
    settings = load_settings(EXAMPLE).model_dump()
    settings["query"]["bbox"] = dict(west=-10.0, south=35.0, east=30.0, north=60.0)
    with pytest.raises(ValidationError, match="max_box_deg"):
        Settings.model_validate(settings)


def test_runs_dir_expands_home_and_refuses_parent_steps():
    settings = load_settings(EXAMPLE).model_dump()
    assert "~" not in str(Settings.model_validate({**settings, "runs_dir": "~/cdse"}).runs_dir)
    with pytest.raises(ValidationError, match=r"\.\."):
        Settings.model_validate({**settings, "runs_dir": "data/../../elsewhere"})


def test_missing_settings_file_is_a_clear_error(tmp_path):
    with pytest.raises(SettingsError, match="does not exist"):
        load_settings(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    "content, problem",
    [
        (b"north =\n", "cannot be read"),
        (b"[query.bbox]\neast = 6,994\n", "cannot be read"),
        (b"[query.bbox]\nnorth = 1\nnorth = 2\n", "cannot be read"),
        (b"runs_dir = \"caf\xe9\"\n", "not UTF-8"),
    ],
    ids=["no-value", "comma-decimal", "twice", "latin-1"],
)
def test_broken_settings_files_give_one_clear_message(tmp_path, content, problem):
    path = tmp_path / "broken.toml"
    path.write_bytes(content)
    with pytest.raises(SettingsError, match=problem):
        load_settings(path)


def test_settings_file_with_a_byte_order_mark_is_read(tmp_path):
    path = tmp_path / "bom.toml"
    path.write_bytes(b"\xef\xbb\xbf" + EXAMPLE.read_bytes())
    assert load_settings(path).query.parcel_id == "nw_bornheim_core"
