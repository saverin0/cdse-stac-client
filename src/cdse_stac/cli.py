"""Command line: python -m cdse_stac migrate | fetch | load | validate | run | cleanup.

Every expected failure ends as one clear line and an exit code: 2 for settings that cannot be
used, 1 for everything else. The heavy libraries are only loaded by the commands that need them.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from cdse_stac.envfile import secret
from cdse_stac.errors import DatabaseRefused, FetchError, LoadError, SettingsError
from cdse_stac.logs import resolve_level, run_logger, setup_logging

DEFAULT_CONFIG = Path("config/bornheim.toml")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def migrate(engine) -> str:
    """Create or update the iris_staging tables. Safe to run again: Alembic skips what is done.

    Returns the migration the schema is at afterwards.
    """
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    with engine.connect() as connection:
        return connection.exec_driver_sql("SELECT version_num FROM iris_staging.alembic_version").scalar()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cdse_stac",
        description="Copernicus Data Space STAC scene search for parcel monitoring, staged in PostGIS.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    shared = "write even to a database that holds iris_core (refused by default)"

    migrate_command = commands.add_parser("migrate", help="create or update the iris_staging tables (Alembic upgrade head)")
    migrate_command.add_argument("--allow-shared-database", action="store_true", help=shared)

    fetch_command = commands.add_parser("fetch", help="ask the catalogue and write every page to a log (no database)")
    fetch_command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    fetch_command.add_argument("--resume", type=Path, metavar="RUN_DIR", help="go on with an unfinished run")

    load_command = commands.add_parser("load", help="read a fetch log and write iris_staging (no network)")
    load_command.add_argument("--run", type=Path, required=True, metavar="RUN_DIR")
    load_command.add_argument("--force", action="store_true", help="load a log that changed since it was loaded before")
    load_command.add_argument("--allow-shared-database", action="store_true", help=shared)

    validate_command = commands.add_parser("validate", help="run the SQL checks and the coverage summary (read-only)")
    validate_command.add_argument("--out", type=Path, help="also write the report to this file")

    run_command = commands.add_parser("run", help="migrate, fetch, load, then validate")
    run_command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    run_command.add_argument("--out", type=Path, help="also write the validation report to this file")
    run_command.add_argument("--allow-shared-database", action="store_true", help=shared)

    cleanup_command = commands.add_parser("cleanup", help="remove old finished run folders, keeping the newest per parcel")
    cleanup_command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    cleanup_command.add_argument("--keep", type=int, default=5, help="runs to keep per parcel (default 5)")
    cleanup_command.add_argument("--yes", action="store_true", help="really remove; without it the folders are only listed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(Path.cwd() / ".env")  # only the .env of the folder you run from; real environment variables win
    level, note = resolve_level(os.environ.get("LOG_LEVEL"))
    setup_logging(level)
    log = run_logger("cdse_stac.cli")
    if note:
        log.warning("%s", note)
    try:
        return _run(args, log)
    except Exception as error:  # one clear line for every failure we expect; anything else stays a traceback
        explained = _explain(error)
        if explained is None:
            raise
        code, message = explained
        log.error("%s", message)
        return code


def _explain(error: Exception) -> tuple[int, str] | None:
    from pydantic import ValidationError

    if isinstance(error, ValidationError):
        return 2, f"the settings file is not valid:\n{error}"
    if isinstance(error, SettingsError):
        return 2, str(error)
    if isinstance(error, (FetchError, LoadError, DatabaseRefused, RuntimeError)):  # RuntimeError: DATABASE_URL and the like
        return 1, str(error)
    if isinstance(error, OSError):
        return 1, f"a file could not be read or written: {error}"
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    if isinstance(error, SQLAlchemyError):
        cause = getattr(error, "orig", None) or error
        first_line = (str(cause).strip().splitlines() or [type(error).__name__])[0]  # never holds the password
        hint = " (is the database running? docker compose up -d db)" if isinstance(error, OperationalError) else ""
        return 1, f"database problem: {type(error).__name__}: {first_line}{hint}"
    return None


def _guard(engine, allowed: bool, log) -> None:
    from cdse_stac.db import shared_database_problem

    name, problem = shared_database_problem(engine)
    log.info("database %s", name)
    if problem and not allowed:
        raise DatabaseRefused(problem)


def _run(args, log) -> int:
    if args.command == "cleanup":
        from cdse_stac.cleanup import clean_up
        from cdse_stac.config import load_settings

        if args.keep < 0:
            raise SettingsError("--keep must be 0 or more")  # a negative slice would pick the newest runs
        runs = clean_up(Path(load_settings(args.config).runs_dir), args.keep, args.yes)
        for run in runs:
            print(("removed " if args.yes else "would remove ") + str(run))
        if not args.yes and runs:
            print("nothing removed; add --yes to remove these folders")
        return 0

    engine = None
    try:
        if args.command in ("migrate", "run"):
            from cdse_stac.db import make_engine

            engine = make_engine()
            _guard(engine, args.allow_shared_database, log)
            log.info("iris_staging is at migration %s", migrate(engine))
            if args.command == "migrate":
                return 0
        if args.command in ("fetch", "run"):
            from cdse_stac.client import token_problem
            from cdse_stac.config import load_settings
            from cdse_stac.fetch import run_fetch

            settings = load_settings(args.config)
            token = secret("CDSE_ACCESS_TOKEN")  # or from CDSE_ACCESS_TOKEN_FILE, as Docker secrets give it
            if token and token_problem(token):
                raise SettingsError(f"CDSE_ACCESS_TOKEN cannot be used: {token_problem(token)}; it was not sent anywhere")
            result = run_fetch(settings, token=token, resume_dir=getattr(args, "resume", None))
            print(result.run_dir)
            if args.command == "fetch":
                return 0
            args.run, args.force = result.run_dir, False
        if args.command in ("load", "run"):
            from cdse_stac.db import make_engine
            from cdse_stac.load import load_log

            engine = engine or make_engine()
            if args.command == "load":
                _guard(engine, args.allow_shared_database, log)
            stats = load_log(engine, args.run / "fetch.jsonl", force=args.force)
            print(json.dumps(stats.as_dict()))
            if args.command == "load":
                return 0
        from cdse_stac.db import make_engine
        from cdse_stac.validate import coverage, report, run_checks

        engine = engine or make_engine()
        checks = run_checks(engine)
        text = report(checks, coverage(engine))
        print(text)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
        return 0 if all(check.passed for check in checks) else 1
    finally:
        if engine is not None:
            engine.dispose()
