"""Alembic setup. It only ever sees and touches the iris_staging schema."""

from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import text

from cdse_stac.db import STAGING_SCHEMA, Base, make_engine

load_dotenv(Path.cwd() / ".env")  # only the .env of the folder you run from; real environment variables win

target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    """Look only at iris_staging. Every other schema is ignored."""
    if type_ == "schema":
        return name == STAGING_SCHEMA
    return True


def run_migrations_online() -> None:
    connectable = make_engine()  # the same time limits and search path as the client
    try:
        with connectable.connect() as connection:
            # Alembic's own version table lives in iris_staging too, so nothing lands in public.
            # The schema is only created when it is missing: the client's own role may use it but
            # may not create schemas, and PostgreSQL checks that right even for IF NOT EXISTS.
            exists = connection.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": STAGING_SCHEMA}
            ).scalar()
            if not exists:
                connection.exec_driver_sql(f"CREATE SCHEMA {STAGING_SCHEMA}")
            connection.commit()
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_schemas=True,
                include_name=include_name,
                version_table_schema=STAGING_SCHEMA,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline SQL generation is not supported; run against a database")
run_migrations_online()
