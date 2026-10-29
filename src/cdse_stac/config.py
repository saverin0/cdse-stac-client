"""Settings for one search: where to ask, what to ask, where to write.

The values come from a TOML file such as config/bornheim.toml. Everything is
checked here, before any request is sent, so a wrong box or date window fails
at once with a clear message instead of quietly returning the wrong scenes.
Numbers must be written as numbers: true, false and "50.7" in quotes are refused.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, ConfigDict, Field, Strict, field_validator, model_validator

from cdse_stac.errors import SettingsError

# Oldest scene first; scenes taken at the same moment are ordered by id.
# A fixed order makes every run return the pages in the same order.
SORT_ORDER = [
    {"field": "properties.datetime", "direction": "asc"},
    {"field": "id", "direction": "asc"},
]

# The access token may only ever be sent to Copernicus Data Space hosts, whatever the settings file says.
COPERNICUS_DOMAIN = ".dataspace.copernicus.eu"
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"{name}{n}" for name in ("com", "lpt") for n in range(1, 10)}

Number = Annotated[float, Strict()]  # an int or a float; true, false and text are refused
Whole = Annotated[int, Strict()]


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def url_problem(url: object, allowed_hosts: tuple[str, ...]) -> str | None:
    """Why this address may not be called, or None when it is safe.

    The host is read twice: by Python's urlparse, and from the address exactly as requests
    prepares it for sending. Both readings must agree and be on the allow list. A backslash
    or a user part (@) can make the two readings differ, so either one is refused outright.
    Only the standard https port is allowed.
    """
    if not isinstance(url, str) or not url:
        return "the address is empty"
    if "\\" in url:
        return "the address contains a backslash"
    parsed = urlparse(url)
    if "@" in parsed.netloc:
        return "the address contains a user part (@)"
    if parsed.scheme != "https":
        return "the address does not use https"
    try:
        port = parsed.port
        prepared = urlparse(requests.Request("POST", url).prepare().url)
        prepared_port = prepared.port
    except (ValueError, requests.RequestException):
        return "the address cannot be read"
    if port not in (None, 443) or prepared_port not in (None, 443):
        return f"the address uses port {port or prepared_port}; only the standard https port is allowed"
    if parsed.hostname not in allowed_hosts or prepared.hostname != parsed.hostname:
        return f"host {parsed.hostname} is not in allowed_hosts"
    return None


class BBox(BaseModel):
    """A box on the map in WGS84 degrees, with each side named.

    Named sides make the order impossible to mix up in the config file.
    The STAC request still wants a plain list: west, south, east, north,
    which is longitude before latitude.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    west: Number = Field(ge=-180, le=180)
    south: Number = Field(ge=-90, le=90)
    east: Number = Field(ge=-180, le=180)
    north: Number = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def sides_in_order(self) -> BBox:
        if self.west >= self.east:
            raise ValueError(
                f"west ({self.west}) must be smaller than east ({self.east}). "
                "The server reads west > east as a box crossing the date line, "
                "which covers almost every longitude on Earth."
            )
        if self.south >= self.north:
            raise ValueError(f"south ({self.south}) must be smaller than north ({self.north})")
        return self

    def as_stac_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


class Query(BaseModel):
    """The question for the catalogue: which scenes cover this parcel?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Also used as a folder name, and stored in a 100-character column.
    parcel_id: str = Field(pattern=r"^[a-z0-9_]+$", max_length=100)
    # A STAC collection id such as sentinel-2-l2a; stored in a 100-character column.
    collection: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=100)
    bbox: BBox
    start: datetime
    end: datetime
    max_cloud_cover_pct: Number = Field(ge=0, le=100, allow_inf_nan=False)

    @field_validator("parcel_id")
    @classmethod
    def usable_as_folder_name(cls, value: str) -> str:
        if value in WINDOWS_RESERVED:
            raise ValueError(f"{value!r} is a reserved file name on Windows; choose another parcel_id")
        return value

    @field_validator("start", "end", mode="before")
    @classmethod
    def written_as_a_time(cls, value: Any) -> Any:
        # A plain number would be read as seconds since 1970, which is never what a settings file means.
        if not isinstance(value, (str, datetime)):
            raise ValueError("write the time as text with a time zone, for example 2026-04-01T00:00:00Z")
        return value

    @field_validator("start", "end")
    @classmethod
    def utc_with_time_zone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("needs a time zone, for example 2026-04-01T00:00:00Z")
        try:
            return value.astimezone(timezone.utc)
        except OverflowError:
            raise ValueError("is outside the range of dates that can be handled") from None

    @model_validator(mode="after")
    def window_runs_forward(self) -> Query:
        if self.start >= self.end:
            raise ValueError(f"start ({self.start}) must be before end ({self.end})")
        return self

    def stac_datetime(self) -> str:
        return f"{_utc_text(self.start)}/{_utc_text(self.end)}"

    def search_body(self, page_size: int) -> dict:
        """The JSON body for POST /search, in the shape the server accepted."""
        return {
            "collections": [self.collection],
            "bbox": self.bbox.as_stac_list(),
            "datetime": self.stac_datetime(),
            "limit": page_size,
            "sortby": [dict(rule) for rule in SORT_ORDER],
            "filter-lang": "cql2-json",
            "filter": {"op": "<=", "args": [{"property": "eo:cloud_cover"}, self.max_cloud_cover_pct]},
        }

    def fingerprint(self) -> str:
        """sha256 of the question, so the same question always gets the same id."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SourceSettings(BaseModel):
    """Where to ask, how politely, and how much is too much."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_url: str
    allowed_hosts: tuple[str, ...] = Field(min_length=1)
    # One page is one database transaction and is held in memory a few times over, so pages stay modest.
    page_size: Whole = Field(default=50, ge=1, le=1000)
    requests_per_second: Number = Field(default=1.0, ge=0.05, le=10, allow_inf_nan=False)
    connect_timeout_s: Number = Field(default=5.0, gt=0, le=300, allow_inf_nan=False)
    read_timeout_s: Number = Field(default=30.0, gt=0, le=300, allow_inf_nan=False)  # longest silence between bytes
    total_timeout_s: Number = Field(default=120.0, gt=0, le=3600, allow_inf_nan=False)  # longest time for one answer
    max_attempts: Whole = Field(default=5, ge=1, le=10)
    max_page_bytes: Whole = Field(default=50_000_000, ge=10_000, le=1_000_000_000)  # a real page of 50 scenes is 3.5 MB
    max_pages: Whole = Field(default=200, ge=1, le=10_000)  # safety net against endless next links
    max_box_deg: Number = Field(default=1.0, gt=0, le=10, allow_inf_nan=False)  # a parcel, not a country

    @field_validator("allowed_hosts")
    @classmethod
    def plain_copernicus_hosts(cls, hosts: tuple[str, ...]) -> tuple[str, ...]:
        clean = []
        for host in hosts:
            name = host.strip().lower().rstrip(".")
            if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", name):
                raise ValueError(
                    f"allowed_hosts entry {host!r} must be a plain host name such as "
                    "stac.dataspace.copernicus.eu, without https://, a port or a path"
                )
            if not name.endswith(COPERNICUS_DOMAIN):
                raise ValueError(
                    f"allowed_hosts entry {name!r} is not a Copernicus Data Space host; "
                    f"the access token may only go to *{COPERNICUS_DOMAIN}"
                )
            clean.append(name)
        return tuple(clean)

    @model_validator(mode="after")
    def url_is_allowed(self) -> SourceSettings:
        problem = url_problem(self.search_url, self.allowed_hosts)
        if problem:
            raise ValueError(f"search_url refused: {problem}")
        return self


class Settings(BaseModel):
    """Everything one run needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_dir: Path = Path("data/runs")
    source: SourceSettings
    query: Query

    @field_validator("runs_dir")
    @classmethod
    def plain_runs_dir(cls, value: Path) -> Path:
        value = value.expanduser()
        if ".." in value.parts:
            raise ValueError("runs_dir may not contain '..'; give the folder directly")
        return value

    @model_validator(mode="after")
    def box_is_a_parcel(self) -> Settings:
        box, limit = self.query.bbox, self.source.max_box_deg
        if box.east - box.west > limit or box.north - box.south > limit:
            raise ValueError(
                f"the box is {box.east - box.west:.3f} by {box.north - box.south:.3f} degrees, larger than "
                f"max_box_deg ({limit}); raise max_box_deg in [source] if a box this large is really meant"
            )
        return self


def load_settings(path: str | Path) -> Settings:
    """Read a TOML settings file and check every value."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise SettingsError(f"the settings file {path} does not exist") from None
    except OSError as error:
        raise SettingsError(f"the settings file {path} cannot be read: {error.strerror}") from None
    try:
        text = data.decode("utf-8-sig")  # a byte-order mark from a Windows editor is fine
    except UnicodeDecodeError:
        raise SettingsError(f"the settings file {path} is not UTF-8 text") from None
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"the settings file {path} cannot be read: {error}") from None
    return Settings.model_validate(values)
