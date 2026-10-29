"""Link every scene to every fetch that returned it, let the newest fetch win, and add checks.

- cdse_scene_fetch: which fetch returned which scene. Two overlapping parcels now each keep
  their scenes; before, a scene belonged only to the fetch that last changed it.
- cdse_scene.first_seen_at, source_fetched_at, last_seen_at: when those fetches started, so the
  newest fetch wins whatever order the logs are loaded in.
- cdse_scene.assets is dropped: it was a second copy of raw_item -> 'assets', the largest part of a scene.
- cdse_fetch_run.load_status (a load cut off halfway stays 'loading'), log_lines and log_last_seal
  (tell a resumed log from a changed one).
- cdse_reject.raw_record is SQL NULL, not the JSON value null, for a whole-page reject.
- CHECK constraints, so the table itself refuses values outside their ranges.

Revision ID: 0003_scene_fetch
Revises: 0002_last_seen
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_scene_fetch"
down_revision = "0002_last_seen"
branch_labels = None
depends_on = None

SCHEMA = "iris_staging"

SCENE_CHECKS = {
    "ck_cdse_scene_cloud_cover_pct": "cloud_cover_pct BETWEEN 0 AND 100",
    "ck_cdse_scene_snow_cover_pct": "snow_cover_pct BETWEEN 0 AND 100",
    "ck_cdse_scene_gsd_m": "gsd_m > 0",
    "ck_cdse_scene_sun_elevation_deg": "sun_elevation_deg BETWEEN -90 AND 90",
    "ck_cdse_scene_bbox": "bbox_west <= bbox_east AND bbox_south <= bbox_north "
    "AND bbox_west >= -180 AND bbox_east <= 180 AND bbox_south >= -90 AND bbox_north <= 90",
}
RUN_CHECKS = {
    "ck_cdse_fetch_run_fetch_status": "fetch_status IN ('complete', 'incomplete')",
    "ck_cdse_fetch_run_load_status": "load_status IN ('loading', 'loaded')",
    "ck_cdse_fetch_run_counts": "least(pages, scenes_seen, inserted, updated, unchanged, duplicates, rejected, "
    "damaged_lines) >= 0",
}


def upgrade() -> None:
    op.create_table(
        "cdse_scene_fetch",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fetch_id", sa.String(64), sa.ForeignKey(f"{SCHEMA}.cdse_fetch_run.fetch_id"), nullable=False),
        sa.Column("scene_id", sa.String(255), sa.ForeignKey(f"{SCHEMA}.cdse_scene.scene_id"), nullable=False),
        sa.Column("page", sa.Integer()),
        sa.Column("position", sa.Integer()),
        sa.UniqueConstraint("fetch_id", "scene_id", name="uq_cdse_scene_fetch_fetch_scene"),
        schema=SCHEMA,
    )
    op.create_index("idx_cdse_scene_fetch_scene_id", "cdse_scene_fetch", ["scene_id"], schema=SCHEMA)
    # Rebuild the links that are known: the first, the updating and the last fetch of every scene.
    op.execute(
        """
        INSERT INTO iris_staging.cdse_scene_fetch (fetch_id, scene_id)
        SELECT DISTINCT fetch_id, scene_id FROM (
            SELECT first_fetch_id AS fetch_id, scene_id FROM iris_staging.cdse_scene
            UNION SELECT updated_by_fetch_id, scene_id FROM iris_staging.cdse_scene
            UNION SELECT last_seen_fetch_id, scene_id FROM iris_staging.cdse_scene
        ) links
        """
    )

    for name in ("first_seen_at", "source_fetched_at", "last_seen_at"):
        op.add_column("cdse_scene", sa.Column(name, sa.DateTime(timezone=True)), schema=SCHEMA)
    # Each new time column takes the start time of the fetch its id column names.
    op.execute(
        "UPDATE iris_staging.cdse_scene s SET first_seen_at = coalesce(r.started_at, r.created_at) "
        "FROM iris_staging.cdse_fetch_run r WHERE r.fetch_id = s.first_fetch_id"
    )
    op.execute(
        "UPDATE iris_staging.cdse_scene s SET source_fetched_at = coalesce(r.started_at, r.created_at) "
        "FROM iris_staging.cdse_fetch_run r WHERE r.fetch_id = s.updated_by_fetch_id"
    )
    op.execute(
        "UPDATE iris_staging.cdse_scene s SET last_seen_at = coalesce(r.started_at, r.created_at) "
        "FROM iris_staging.cdse_fetch_run r WHERE r.fetch_id = s.last_seen_fetch_id"
    )
    for column in ("first_seen_at", "source_fetched_at", "last_seen_at"):
        op.alter_column("cdse_scene", column, nullable=False, schema=SCHEMA)
    op.drop_column("cdse_scene", "assets", schema=SCHEMA)

    op.add_column(
        "cdse_fetch_run",
        sa.Column("load_status", sa.String(20), nullable=False, server_default="loaded"),
        schema=SCHEMA,
    )
    op.alter_column("cdse_fetch_run", "load_status", server_default="loading", schema=SCHEMA)
    op.add_column("cdse_fetch_run", sa.Column("log_lines", sa.Integer()), schema=SCHEMA)
    op.add_column("cdse_fetch_run", sa.Column("log_last_seal", sa.String(64)), schema=SCHEMA)

    op.execute("UPDATE iris_staging.cdse_reject SET raw_record = NULL WHERE raw_record = 'null'::jsonb")

    for name, condition in SCENE_CHECKS.items():
        op.create_check_constraint(name, "cdse_scene", condition, schema=SCHEMA)
    for name, condition in RUN_CHECKS.items():
        op.create_check_constraint(name, "cdse_fetch_run", condition, schema=SCHEMA)
    op.create_check_constraint(
        "ck_cdse_reject_page_position", "cdse_reject", "page >= 1 AND position >= -1", schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint("ck_cdse_reject_page_position", "cdse_reject", schema=SCHEMA, type_="check")
    for name in RUN_CHECKS:
        op.drop_constraint(name, "cdse_fetch_run", schema=SCHEMA, type_="check")
    for name in SCENE_CHECKS:
        op.drop_constraint(name, "cdse_scene", schema=SCHEMA, type_="check")
    for name in ("log_last_seal", "log_lines", "load_status"):
        op.drop_column("cdse_fetch_run", name, schema=SCHEMA)
    op.add_column("cdse_scene", sa.Column("assets", postgresql.JSONB()), schema=SCHEMA)
    op.execute("UPDATE iris_staging.cdse_scene SET assets = raw_item -> 'assets'")
    op.alter_column("cdse_scene", "assets", nullable=False, schema=SCHEMA)
    for name in ("last_seen_at", "source_fetched_at", "first_seen_at"):
        op.drop_column("cdse_scene", name, schema=SCHEMA)
    op.drop_index("idx_cdse_scene_fetch_scene_id", table_name="cdse_scene_fetch", schema=SCHEMA)
    op.drop_table("cdse_scene_fetch", schema=SCHEMA)
