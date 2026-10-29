"""Add last_seen_fetch_id: the latest fetch whose answer contained each scene.

updated_by_fetch_id says which fetch last changed a scene. last_seen_fetch_id says
which fetch last returned it, changed or not. A scene whose last_seen_fetch_id is
older than the newest fetch of its parcel was not returned again, for example
because the catalogue withdrew it.

Revision ID: 0002_last_seen
Revises: 0001_iris_staging
Create Date: 2026-10-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_last_seen"
down_revision = "0001_iris_staging"
branch_labels = None
depends_on = None

SCHEMA = "iris_staging"


def upgrade() -> None:
    op.add_column("cdse_scene", sa.Column("last_seen_fetch_id", sa.String(64), nullable=True), schema=SCHEMA)
    op.execute("UPDATE iris_staging.cdse_scene SET last_seen_fetch_id = updated_by_fetch_id")
    op.alter_column("cdse_scene", "last_seen_fetch_id", nullable=False, schema=SCHEMA)
    op.create_foreign_key(
        "fk_cdse_scene_last_seen_fetch_id",
        "cdse_scene",
        "cdse_fetch_run",
        ["last_seen_fetch_id"],
        ["fetch_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )
    op.create_index("idx_cdse_scene_last_seen_fetch_id", "cdse_scene", ["last_seen_fetch_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("idx_cdse_scene_last_seen_fetch_id", table_name="cdse_scene", schema=SCHEMA)
    op.drop_constraint("fk_cdse_scene_last_seen_fetch_id", "cdse_scene", schema=SCHEMA, type_="foreignkey")
    op.drop_column("cdse_scene", "last_seen_fetch_id", schema=SCHEMA)
