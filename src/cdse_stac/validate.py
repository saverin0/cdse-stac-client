"""Run the SQL checks in sql/validation.sql and the coverage summary in sql/coverage.sql.

Both run inside a read-only transaction, so these SQL files can never change the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from cdse_stac.jsonsafe import printable

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _read_only(engine: Engine, sql: str) -> list:
    with engine.connect() as connection:
        connection.execution_options(postgresql_readonly=True)  # the database refuses any write
        rows = connection.execute(text(sql)).all()
        connection.rollback()
    return rows


def run_checks(engine: Engine) -> list[Check]:
    rows = _read_only(engine, (SQL_DIR / "validation.sql").read_text(encoding="utf-8"))
    return [Check(row.check_name, bool(row.passed), row.detail or "") for row in rows]


def coverage(engine: Engine) -> list[dict]:
    rows = _read_only(engine, (SQL_DIR / "coverage.sql").read_text(encoding="utf-8"))
    return [dict(row._mapping) for row in rows]


def report(checks: list[Check], months: list[dict]) -> str:
    """Plain-text report: the checks first, then coverage per month. Text from the scenes is cleaned."""
    lines = ["Validation checks"]
    for check in checks:
        lines.append(f"  [{'PASS' if check.passed else 'FAIL'}] {check.name} ({printable(check.detail, 600)})")
    lines.append("")
    lines.append("Coverage per parcel and month")
    header = ["parcel_id", "month", "scenes", "days_with_a_scene", "tiles", "satellites",
              "min_cloud_pct", "mean_cloud_pct", "partial_cover"]
    table = [header] + [[printable(str(row[name]), 120) for name in header] for row in months]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        lines.append("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)))
    return "\n".join(lines)
