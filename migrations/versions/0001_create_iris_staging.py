"""Create the iris_staging schema and the three Copernicus tables.

Revision ID: 0001_iris_staging
Revises:
Create Date: 2026-10-28
"""

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_iris_staging"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "iris_staging"


def _audit_columns():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    # Installing PostGIS would write into the public schema, so the migration only checks for it.
    # The PostGIS image installs it when the database is created.
    if op.get_bind().exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname = 'postgis'").scalar() is None:
        raise RuntimeError("PostGIS is not installed in this database; install it first (CREATE EXTENSION postgis)")
    # env.py has already made sure the schema exists; the client's role may not create schemas.

    op.create_table(
        "cdse_fetch_run",
        sa.Column("fetch_id", sa.String(64), primary_key=True),
        sa.Column("parcel_id", sa.String(100), nullable=False),
        sa.Column("query_fingerprint", sa.String(64), nullable=False),
        sa.Column("query", postgresql.JSONB(), nullable=False),
        sa.Column(
            "parcel_bbox",
            geoalchemy2.types.Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("log_path", sa.Text(), nullable=False),
        sa.Column("log_sha256", sa.String(64), nullable=False),
        sa.Column("fetch_status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("scenes_seen", sa.Integer(), nullable=False),
        sa.Column("inserted", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("unchanged", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("damaged_lines", sa.Integer(), nullable=False),
        sa.Column("rate_limited", sa.Integer()),
        sa.Column("load_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        *_audit_columns(),
        schema=SCHEMA,
    )
    op.create_index("idx_cdse_fetch_run_parcel_id", "cdse_fetch_run", ["parcel_id"], schema=SCHEMA)

    op.create_table(
        "cdse_scene",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scene_id", sa.String(255), nullable=False),
        sa.Column("collection", sa.String(100), nullable=False),
        sa.Column("platform", sa.String(50)),
        sa.Column("product_type", sa.String(50)),
        sa.Column("processing_baseline", sa.String(20)),
        sa.Column("mgrs_tile", sa.String(10)),
        sa.Column("relative_orbit", sa.Integer()),
        sa.Column("orbit_state", sa.String(20)),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("cloud_cover_pct", sa.Float(), nullable=False),
        sa.Column("snow_cover_pct", sa.Float()),
        sa.Column("gsd_m", sa.Float()),
        sa.Column("sun_elevation_deg", sa.Float()),
        sa.Column(
            "footprint",
            geoalchemy2.types.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("bbox_west", sa.Float(), nullable=False),
        sa.Column("bbox_south", sa.Float(), nullable=False),
        sa.Column("bbox_east", sa.Float(), nullable=False),
        sa.Column("bbox_north", sa.Float(), nullable=False),
        sa.Column("thumbnail_url", sa.Text()),
        sa.Column("assets", postgresql.JSONB(), nullable=False),
        sa.Column("raw_item", postgresql.JSONB(), nullable=False),
        sa.Column("record_sha256", sa.String(64), nullable=False),
        sa.Column("first_fetch_id", sa.String(64), sa.ForeignKey(f"{SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False),
        sa.Column(
            "updated_by_fetch_id", sa.String(64), sa.ForeignKey(f"{SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False
        ),
        *_audit_columns(),
        sa.UniqueConstraint("scene_id", name="uq_cdse_scene_scene_id"),
        schema=SCHEMA,
    )
    op.create_index("idx_cdse_scene_acquired_at", "cdse_scene", ["acquired_at"], schema=SCHEMA)
    op.create_index("idx_cdse_scene_mgrs_tile", "cdse_scene", ["mgrs_tile"], schema=SCHEMA)
    op.create_index("idx_cdse_scene_footprint", "cdse_scene", ["footprint"], schema=SCHEMA, postgresql_using="gist")

    op.create_table(
        "cdse_reject",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fetch_id", sa.String(64), sa.ForeignKey(f"{SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("scene_id", sa.String(255)),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("raw_record", postgresql.JSONB()),
        *_audit_columns(),
        sa.UniqueConstraint("fetch_id", "page", "position", name="uq_cdse_reject_fetch_page_position"),
        schema=SCHEMA,
    )
    op.create_index("idx_cdse_reject_reason", "cdse_reject", ["reason"], schema=SCHEMA)


def downgrade() -> None:
    # The schema itself stays: Alembic's version table lives in it.
    op.drop_table("cdse_reject", schema=SCHEMA)
    op.drop_table("cdse_scene", schema=SCHEMA)
    op.drop_table("cdse_fetch_run", schema=SCHEMA)
