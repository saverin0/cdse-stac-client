import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import responses
from sqlalchemy import text

from cdse_stac.cli import main
from cdse_stac.db import database_url
from cdse_stac.logs import resolve_level

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "bornheim.toml"
SECRET = "p@ss:w/rd#%"


def screen(capsys):
    """What the program printed to the screen so far, including its log lines (they go to stderr)."""
    captured = capsys.readouterr()
    return captured.out + captured.err


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in ("DATABASE_URL", "CDSE_ACCESS_TOKEN", "CDSE_ACCESS_TOKEN_FILE", "LOG_LEVEL", "CDSE_DB_PASSWORD",
                 "CDSE_DB_PASSWORD_FILE", "CDSE_DB_HOST", "CDSE_DB_PORT", "CDSE_DB_USER", "CDSE_DB_NAME"):
        monkeypatch.delenv(name, raising=False)


def test_missing_settings_file_is_one_line_and_exit_2(tmp_path, capsys):
    assert main(["fetch", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "nope.toml does not exist" in screen(capsys)


def test_broken_settings_file_is_one_line_and_exit_2(tmp_path, capsys):
    path = tmp_path / "broken.toml"
    path.write_text("north =\n", encoding="utf-8")
    assert main(["fetch", "--config", str(path)]) == 2
    assert "cannot be read" in screen(capsys)


def test_load_of_a_missing_run_folder_is_one_line_and_exit_1(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://iris:x@127.0.0.1:1/iris")
    assert main(["load", "--run", str(tmp_path / "nope")]) == 1
    out = screen(capsys)
    assert "does not exist" in out or "database problem" in out


@responses.activate
def test_token_with_a_line_end_is_refused_before_any_request(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CDSE_ACCESS_TOKEN", "abc\ndef")
    assert main(["fetch", "--config", str(EXAMPLE)]) == 2
    assert len(responses.calls) == 0
    out = screen(capsys)
    assert "abc" not in out and "line ends" in out


def test_database_error_shows_its_reason_but_not_the_password(capsys, monkeypatch):
    # connect_timeout in the address is kept (it used to be replaced), so this waits 1 s, not 10.
    url = f"postgresql+psycopg://iris:{SECRET.replace('@', '%40')}@127.0.0.1:1/iris?connect_timeout=1"
    monkeypatch.setenv("DATABASE_URL", url)
    assert main(["validate"]) == 1
    out = screen(capsys)
    assert "database problem: OperationalError: connection" in out  # the reason, not only the class name
    assert "is the database running" in out
    assert "w/rd" not in out


def test_unreadable_database_url_says_so_without_the_password(monkeypatch):
    with pytest.raises(RuntimeError) as caught:
        database_url("postgresql+psycopg://iris:secret-pw@127.0.0.1:notaport/iris")
    assert "cannot be read" in str(caught.value) and "secret-pw" not in str(caught.value)


def test_plain_postgresql_url_gets_the_installed_driver():
    assert database_url("postgresql://iris:x@127.0.0.1:5434/iris").drivername == "postgresql+psycopg"


def test_other_drivers_are_refused():
    with pytest.raises(RuntimeError, match="needs postgresql\\+psycopg"):
        database_url("mysql://iris:x@127.0.0.1/iris")


def test_any_password_works_when_the_address_is_built_from_parts(monkeypatch, tmp_path):
    monkeypatch.setenv("CDSE_DB_PASSWORD", SECRET)
    url = database_url()
    assert url.password == SECRET and url.host == "127.0.0.1"
    secret_file = tmp_path / "password"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")
    monkeypatch.delenv("CDSE_DB_PASSWORD")
    monkeypatch.setenv("CDSE_DB_PASSWORD_FILE", str(secret_file))
    assert database_url().password == SECRET


@pytest.mark.parametrize(
    "value, level, note",
    [
        ("debug", logging.DEBUG, None),
        (" Warning ", logging.WARNING, None),
        ("10", logging.DEBUG, None),
        ("", logging.INFO, None),
        ("verbose", logging.INFO, "not a level name"),
        ("CRITICAL", logging.ERROR, "hide every error"),
    ],
)
def test_log_level_is_read_forgivingly_and_never_hides_errors(value, level, note):
    resolved, message = resolve_level(value)
    assert resolved == level
    assert (note is None and message is None) or note in message


def test_only_the_env_file_of_the_current_folder_is_read(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CDSE_PARENT_MARKER=1\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("CDSE_PARENT_MARKER", raising=False)
    main(["cleanup", "--config", str(EXAMPLE)])
    assert "CDSE_PARENT_MARKER" not in os.environ


def test_help_does_not_load_the_heavy_libraries():
    code = "import sys, cdse_stac.cli; print(sorted(m for m in ('shapely', 'sqlalchemy', 'requests') if m in sys.modules))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.stdout.strip() == "[]"


def make_run(parcel_dir, name, status):
    run = parcel_dir / name
    run.mkdir(parents=True)
    (run / "status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    return run


def test_cleanup_lists_then_removes_only_old_finished_runs(tmp_path, capsys):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE.read_text(encoding="utf-8").replace('runs_dir = "data/runs"',
                                                                  f'runs_dir = "{(tmp_path / "runs").as_posix()}"'),
                      encoding="utf-8")
    parcel = tmp_path / "runs" / "nw_bornheim_core"
    old = make_run(parcel, "20261001T000000Z_aaaaaaaa", "complete")
    failed = make_run(parcel, "20261002T000000Z_aaaaaaaa", "failed")
    newest = make_run(parcel, "20261003T000000Z_aaaaaaaa", "complete")
    assert main(["cleanup", "--config", str(config), "--keep", "1"]) == 0
    assert "would remove" in capsys.readouterr().out and old.exists()
    assert main(["cleanup", "--config", str(config), "--keep", "1", "--yes"]) == 0
    assert not old.exists() and failed.exists() and newest.exists()


@pytest.mark.db
def test_migrate_says_which_revision_the_schema_is_at(db, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.url.render_as_string(hide_password=False))
    assert main(["migrate"]) == 0
    assert "iris_staging is at migration 0003_scene_fetch" in screen(capsys)


@pytest.mark.db
def test_migrate_refuses_a_shared_database_unless_told(db, admin_engine, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.url.render_as_string(hide_password=False))
    with admin_engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA iris_core"))
    try:
        assert main(["migrate"]) == 1
        assert "iris_core" in screen(capsys)
        assert main(["migrate", "--allow-shared-database"]) == 0
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text("DROP SCHEMA iris_core"))


def test_cleanup_refuses_a_negative_keep(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE.read_text(encoding="utf-8").replace('runs_dir = "data/runs"',
                                                                  f'runs_dir = "{(tmp_path / "runs").as_posix()}"'),
                      encoding="utf-8")
    parcel = tmp_path / "runs" / "nw_bornheim_core"
    runs = [make_run(parcel, f"2026100{n}T000000Z_aaaaaaaa", "complete") for n in (1, 2)]
    assert main(["cleanup", "--config", str(config), "--keep", "-3", "--yes"]) == 2
    assert all(run.exists() for run in runs)


def test_secret_file_keeps_spaces_and_drops_only_the_line_end(tmp_path, monkeypatch):
    from cdse_stac.envfile import secret

    path = tmp_path / "password"
    path.write_text(" pass word \r\n", encoding="utf-8", newline="")
    monkeypatch.setenv("CDSE_DB_PASSWORD_FILE", str(path))
    assert secret("CDSE_DB_PASSWORD") == " pass word "
