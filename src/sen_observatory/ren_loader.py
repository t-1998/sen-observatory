"""Load REN Data Hub generation-mix CSV files into DuckDB.

Usage:
    uv run python -m sen_observatory.ren_loader data/raw/ren/*.csv
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_DB_PATH = Path("data/sen.duckdb")
TABLE = "ren_generation"
LOCAL_TZ = "Europe/Lisbon"
BALANCE_THRESHOLD_MW = 50.0

# Portuguese column names -> English snake_case.
# "Gás natural - Cogeração" is deliberately absent: it duplicates the CCGT
# column (see "Known issues" in SOURCES.md), so it is dropped on load.
COLUMN_MAP = {
    "Data e Hora": "ts_local",
    "Hídrica": "hydro",
    "Eólica": "wind",
    "Solar": "solar",
    "Biomassa": "biomass",
    "Ondas": "wave",
    "Gás Natural - Ciclo Combinado": "natural_gas",
    "Carvão": "coal",
    "Outra Térmica": "other_thermal",
    "Importação": "imports",
    "Exportação": "exports",
    "Bombagem": "pumping",
    "Injeção de Baterias": "battery_discharge",
    "Consumo Baterias": "battery_charge",
    "Consumo": "consumption",
}

GENERATION_COLUMNS = [
    "hydro",
    "wind",
    "solar",
    "biomass",
    "wave",
    "natural_gas",
    "coal",
    "other_thermal",
    "battery_discharge",
]

VALUE_COLUMNS = [c for c in COLUMN_MAP.values() if c != "ts_local"]
OUTPUT_COLUMNS = ["ts_utc", *VALUE_COLUMNS, "balance_residual_mw", "source_file"]


def read_ren_csv(path: Path) -> pd.DataFrame:
    """Read one REN CSV and return a cleaned frame with UTC timestamps."""
    # utf-8-sig strips the BOM that REN puts at the start of the file.
    raw = pd.read_csv(path, skiprows=2, encoding="utf-8-sig")
    raw.columns = [c.strip() for c in raw.columns]

    missing = set(COLUMN_MAP) - set(raw.columns)
    if missing:
        raise ValueError(f"{path.name}: missing expected columns: {sorted(missing)}")

    df = raw[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)

    # Local wall-clock time (interval start) -> UTC. "infer" resolves the
    # repeated hour on the autumn DST change from row order; the skipped hour
    # in spring simply does not occur in the data.
    local = pd.to_datetime(df.pop("ts_local"))
    try:
        ts_utc = local.dt.tz_localize(LOCAL_TZ, ambiguous="infer").dt.tz_convert("UTC")
    except ValueError as exc:
        raise ValueError(
            f"{path.name}: cannot resolve local timestamps to UTC "
            f"(rows out of chronological order?): {exc}"
        ) from exc

    # "infer" relies on row order, so a shuffled or reversed file would be
    # silently mis-converted. Fail loudly instead.
    if not ts_utc.is_monotonic_increasing or ts_utc.duplicated().any():
        raise ValueError(
            f"{path.name}: rows are not in strictly increasing chronological order "
            "(or contain duplicate timestamps); refusing to load."
        )
    df.insert(0, "ts_utc", ts_utc)

    df["balance_residual_mw"] = (
        df[GENERATION_COLUMNS].sum(axis=1)
        + df["imports"]
        - df["exports"]
        - df["pumping"]
        - df["battery_charge"]
        - df["consumption"]
    )
    df["source_file"] = path.name
    return df[OUTPUT_COLUMNS]


def check_balance(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Warn about rows whose energy-balance residual exceeds the threshold."""
    bad = df[df["balance_residual_mw"].abs() > BALANCE_THRESHOLD_MW]
    if not bad.empty:
        worst = bad["balance_residual_mw"].abs().idxmax()
        warnings.warn(
            f"{name}: {len(bad)} row(s) with |balance residual| > "
            f"{BALANCE_THRESHOLD_MW:.0f} MW (worst {df.loc[worst, 'balance_residual_mw']:.1f} MW "
            f"at {df.loc[worst, 'ts_utc']})",
            stacklevel=2,
        )
    return bad


def ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    value_defs = ", ".join(f"{c} DOUBLE" for c in VALUE_COLUMNS)
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            ts_utc TIMESTAMPTZ PRIMARY KEY,
            {value_defs},
            balance_residual_mw DOUBLE,
            source_file VARCHAR
        )
        """
    )


def upsert(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> None:
    """Insert rows, replacing any existing row with the same ts_utc."""
    con.register("incoming", df)
    try:
        cols = ", ".join(OUTPUT_COLUMNS)
        con.execute(f"INSERT OR REPLACE INTO {TABLE} ({cols}) SELECT {cols} FROM incoming")
    finally:
        con.unregister("incoming")


def load_files(paths: list[Path], db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("SET TimeZone = 'UTC'")
        ensure_table(con)
        for path in paths:
            df = read_ren_csv(path)
            bad = check_balance(df, path.name)
            upsert(con, df)
            res = df["balance_residual_mw"]
            print(
                f"{path.name}: {len(df)} rows | "
                f"{df['ts_utc'].min():%Y-%m-%d %H:%M} -> {df['ts_utc'].max():%Y-%m-%d %H:%M} UTC | "
                f"residual mean {res.mean():.1f} MW, max |{res.abs().max():.1f}| MW | "
                f"{len(bad)} row(s) > {BALANCE_THRESHOLD_MW:.0f} MW"
            )
        total = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        print(f"Table {TABLE} in {db_path}: {total} rows in total")
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path, help="REN CSV files to load")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="DuckDB file path")
    args = parser.parse_args(argv)
    load_files(args.files, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
