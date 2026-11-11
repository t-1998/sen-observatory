"""Tests for sen_observatory.ren_loader.

Most tests use small synthetic CSVs built in a temporary directory, so they
run anywhere (``data/`` is git-ignored). A few extra tests run against the real
files in ``data/raw/ren/`` and are skipped when those files are absent.
"""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from sen_observatory import ren_loader

LISBON = ZoneInfo("Europe/Lisbon")
HEADER = (
    "Data e Hora,Hídrica,Eólica,Solar,Biomassa,Ondas,Gás Natural - Ciclo Combinado,"
    "Gás natural - Cogeração,Carvão,Outra Térmica,Importação,Exportação,Bombagem,"
    "Injeção de Baterias,Consumo Baterias,Consumo"
)
REAL_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "ren"


def make_csv(
    path: Path,
    local_day: str,
    *,
    shuffle: bool = False,
    consumption_offset: float = 0.0,
) -> Path:
    """Write a REN-style CSV covering one full local day.

    Timestamps are produced by walking UTC in 15-min steps and printing the
    Lisbon wall-clock time, exactly like REN does (no DST marker). Values are
    balanced so the residual equals ``consumption_offset`` (negated).
    """
    start_local = datetime.fromisoformat(local_day).replace(tzinfo=LISBON)
    end_local = (datetime.fromisoformat(local_day) + timedelta(days=1)).replace(tzinfo=LISBON)
    t = start_local.astimezone(timezone.utc)
    end = end_local.astimezone(timezone.utc)

    rows = []
    while t < end:
        stamp = t.astimezone(LISBON).strftime("%Y-%m-%d %H:%M:%S")
        # hydro 1000, wind 500, gas 400 (CCGT) and 400 (cogeneration duplicate),
        # imports 100, exports 50 -> generation 1900 + 100 - 50 = 1950.
        consumption = 1950.0 - consumption_offset
        rows.append(
            f"{stamp},1000,500,0,0,0,400,400,0,0,100,50,0,0,0,{consumption}"
        )
        t += timedelta(minutes=15)

    if shuffle:
        rows.reverse()
    path.write_text(
        "﻿Unidades: MW\nInformação acedida em: 01/01/2026 00:00:00\n"
        + HEADER + "\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def dst_db(tmp_path):
    """DuckDB loaded with four synthetic days: normal, normal, spring, autumn."""
    files = [
        make_csv(tmp_path / "ren_generation_20250421.csv", "2025-04-21"),
        make_csv(tmp_path / "ren_generation_20250428.csv", "2025-04-28"),
        make_csv(tmp_path / "ren_generation_20250330.csv", "2025-03-30"),
        make_csv(tmp_path / "ren_generation_20251026.csv", "2025-10-26"),
    ]
    db = tmp_path / "test.duckdb"
    ren_loader.load_files(files, db)
    con = duckdb.connect(str(db))
    con.execute("SET TimeZone = 'UTC'")
    yield con
    con.close()


# --------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------

def test_rows_per_local_day(dst_db):
    rows = dst_db.execute(
        """
        SELECT CAST(timezone('Europe/Lisbon', ts_utc) AS DATE) AS d, count(*)
        FROM ren_generation GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    counts = {str(d): n for d, n in rows}
    assert counts == {
        "2025-03-30": 92,
        "2025-04-21": 96,
        "2025-04-28": 96,
        "2025-10-26": 100,
    }


def test_consecutive_intervals_are_15_minutes_within_each_file(dst_db):
    bad = dst_db.execute(
        """
        WITH g AS (
          SELECT ts_utc - lag(ts_utc) OVER (PARTITION BY source_file ORDER BY ts_utc) AS gap
          FROM ren_generation)
        SELECT count(*) FROM g WHERE gap IS NOT NULL AND gap <> INTERVAL 15 MINUTE
        """
    ).fetchone()[0]
    assert bad == 0


def test_no_duplicate_ts_utc(dst_db):
    dupes = dst_db.execute(
        "SELECT count(*) FROM (SELECT ts_utc FROM ren_generation "
        "GROUP BY ts_utc HAVING count(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0


def test_autumn_repeated_hour_is_west_then_wet(dst_db):
    rows = dst_db.execute(
        """
        SELECT strftime(ts_utc, '%H:%M'), strftime(timezone('Europe/Lisbon', ts_utc), '%H:%M')
        FROM ren_generation
        WHERE ts_utc BETWEEN TIMESTAMPTZ '2025-10-26 00:00+00' AND TIMESTAMPTZ '2025-10-26 01:45+00'
        ORDER BY ts_utc
        """
    ).fetchall()
    local = ["01:00", "01:15", "01:30", "01:45"] * 2
    utc = ["00:00", "00:15", "00:30", "00:45", "01:00", "01:15", "01:30", "01:45"]
    assert [r[0] for r in rows] == utc
    assert [r[1] for r in rows] == local


def test_spring_skipped_hour_is_absent(dst_db):
    local_times = {
        r[0]
        for r in dst_db.execute(
            "SELECT strftime(timezone('Europe/Lisbon', ts_utc), '%H:%M') "
            "FROM ren_generation WHERE CAST(timezone('Europe/Lisbon', ts_utc) AS DATE) = DATE '2025-03-30'"
        ).fetchall()
    }
    assert not {"01:00", "01:15", "01:30", "01:45"} & local_times


# --------------------------------------------------------------------------
# Ordering guard
# --------------------------------------------------------------------------

@pytest.mark.parametrize("day", ["2025-04-21", "2025-10-26"])
def test_unsorted_rows_are_rejected(tmp_path, day):
    path = make_csv(tmp_path / "ren_generation_x.csv", day, shuffle=True)
    with pytest.raises(ValueError):
        ren_loader.read_ren_csv(path)


# --------------------------------------------------------------------------
# Columns, balance and idempotency
# --------------------------------------------------------------------------

def test_gas_counted_once_and_cogeneration_dropped(tmp_path):
    df = ren_loader.read_ren_csv(make_csv(tmp_path / "f.csv", "2025-04-21"))
    assert "natural_gas" in df.columns
    assert not any("cogen" in c for c in df.columns)
    assert (df["natural_gas"] == 400).all()
    assert df["balance_residual_mw"].abs().max() == pytest.approx(0.0)


def test_balance_warning_above_threshold(tmp_path):
    df = ren_loader.read_ren_csv(
        make_csv(tmp_path / "f.csv", "2025-04-21", consumption_offset=60.0)
    )
    with pytest.warns(UserWarning, match="balance residual"):
        bad = ren_loader.check_balance(df, "f.csv")
    assert len(bad) == len(df)


def test_no_balance_warning_within_threshold(tmp_path):
    df = ren_loader.read_ren_csv(
        make_csv(tmp_path / "f.csv", "2025-04-21", consumption_offset=49.0)
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ren_loader.check_balance(df, "f.csv").empty


def test_loading_same_file_twice_does_not_duplicate(tmp_path):
    f = make_csv(tmp_path / "ren_generation_20251026.csv", "2025-10-26")
    db = tmp_path / "t.duckdb"
    ren_loader.load_files([f], db)
    ren_loader.load_files([f], db)
    con = duckdb.connect(str(db))
    assert con.execute("SELECT count(*) FROM ren_generation").fetchone()[0] == 100
    con.close()


# --------------------------------------------------------------------------
# Real data (skipped when data/ is not present)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, expected_rows",
    [
        ("ren_generation_20250330.csv", 92),
        ("ren_generation_20250421.csv", 96),
        ("ren_generation_20250428.csv", 96),
        ("ren_generation_20251026.csv", 100),
    ],
)
def test_real_files(name, expected_rows):
    path = REAL_DIR / name
    if not path.exists():
        pytest.skip(f"{path} not available")
    df = ren_loader.read_ren_csv(path)
    assert len(df) == expected_rows
    assert df["ts_utc"].diff().dropna().eq(timedelta(minutes=15)).all()
    assert df["balance_residual_mw"].abs().max() <= ren_loader.BALANCE_THRESHOLD_MW
