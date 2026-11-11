"""Compare REN Data Hub and ENTSO-E data for Portugal (energy per day and component).

The correspondence between the two sources lives in ``sql/pt_component_map.sql`` and the
query in ``sql/compare_ren_entsoe_pt.sql``; this module only runs them.

Usage:
    uv run python -m sen_observatory.comparison
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

SQL_DIR = Path(__file__).parent / "sql"
DEFAULT_DB_PATH = Path("data/sen.duckdb")


def read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def load_component_map(con: duckdb.DuckDBPyConnection) -> None:
    """Create the temp table ``component_map`` on this connection."""
    con.execute(read_sql("pt_component_map.sql"))


def run_comparison(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    con.execute("SET TimeZone = 'UTC'")
    load_component_map(con)
    return con.execute(read_sql("compare_ren_entsoe_pt.sql")).df()


def check_mapping(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return problems that mean the component mapping needs a review.

    * a category excluded as "zero" has non-zero ENTSO-E values on the hours REN covers;
    * a Portuguese generation/consumption variable in ENTSO-E is not in the mapping at all.
    """
    load_component_map(con)
    problems: list[str] = []

    nonzero = con.execute(
        """
        SELECT m.entsoe_variable, round(sum(abs(e.value)), 3)
        FROM entsoe_timeseries e
        JOIN component_map m ON m.entsoe_variable = e.variable AND m.entsoe_country = e.country
        WHERE m.status = 'excluded_zero'
          AND date_trunc('hour', e.ts_utc) IN (SELECT date_trunc('hour', ts_utc) FROM ren_generation)
        GROUP BY 1
        HAVING sum(abs(e.value)) > 0
        ORDER BY 1
        """
    ).fetchall()
    for variable, total in nonzero:
        problems.append(
            f"{variable} was excluded as zero but has non-zero values (sum |value| = {total}); "
            "review pt_component_map.sql"
        )

    unmapped = con.execute(
        """
        SELECT DISTINCT variable FROM entsoe_timeseries
        WHERE country = 'PT'
          AND (starts_with(variable, 'generation_') OR starts_with(variable, 'consumption_'))
          AND variable NOT IN (SELECT entsoe_variable FROM component_map WHERE entsoe_variable IS NOT NULL)
        ORDER BY 1
        """
    ).fetchall()
    for (variable,) in unmapped:
        problems.append(f"{variable} is in ENTSO-E (PT) but not in pt_component_map.sql; add it")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    con = duckdb.connect(str(args.db), read_only=True)
    try:
        df = run_comparison(con)
        problems = check_mapping(con)
    finally:
        con.close()
    for problem in problems:
        print(f"WARNING: {problem}", file=sys.stderr)
    with pd.option_context("display.width", 200, "display.max_rows", None):
        for (day, period), g in df.groupby(["local_day", "period"], sort=False):
            print(f"\n== {day}  {period}")
            print(g.drop(columns=["local_day", "period"]).to_string(index=False))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
