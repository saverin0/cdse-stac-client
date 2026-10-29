"""Turn one raw STAC scene into a clean row, or refuse it with a reason.

Nothing is dropped silently. Every scene becomes either a CleanScene, ready
for the staging table, or a Rejection that says why it was refused. The load
step stores both, so the numbers always add up. An optional value that is
present but cannot be read is left empty and named in `dropped_fields`; the
raw value always stays in raw_item.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from shapely import force_2d
from shapely.geometry import MultiPolygon, box, shape
from shapely.validation import explain_validity

from cdse_stac.config import Query
from cdse_stac.jsonsafe import printable, unstorable

# Bump when the cleaning rules change: the fingerprint includes it, so loading an old log
# again applies the new rules to rows cleaned by the old ones.
CLEAN_RULES_VERSION = 2

# Reason codes. Short fixed words, so refusals can be counted and grouped.
NOT_A_SCENE = "not_a_scene"
UNSTORABLE_VALUE = "unstorable_value"
MISSING_ID = "missing_id"
BAD_ID = "bad_id"
FIELD_TOO_LONG = "field_too_long"
WRONG_COLLECTION = "wrong_collection"
MISSING_PROPERTIES = "missing_properties"
BAD_DATETIME = "bad_datetime"
OUTSIDE_DATE_WINDOW = "outside_date_window"
MISSING_CLOUD_COVER = "missing_cloud_cover"
BAD_CLOUD_COVER = "bad_cloud_cover"
ABOVE_CLOUD_LIMIT = "above_cloud_limit"
MISSING_GEOMETRY = "missing_geometry"
BAD_GEOMETRY = "bad_geometry"
OUTSIDE_QUERY_BBOX = "outside_query_bbox"
MISSING_ASSETS = "missing_assets"
BAD_ASSETS = "bad_assets"
CONFLICTING_DUPLICATE = "conflicting_duplicate"
BAD_RECORD = "bad_record"  # a rule failed in a way no check foresaw; kept as a reject, never a crash

# The longest text each column holds; tests/test_records.py checks these against db.py.
TEXT_LIMITS = {
    "scene_id": 255,
    "collection": 100,
    "platform": 50,
    "product_type": 50,
    "processing_baseline": 20,
    "mgrs_tile": 10,
    "orbit_state": 20,
}
INT32 = (-(2**31), 2**31 - 1)


@dataclass(frozen=True)
class Rejection:
    """A scene we refused, and why."""

    reason: str
    detail: str
    scene_id: str | None


@dataclass(frozen=True)
class CleanScene:
    """One scene, checked and normalised, ready for iris_staging.cdse_scene."""

    scene_id: str
    collection: str
    platform: str | None
    product_type: str | None
    processing_baseline: str | None
    mgrs_tile: str | None
    relative_orbit: int | None
    orbit_state: str | None
    acquired_at: datetime
    published_at: datetime | None
    source_updated_at: datetime | None
    cloud_cover_pct: float
    snow_cover_pct: float | None
    gsd_m: float | None
    sun_elevation_deg: float | None
    footprint_wkt: str
    bbox_west: float
    bbox_south: float
    bbox_east: float
    bbox_north: float
    thumbnail_url: str | None
    raw_item: dict
    record_sha256: str
    raw_json: str  # raw_item as canonical JSON, made once for the fingerprint and reused for the database
    dropped_fields: tuple[str, ...] = ()  # optional values present but unreadable, so left empty


class _Refused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def usable_id(item: Any) -> str | None:
    """The scene id if it can be stored as it is, else None."""
    scene_id = item.get("id") if isinstance(item, dict) else None
    if not isinstance(scene_id, str) or not scene_id or scene_id != scene_id.strip():
        return None
    if len(scene_id) > TEXT_LIMITS["scene_id"] or unstorable(scene_id) or not scene_id.isprintable():
        return None
    return scene_id


def clean_scene(item: Any, query: Query) -> CleanScene | Rejection:
    """Check one raw scene against the query. Never raises for bad data."""
    try:
        return _clean(item, query)
    except _Refused as refused:
        return Rejection(reason=refused.reason, detail=refused.detail, scene_id=usable_id(item))
    except Exception as error:  # the promise above holds even for input no rule was written for
        return Rejection(BAD_RECORD, f"{type(error).__name__}: {printable(str(error), 200)}", usable_id(item))


def split_duplicates(
    scenes: list[CleanScene], seen: dict[str, str] | None = None
) -> tuple[list[CleanScene], list[CleanScene], list[CleanScene]]:
    """Keep the first copy of each scene id. Returns (kept, duplicates, conflicts).

    A duplicate is the same scene again. A conflict has the same id but different content
    than the copy kept; it is refused, so the difference is on record.
    Pass one `seen` dict (scene id to fingerprint) for a whole run to catch a scene on two pages.
    """
    seen = {} if seen is None else seen
    kept: list[CleanScene] = []
    duplicates: list[CleanScene] = []
    conflicts: list[CleanScene] = []
    for scene in scenes:
        first = seen.get(scene.scene_id)
        if first is None:
            seen[scene.scene_id] = scene.record_sha256
            kept.append(scene)
        elif first == scene.record_sha256:
            duplicates.append(scene)
        else:
            conflicts.append(scene)
    return kept, duplicates, conflicts


def canonical_json(item: dict) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_fingerprint(item: dict, canonical: str | None = None) -> str:
    """sha256 of the scene with its keys sorted, plus the version of the cleaning rules."""
    text = canonical if canonical is not None else canonical_json(item)
    return hashlib.sha256(f"rules {CLEAN_RULES_VERSION}\n{text}".encode()).hexdigest()


def _clean(item: Any, query: Query) -> CleanScene:
    if not isinstance(item, dict):
        raise _Refused(NOT_A_SCENE, f"expected a JSON object, got {type(item).__name__}")

    problem = unstorable(item)
    if problem:
        raise _Refused(UNSTORABLE_VALUE, problem)

    scene_id = item.get("id")
    if scene_id is None:
        raise _Refused(MISSING_ID, "the scene has no id")
    if not isinstance(scene_id, str):
        raise _Refused(MISSING_ID, f"the id is a {type(scene_id).__name__}, not text")
    if not scene_id.strip():
        raise _Refused(MISSING_ID, "the id is empty")
    if scene_id != scene_id.strip() or not scene_id.isprintable():
        raise _Refused(BAD_ID, f"the id {printable(scene_id, 80)!r} has spaces around it or control characters")
    _fits("scene_id", scene_id)

    collection = item.get("collection")
    if collection != query.collection:
        shown = printable(collection, 80) if isinstance(collection, str) else collection
        raise _Refused(WRONG_COLLECTION, f"collection is {shown!r}, asked for {query.collection!r}")

    props = item.get("properties")
    if not isinstance(props, dict):
        raise _Refused(MISSING_PROPERTIES, "the scene has no properties object")

    acquired_at = _acquired_at(props)
    if not query.start <= acquired_at <= query.end:
        raise _Refused(OUTSIDE_DATE_WINDOW, f"taken {acquired_at.isoformat()}, outside {query.stac_datetime()}")

    cloud = props.get("eo:cloud_cover")
    if cloud is None:
        raise _Refused(MISSING_CLOUD_COVER, "eo:cloud_cover is missing")
    cloud_pct = _number(cloud)
    if cloud_pct is None or not 0 <= cloud_pct <= 100:
        raise _Refused(BAD_CLOUD_COVER, f"eo:cloud_cover {printable(repr(cloud), 60)} is not a number from 0 to 100")
    if cloud_pct > query.max_cloud_cover_pct:
        raise _Refused(ABOVE_CLOUD_LIMIT, f"cloud cover {cloud_pct} is above the limit {query.max_cloud_cover_pct}")

    footprint, flattened = _footprint(item.get("geometry"), query)
    _bbox_agrees(item.get("bbox"), footprint)
    assets = _assets(item.get("assets"))

    dropped: list[str] = ["footprint height"] if flattened else []
    texts = {
        "platform": props.get("platform"),
        "product_type": props.get("product:type"),
        "processing_baseline": props.get("processing:version"),
        "orbit_state": props.get("sat:orbit_state"),
    }
    grid = props.get("grid:code")
    texts["mgrs_tile"] = grid.removeprefix("MGRS-") if isinstance(grid, str) else grid
    clean_texts = {name: _optional_text(name, value, dropped) for name, value in texts.items()}

    west, south, east, north = footprint.bounds
    thumbnail = assets.get("thumbnail")
    canonical = canonical_json(item)
    return CleanScene(
        scene_id=scene_id,
        collection=query.collection,
        platform=clean_texts["platform"],
        product_type=clean_texts["product_type"],
        processing_baseline=clean_texts["processing_baseline"],
        mgrs_tile=clean_texts["mgrs_tile"],
        relative_orbit=_optional(
            "relative_orbit", props.get("sat:relative_orbit"), _whole_int32, dropped
        ),
        orbit_state=clean_texts["orbit_state"],
        acquired_at=acquired_at,
        published_at=_optional("published_at", props.get("published"), _time_or_none, dropped),
        source_updated_at=_optional("source_updated_at", props.get("updated"), _time_or_none, dropped),
        cloud_cover_pct=cloud_pct,
        snow_cover_pct=_optional("snow_cover_pct", props.get("eo:snow_cover"), _in_range(0, 100), dropped),
        gsd_m=_optional("gsd_m", props.get("gsd"), _in_range(0, math.inf, low_open=True), dropped),
        sun_elevation_deg=_optional(
            "sun_elevation_deg", props.get("view:sun_elevation"), _in_range(-90, 90), dropped
        ),
        footprint_wkt=footprint.wkt,
        bbox_west=west,
        bbox_south=south,
        bbox_east=east,
        bbox_north=north,
        thumbnail_url=thumbnail["href"] if isinstance(thumbnail, dict) else None,
        raw_item=item,
        record_sha256=record_fingerprint(item, canonical),
        raw_json=canonical,
        dropped_fields=tuple(dropped),
    )


def _fits(name: str, value: str) -> None:
    if len(value) > TEXT_LIMITS[name]:
        raise _Refused(FIELD_TOO_LONG, f"{name} has {len(value)} characters, the column holds {TEXT_LIMITS[name]}")


def _optional(name: str, value: Any, read, dropped: list[str]):
    """An optional value: read it, or leave it empty and note it when it is present but unreadable."""
    if value is None:
        return None
    result = read(value)
    if result is None:
        dropped.append(name)
    return result


def _optional_text(name: str, value: Any, dropped: list[str]) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.isprintable():
        dropped.append(name)
        return None
    _fits(name, value)
    return value


def _acquired_at(props: dict) -> datetime:
    """STAC allows datetime to be null when start_datetime and end_datetime are given; then the start counts."""
    value = props.get("datetime")
    if value is None and props.get("start_datetime") is not None:
        return _required_time(props.get("start_datetime"), "properties.start_datetime")
    return _required_time(value, "properties.datetime")


def _required_time(value: Any, name: str = "properties.datetime") -> datetime:
    if value is None:
        raise _Refused(BAD_DATETIME, f"{name} is missing")
    if not isinstance(value, str) or not value:
        raise _Refused(BAD_DATETIME, f"{name} is a {type(value).__name__}, not a time written as text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _Refused(BAD_DATETIME, f"{name} {printable(value, 60)!r} is not an ISO 8601 time") from None
    if parsed.tzinfo is None:
        raise _Refused(BAD_DATETIME, f"{name} {value!r} has no time zone")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        raise _Refused(BAD_DATETIME, f"{name} {value!r} is outside the range of dates that can be handled") from None


def _time_or_none(value: Any) -> datetime | None:
    try:
        return _required_time(value)
    except _Refused:
        return None


def _coordinates_are_numbers(coordinates: Any) -> bool:
    stack = [coordinates]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, bool) or not isinstance(current, (int, float)):
            return False
    return True


def _footprint(geometry: Any, query: Query) -> tuple[MultiPolygon, bool]:
    if not geometry:
        raise _Refused(MISSING_GEOMETRY, "the scene has no footprint")
    if not isinstance(geometry, dict) or not _coordinates_are_numbers(geometry.get("coordinates")):
        raise _Refused(BAD_GEOMETRY, "footprint coordinates must be numbers")
    try:
        shp = shape(geometry)
    except Exception as error:  # shapely raises several error types for broken GeoJSON
        raise _Refused(BAD_GEOMETRY, f"footprint cannot be read: {printable(str(error), 120)}") from None
    if shp.geom_type not in ("Polygon", "MultiPolygon"):
        raise _Refused(BAD_GEOMETRY, f"footprint is a {shp.geom_type}, expected a polygon")
    if shp.is_empty:
        raise _Refused(BAD_GEOMETRY, "footprint is empty")
    flattened = shp.has_z
    if flattened:
        shp = force_2d(shp)  # the column holds 2D footprints; the heights stay in raw_item
    if not shp.is_valid:
        raise _Refused(BAD_GEOMETRY, f"footprint is not a valid polygon: {explain_validity(shp)}")
    west, south, east, north = shp.bounds
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise _Refused(BAD_GEOMETRY, "footprint coordinates are outside longitude and latitude ranges")
    if east - west > 180:
        raise _Refused(
            BAD_GEOMETRY,
            f"footprint spans {east - west:.1f} degrees of longitude; it probably crosses the date line "
            "without being split into two polygons (GeoJSON RFC 7946, section 3.1.9)",
        )
    parcel = box(*query.bbox.as_stac_list())
    if not shp.intersects(parcel):
        detail = f"footprint ({west:.4f}, {south:.4f}, {east:.4f}, {north:.4f}) does not touch the parcel box"
        if box(south, west, north, east).intersects(parcel):
            detail += "; longitude and latitude look swapped"
        raise _Refused(OUTSIDE_QUERY_BBOX, detail)
    return (shp if isinstance(shp, MultiPolygon) else MultiPolygon([shp])), flattened


def _bbox_agrees(bbox: Any, footprint: MultiPolygon) -> None:
    """The item bbox, when given, must be 4 or 6 numbers and contain the footprint."""
    if bbox is None:
        return
    if not isinstance(bbox, list) or len(bbox) not in (4, 6) or not _coordinates_are_numbers(bbox):
        raise _Refused(BAD_GEOMETRY, "bbox is not a list of 4 or 6 numbers")
    values = [_number(value) for value in bbox]
    if None in values:
        raise _Refused(BAD_GEOMETRY, "bbox holds a value that is not a finite number")
    west, south, east, north = (values[0], values[1], values[2], values[3]) if len(values) == 4 else (
        values[0], values[1], values[3], values[4])
    f_west, f_south, f_east, f_north = footprint.bounds
    tolerance = 1e-6
    if f_west < west - tolerance or f_south < south - tolerance or f_east > east + tolerance or f_north > north + tolerance:
        raise _Refused(BAD_GEOMETRY, f"bbox {bbox} does not contain the footprint {footprint.bounds}")


def _assets(assets: Any) -> dict:
    if not isinstance(assets, dict) or not assets:
        raise _Refused(MISSING_ASSETS, "the scene lists no assets")
    for key, asset in assets.items():
        if not isinstance(asset, dict) or not isinstance(asset.get("href"), str) or not asset["href"]:
            raise _Refused(BAD_ASSETS, f"asset {printable(str(key), 60)!r} is not an object with an href")
    return assets


def _number(value: Any) -> float | None:
    """A finite int or float as a float. Text, true/false, NaN and numbers too large for a float become None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _in_range(low: float, high: float, low_open: bool = False):
    def read(value: Any) -> float | None:
        number = _number(value)
        if number is None or number > high or number < low or (low_open and number == low):
            return None
        return number

    return read


def _whole_int32(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if INT32[0] <= value <= INT32[1] else None
