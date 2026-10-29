import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from cdse_stac import runlog
from cdse_stac.db import make_engine, url_from_parts

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# The tests empty every table they use, so the database must be named as a test database:
# "test" as a whole word, such as iris_test or test_iris. "contest" or "iris_latest" are refused.
TEST_DATABASE_NAME = re.compile(r"(^|_)test(_|$)")


def _no_database(reason: str):
    """Skip the database tests, or fail when REQUIRE_DB_TESTS=1 says they must run (as in CI)."""
    if os.environ.get("REQUIRE_DB_TESTS") == "1":
        pytest.fail(f"the database tests must run here, but {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _quick_log_writes(monkeypatch):
    """Skip the disk sync per log line in tests; test_runlog checks that real runs do sync."""
    monkeypatch.setattr(runlog, "DURABLE", False)


def test_database_url() -> str | None:
    """TEST_DATABASE_URL, or the client's own address (CDSE_DB_*) pointed at CDSE_TEST_DB_NAME."""
    if os.environ.get("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"]
    if not os.environ.get("CDSE_TEST_DB_NAME"):
        return None
    try:
        url = url_from_parts().set(database=os.environ["CDSE_TEST_DB_NAME"])
    except RuntimeError:
        return None
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def admin_engine():
    """The test database as its owner, for the few tests that must do what the client may not."""
    url = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if not url:
        pytest.skip("TEST_ADMIN_DATABASE_URL is not set; this test needs the owner of the test database")
    engine = make_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_engine():
    """The test database with the iris_staging tables built from scratch by Alembic."""
    url = test_database_url()
    if not url:
        _no_database("neither TEST_DATABASE_URL nor CDSE_TEST_DB_NAME is set; see .env.example and run: docker compose up -d db")
    if not TEST_DATABASE_NAME.search(make_url(url).database or ""):
        pytest.fail("TEST_DATABASE_URL must name a test database such as iris_test; tests empty its tables")
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except OperationalError as error:
        _no_database(f"the test database is not reachable ({str(error.orig).splitlines()[0]}); run: docker compose up -d db")
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url  # Alembic reads DATABASE_URL; point it at the test database only
    try:
        config = Config(str(ROOT / "alembic.ini"))
        command.downgrade(config, "base")  # every session starts from an empty schema,
        command.upgrade(config, "head")  # so all migrations, and their way back, run each time
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved
    yield engine
    engine.dispose()


@pytest.fixture
def db(test_engine):
    """An empty set of staging tables for each test."""
    with test_engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE iris_staging.cdse_reject, iris_staging.cdse_scene_fetch, iris_staging.cdse_scene, "
            "iris_staging.cdse_fetch_run"
        )
    return test_engine
