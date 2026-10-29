"""The staging tables. Every table of this project lives in the iris_staging schema.

iris_staging.cdse_fetch_run    one row per fetch log loaded: the question, counts, timings
iris_staging.cdse_scene        one row per scene, unique on the STAC item id
iris_staging.cdse_scene_fetch  which fetch returned which scene, so overlapping parcels each keep their scenes
iris_staging.cdse_reject       one row per refused record, with the reason

Nothing here points at iris_core. A test checks that, and the load step
refuses to run if any table points anywhere but iris_staging.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from uuid import uuid4

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cdse_stac.envfile import secret

STAGING_SCHEMA = "iris_staging"
DB_CONNECT_TIMEOUT_S = 10
DB_STATEMENT_TIMEOUT_MS = 60_000  # no single statement may run longer than a minute
DRIVER = "postgresql+psycopg"


class Base(DeclarativeBase):
    pass


class JsonText(str):
    """JSON that is already written out, so it is not serialised a second time on its way to JSONB."""


def _json_serializer(value: object) -> str:
    return value if isinstance(value, JsonText) else json.dumps(value)


class StagingEntity(Base):
    """Shared audit times, set by the database, in the style of the IRIS base entity.

    updated_at has no automatic onupdate: the load step sets it by hand, and only
    when a scene's content really changed, because SQLAlchemy's onupdate does not
    run for INSERT ... ON CONFLICT DO UPDATE.
    """

    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CdseFetchRun(StagingEntity):
    __tablename__ = "cdse_fetch_run"
    __table_args__ = (
        Index("idx_cdse_fetch_run_parcel_id", "parcel_id"),
        CheckConstraint("fetch_status IN ('complete', 'incomplete')", name="ck_cdse_fetch_run_fetch_status"),
        CheckConstraint("load_status IN ('loading', 'loaded')", name="ck_cdse_fetch_run_load_status"),
        CheckConstraint(
            "least(pages, scenes_seen, inserted, updated, unchanged, duplicates, rejected, damaged_lines) >= 0",
            name="ck_cdse_fetch_run_counts",
        ),
        {"schema": STAGING_SCHEMA},
    )

    fetch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parcel_id: Mapped[str] = mapped_column(String(100), nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[dict] = mapped_column(JSONB, nullable=False)
    parcel_bbox: Mapped[str] = mapped_column(Geometry("POLYGON", srid=4326, spatial_index=False), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_size: Mapped[int] = mapped_column(Integer, nullable=False)
    log_path: Mapped[str] = mapped_column(Text, nullable=False)
    log_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    log_lines: Mapped[int | None] = mapped_column(Integer)  # with log_last_seal: tells a resumed log from a changed one
    log_last_seal: Mapped[str | None] = mapped_column(String(64))
    fetch_status: Mapped[str] = mapped_column(String(20), nullable=False)  # complete or incomplete
    load_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="loading")  # loading or loaded
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages: Mapped[int] = mapped_column(Integer, nullable=False)
    scenes_seen: Mapped[int] = mapped_column(Integer, nullable=False)
    inserted: Mapped[int] = mapped_column(Integer, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, nullable=False)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False)
    damaged_lines: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_limited: Mapped[int | None] = mapped_column(Integer)
    load_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CdseScene(StagingEntity):
    __tablename__ = "cdse_scene"
    __table_args__ = (
        UniqueConstraint("scene_id", name="uq_cdse_scene_scene_id"),
        Index("idx_cdse_scene_acquired_at", "acquired_at"),
        Index("idx_cdse_scene_mgrs_tile", "mgrs_tile"),
        Index("idx_cdse_scene_footprint", "footprint", postgresql_using="gist"),
        Index("idx_cdse_scene_last_seen_fetch_id", "last_seen_fetch_id"),
        CheckConstraint("cloud_cover_pct BETWEEN 0 AND 100", name="ck_cdse_scene_cloud_cover_pct"),
        CheckConstraint("snow_cover_pct BETWEEN 0 AND 100", name="ck_cdse_scene_snow_cover_pct"),
        CheckConstraint("gsd_m > 0", name="ck_cdse_scene_gsd_m"),
        CheckConstraint("sun_elevation_deg BETWEEN -90 AND 90", name="ck_cdse_scene_sun_elevation_deg"),
        CheckConstraint(
            "bbox_west <= bbox_east AND bbox_south <= bbox_north "
            "AND bbox_west >= -180 AND bbox_east <= 180 AND bbox_south >= -90 AND bbox_north <= 90",
            name="ck_cdse_scene_bbox",
        ),
        {"schema": STAGING_SCHEMA},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    scene_id: Mapped[str] = mapped_column(String(255), nullable=False)
    collection: Mapped[str] = mapped_column(String(100), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(50))
    product_type: Mapped[str | None] = mapped_column(String(50))
    processing_baseline: Mapped[str | None] = mapped_column(String(20))
    mgrs_tile: Mapped[str | None] = mapped_column(String(10))
    relative_orbit: Mapped[int | None] = mapped_column(Integer)
    orbit_state: Mapped[str | None] = mapped_column(String(20))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cloud_cover_pct: Mapped[float] = mapped_column(Float, nullable=False)
    snow_cover_pct: Mapped[float | None] = mapped_column(Float)
    gsd_m: Mapped[float | None] = mapped_column(Float)
    sun_elevation_deg: Mapped[float | None] = mapped_column(Float)
    footprint: Mapped[str] = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False)
    bbox_west: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_south: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_east: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_north: Mapped[float] = mapped_column(Float, nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    raw_item: Mapped[dict] = mapped_column(JSONB, nullable=False)  # the scene exactly as received, assets included
    record_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    first_fetch_id: Mapped[str] = mapped_column(  # the earliest fetch that returned the scene
        String(64), ForeignKey(f"{STAGING_SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False
    )
    updated_by_fetch_id: Mapped[str] = mapped_column(  # the fetch whose content is stored
        String(64), ForeignKey(f"{STAGING_SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False
    )
    last_seen_fetch_id: Mapped[str] = mapped_column(  # the latest fetch that returned the scene at all
        String(64),
        ForeignKey(f"{STAGING_SCHEMA}.cdse_fetch_run.fetch_id", name="fk_cdse_scene_last_seen_fetch_id"),
        nullable=False,
    )
    # When each of those fetches started. The newest fetch wins, whatever order the logs are loaded in.
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CdseSceneFetch(Base):
    __tablename__ = "cdse_scene_fetch"
    __table_args__ = (
        UniqueConstraint("fetch_id", "scene_id", name="uq_cdse_scene_fetch_fetch_scene"),
        Index("idx_cdse_scene_fetch_scene_id", "scene_id"),
        {"schema": STAGING_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fetch_id: Mapped[str] = mapped_column(
        String(64), ForeignKey(f"{STAGING_SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False
    )
    scene_id: Mapped[str] = mapped_column(
        String(255), ForeignKey(f"{STAGING_SCHEMA}.cdse_scene.scene_id"), nullable=False
    )
    page: Mapped[int | None] = mapped_column(Integer)  # empty for links rebuilt by migration 0003
    position: Mapped[int | None] = mapped_column(Integer)


class CdseReject(StagingEntity):
    __tablename__ = "cdse_reject"
    __table_args__ = (
        UniqueConstraint("fetch_id", "page", "position", name="uq_cdse_reject_fetch_page_position"),
        Index("idx_cdse_reject_reason", "reason"),
        CheckConstraint("page >= 1 AND position >= -1", name="ck_cdse_reject_page_position"),
        {"schema": STAGING_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fetch_id: Mapped[str] = mapped_column(
        String(64), ForeignKey(f"{STAGING_SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False
    )
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # -1 means the whole page
    scene_id: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    raw_record: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))  # SQL NULL for a whole-page reject


def staging_schemas() -> set[str | None]:
    """Every schema any of our tables points at. Must be exactly {'iris_staging'}."""
    return {table.schema for table in Base.metadata.tables.values()}


def database_url(url: str | None = None) -> URL:
    """The database address, checked. Without DATABASE_URL it is built from the CDSE_DB_* parts.

    Building it from parts URL-encodes the password, so any character works in it; the password
    can also come from a file (CDSE_DB_PASSWORD_FILE). A plain postgresql:// address gets the psycopg
    driver this project installs. Errors never show the password.
    """
    text = url or os.environ.get("DATABASE_URL")
    if text:
        try:
            parsed = make_url(text)
        except (ArgumentError, ValueError):
            raise RuntimeError(
                "DATABASE_URL cannot be read; the form is postgresql+psycopg://user:password@host:port/database"
            ) from None
        if parsed.drivername in ("postgresql", "postgres"):
            parsed = parsed.set(drivername=DRIVER)
        if parsed.drivername != DRIVER:
            raise RuntimeError(f"DATABASE_URL uses {parsed.drivername}; this project needs {DRIVER}")
        return parsed
    return url_from_parts()


def url_from_parts() -> URL:
    """The client's address from CDSE_DB_USER, CDSE_DB_PASSWORD (or _FILE), CDSE_DB_HOST, _PORT and _NAME."""
    password = secret("CDSE_DB_PASSWORD")
    if not password:
        raise RuntimeError("neither DATABASE_URL nor CDSE_DB_PASSWORD is set; copy .env.example to .env")
    port = os.environ.get("CDSE_DB_PORT", "5434")
    if not port.isdigit():
        raise RuntimeError(f"CDSE_DB_PORT {port!r} is not a port number")
    return URL.create(
        DRIVER,
        username=os.environ.get("CDSE_DB_USER", "cdse_loader"),
        password=password,
        host=os.environ.get("CDSE_DB_HOST", "127.0.0.1"),
        port=int(port),
        database=os.environ.get("CDSE_DB_NAME", "iris"),
    )


def connect_args(url: URL) -> dict:
    """Time limits, UTC and a fixed search path, merged with any options already in the address."""
    ours = f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} -c TimeZone=UTC -c search_path={STAGING_SCHEMA},public"
    given = url.query.get("options")
    args = {"options": f"{given} {ours}" if given else ours}
    if "connect_timeout" not in url.query:
        args["connect_timeout"] = DB_CONNECT_TIMEOUT_S
    return args


def shared_database_problem(engine: Engine) -> tuple[str, str | None]:
    """The database name, and why this client should not write there, or None.

    A database that holds iris_core is a shared or production database. This client is meant
    for its own staging database, so it refuses unless the caller says writing there is intended.
    """
    with engine.connect() as connection:
        name = connection.exec_driver_sql("SELECT current_database()").scalar()
        core = connection.exec_driver_sql(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'iris_core')"  # visible to every role
        ).scalar()
    if core:
        return name, (
            f"database {name} holds an iris_core schema, so it looks like a shared or production database; "
            "add --allow-shared-database only if creating and writing iris_staging there is intended"
        )
    return name, None


def make_engine(url: str | None = None) -> Engine:
    """A database connection with time limits for connecting and for each statement."""
    parsed = database_url(url)
    return create_engine(
        parsed,
        pool_pre_ping=True,
        connect_args=connect_args(parsed),
        json_serializer=_json_serializer,
    )
