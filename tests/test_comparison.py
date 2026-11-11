"""Tests for the REN vs ENTSO-E comparison (sql/ files and sen_observatory.comparison).

Most tests build tiny synthetic databases, so they run without ``data/``. The last
ones run against the real database and are skipped when it is not there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import pytest

from sen_observatory import comparison, entsoe_loader, ren_loader

LISBON = ZoneInfo("Europe/Lisbon")
HOUR = timedelta(hours=1)
REAL_DB = Path(__file__).resolve().parents[1] / "data" / "sen.duckdb"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("SET TimeZone = 'UTC'")
    ren_loader.ensure_table(c)
    entsoe_loader.ensure_table(c)
    yield c
    c.close()


def utc_hours(day: str):
    """UTC start of every hour of a local (Lisbon) day."""
    start = datetime.fromisoformat(day).replace(tzinfo=LISBON).astimezone(timezone.utc)
    end = (datetime.fromisoformat(day) + timedelta(days=1)).replace(tzinfo=LISBON).astimezone(timezone.utc)
    t = start
    while t < end:
        yield t
        t += HOUR


def add_ren(con, day: str, *, skip=(), **values: float) -> None:
    """15-min REN rows for a local day. ``skip`` = UTC datetimes of readings to leave out."""
    rows = []
    for h in utc_hours(day):
        for q in range(4):
            ts = h + q * timedelta(minutes=15)
            if ts in skip:
                continue
            row = {c: 0.0 for c in ren_loader.VALUE_COLUMNS}
            row.update(values)
            rows.append({"ts_utc": ts, **row, "balance_residual_mw": 0.0, "source_file": "t.csv"})
    df = pd.DataFrame(rows)
    con.register("tmp_ren", df)
    cols = ", ".join(ren_loader.OUTPUT_COLUMNS)
    con.execute(f"INSERT INTO ren_generation ({cols}) SELECT {cols} FROM tmp_ren")
    con.unregister("tmp_ren")


def add_entsoe(con, day: str, variable: str, value: float, *, country="PT",
               resolution=60, skip_hours=()) -> None:
    rows = []
    unit = "MW"
    for h in utc_hours(day):
        if h in skip_hours:
            continue
        for k in range(60 // resolution):
            rows.append((h + k * timedelta(minutes=resolution), country, variable, value, unit, resolution))
    df = pd.DataFrame(rows, columns=entsoe_loader.COLUMNS)
    con.register("tmp_en", df)
    cols = ", ".join(entsoe_loader.COLUMNS)
    con.execute(f"INSERT INTO entsoe_timeseries ({cols}) SELECT {cols} FROM tmp_en")
    con.unregister("tmp_en")


def row(df: pd.DataFrame, day: str, component: str, period: str = "full_day") -> pd.Series:
    sel = df[(df["local_day"] == pd.Timestamp(day)) & (df["component"] == component) & (df["period"] == period)]
    assert len(sel) == 1, f"expected one row for {day}/{period}/{component}, got {len(sel)}"
    return sel.iloc[0]


DAY = "2025-04-21"


def full_pt_day(con, day=DAY, **overrides):
    """A day where both sources agree on everything (generation, load, pumping, flows)."""
    ren = dict(hydro=100.0, wind=50.0, solar=20.0, biomass=10.0, natural_gas=40.0, coal=0.0,
               other_thermal=5.0, pumping=30.0, consumption=300.0, imports=80.0, exports=0.0)
    ren.update(overrides.get("ren", {}))
    add_ren(con, day, **ren)
    en = {
        "generation_hydro_run_of_river": 40.0, "generation_hydro_reservoir": 30.0,
        "generation_hydro_pumped_storage": 30.0,
        "generation_wind_onshore": 45.0, "generation_wind_offshore": 5.0,
        "generation_solar": 20.0, "generation_biomass": 10.0, "generation_fossil_gas": 40.0,
        "generation_fossil_hard_coal": 0.0, "generation_other": 5.0,
        "consumption_hydro_pumped_storage": 30.0, "load_actual": 300.0,
    }
    en.update(overrides.get("en", {}))
    for var, val in en.items():
        add_entsoe(con, day, var, val)
    add_entsoe(con, day, "physical_flow_to_PT", overrides.get("flow_to_pt", 80.0), country="ES")
    add_entsoe(con, day, "physical_flow_to_ES", overrides.get("flow_to_es", 0.0), country="PT")


# --------------------------------------------------------------------------
# mapping and arithmetic
# --------------------------------------------------------------------------

def test_agreeing_sources_give_zero_difference_everywhere(con):
    full_pt_day(con)
    df = comparison.run_comparison(con)
    day = df[df["period"] == "full_day"]
    assert (day["hours"] == 24).all()
    assert (day["diff_mwh"].abs() < 1e-6).all()


def test_hydro_is_run_of_river_plus_reservoir_plus_pumped_generation(con):
    full_pt_day(con)
    r = row(comparison.run_comparison(con), DAY, "hydro")
    assert r["ren_mwh"] == pytest.approx(100 * 24)
    assert r["entsoe_mwh"] == pytest.approx((40 + 30 + 30) * 24)


def test_dropping_pumped_generation_would_show_up_as_a_difference(con):
    full_pt_day(con, en={"generation_hydro_pumped_storage": 0.0})
    r = row(comparison.run_comparison(con), DAY, "hydro")
    assert r["diff_mwh"] == pytest.approx(30 * 24)


def test_wind_is_onshore_plus_offshore(con):
    full_pt_day(con)
    r = row(comparison.run_comparison(con), DAY, "wind")
    assert r["entsoe_mwh"] == pytest.approx(50 * 24)


def test_energy_diff_and_percent(con):
    full_pt_day(con, ren={"solar": 22.0})  # REN 22 MW vs ENTSO-E 20 MW
    r = row(comparison.run_comparison(con), DAY, "solar")
    assert r["ren_mwh"] == pytest.approx(22 * 24)
    assert r["diff_mwh"] == pytest.approx(2 * 24)
    assert r["diff_pct"] == pytest.approx(10.0)


def test_net_interconnection_sign_is_positive_for_imports(con):
    full_pt_day(con, ren={"imports": 100.0, "exports": 0.0}, flow_to_pt=100.0)
    r = row(comparison.run_comparison(con), DAY, "net_interconnection")
    assert r["ren_mwh"] == pytest.approx(100 * 24)
    assert r["entsoe_mwh"] == pytest.approx(100 * 24)


def test_net_interconnection_with_exports(con):
    full_pt_day(con, ren={"imports": 0.0, "exports": 70.0}, flow_to_pt=0.0, flow_to_es=70.0)
    r = row(comparison.run_comparison(con), DAY, "net_interconnection")
    assert r["ren_mwh"] == pytest.approx(-70 * 24)
    assert r["entsoe_mwh"] == pytest.approx(-70 * 24)


def test_pumping_and_consumption_map_to_their_entsoe_variables(con):
    full_pt_day(con, en={"consumption_hydro_pumped_storage": 33.0, "load_actual": 301.0})
    df = comparison.run_comparison(con)
    assert row(df, DAY, "pumping")["diff_mwh"] == pytest.approx(-3 * 24)
    assert row(df, DAY, "consumption")["diff_mwh"] == pytest.approx(-1 * 24)


def test_battery_and_wave_have_no_entsoe_category_and_count_as_zero(con):
    full_pt_day(con, ren={"battery_discharge": 2.0, "wave": 0.0})
    df = comparison.run_comparison(con)
    b = row(df, DAY, "battery_discharge")
    assert b["status"] == "no_entsoe_category"
    assert b["ren_mwh"] == pytest.approx(2 * 24) and b["entsoe_mwh"] == 0
    assert pd.isna(b["diff_pct"])  # no percentage against zero


def test_total_generation_difference_equals_battery(con):
    full_pt_day(con, ren={"battery_discharge": 2.0})
    t = row(comparison.run_comparison(con), DAY, "total_generation")
    assert t["diff_mwh"] == pytest.approx(2 * 24)


# --------------------------------------------------------------------------
# time axis
# --------------------------------------------------------------------------

@pytest.mark.parametrize("day, hours", [("2025-03-30", 23), ("2025-04-21", 24), ("2025-10-26", 25)])
def test_local_day_has_23_24_or_25_hours(con, day, hours):
    full_pt_day(con, day)
    r = row(comparison.run_comparison(con), day, "solar")
    assert r["hours"] == hours
    assert r["ren_mwh"] == pytest.approx(20 * hours)


def test_incomplete_ren_hour_is_dropped(con):
    missing = datetime(2025, 4, 21, 10, 15, tzinfo=timezone.utc)  # one quarter of one hour
    add_ren(con, DAY, skip=(missing,), solar=20.0)
    add_entsoe(con, DAY, "generation_solar", 20.0)
    r = row(comparison.run_comparison(con), DAY, "solar")
    assert r["hours"] == 23


def test_incomplete_multivariable_component_is_not_summed_partially(con):
    full_pt_day(con)
    hour = list(utc_hours(DAY))[5]
    con.execute("DELETE FROM entsoe_timeseries WHERE variable = 'generation_wind_offshore' AND ts_utc = ?", [hour])
    r = row(comparison.run_comparison(con), DAY, "wind")
    assert r["hours"] == 23  # the hour is dropped, not summed with only onshore wind
    assert abs(r["diff_mwh"]) < 1e-6


def test_quarter_hourly_entsoe_flows_are_averaged_to_the_hour(con):
    full_pt_day(con)
    con.execute("DELETE FROM entsoe_timeseries WHERE variable LIKE 'physical_flow%'")
    add_entsoe(con, DAY, "physical_flow_to_PT", 80.0, country="ES", resolution=15)
    add_entsoe(con, DAY, "physical_flow_to_ES", 0.0, country="PT", resolution=15)
    r = row(comparison.run_comparison(con), DAY, "net_interconnection")
    assert r["hours"] == 24 and r["entsoe_mwh"] == pytest.approx(80 * 24)


def test_28_april_is_split_before_during_and_after_the_11h_hour(con):
    full_pt_day(con, "2025-04-28")
    df = comparison.run_comparison(con)
    hours = {p: row(df, "2025-04-28", "solar", p)["hours"]
             for p in ("before_11:00", "transition_11:00-12:00", "after_12:00")}
    assert hours == {"before_11:00": 11, "transition_11:00-12:00": 1, "after_12:00": 12}
    parts = df[(df["local_day"] == pd.Timestamp("2025-04-28")) & (df["component"] == "solar")
               & (df["period"] != "full_day")]["ren_mwh"].sum()
    assert parts == pytest.approx(row(df, "2025-04-28", "solar")["ren_mwh"])


def test_other_days_are_not_split(con):
    full_pt_day(con, DAY)
    assert set(comparison.run_comparison(con)["period"]) == {"full_day"}


# --------------------------------------------------------------------------
# mapping guard
# --------------------------------------------------------------------------

EXCLUDED = ["generation_fossil_oil", "generation_waste", "generation_other_renewable",
            "generation_energy_storage", "consumption_energy_storage"]


def test_guard_passes_when_excluded_categories_are_absent(con):
    full_pt_day(con)
    assert comparison.check_mapping(con) == []


@pytest.mark.parametrize("variable", EXCLUDED)
def test_guard_passes_when_excluded_categories_are_zero(con, variable):
    full_pt_day(con)
    add_entsoe(con, DAY, variable, 0.0)
    assert comparison.check_mapping(con) == []


@pytest.mark.parametrize("variable", EXCLUDED)
def test_guard_fails_when_an_excluded_category_becomes_non_zero(con, variable):
    full_pt_day(con)
    add_entsoe(con, DAY, variable, 1.5)
    problems = comparison.check_mapping(con)
    assert len(problems) == 1 and variable in problems[0] and "non-zero" in problems[0]


def test_guard_fails_for_an_unmapped_pt_variable(con):
    full_pt_day(con)
    add_entsoe(con, DAY, "generation_nuclear", 0.0)
    problems = comparison.check_mapping(con)
    assert len(problems) == 1 and "generation_nuclear" in problems[0]


def test_guard_ignores_spanish_variables(con):
    full_pt_day(con)
    add_entsoe(con, DAY, "generation_nuclear", 7000.0, country="ES")
    assert comparison.check_mapping(con) == []


# --------------------------------------------------------------------------
# real database (skipped when data/ is not available)
# --------------------------------------------------------------------------

@pytest.fixture
def real_con():
    if not REAL_DB.exists():
        pytest.skip(f"{REAL_DB} not available")
    c = duckdb.connect(str(REAL_DB), read_only=True)
    tables = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if not {"ren_generation", "entsoe_timeseries"} <= tables:
        c.close()
        pytest.skip("real database lacks ren_generation or entsoe_timeseries")
    c.execute("SET TimeZone = 'UTC'")
    yield c
    c.close()


def test_real_data_mapping_needs_no_review(real_con):
    """Fails if an excluded category stops being 0 or a new PT category appears."""
    assert comparison.check_mapping(real_con) == []


def test_real_data_covers_every_loaded_ren_day(real_con):
    df = comparison.run_comparison(real_con)
    full = df[df["period"] == "full_day"]
    got = {str(d.date()): int(h) for d, h in
           full[full["component"] == "solar"][["local_day", "hours"]].itertuples(index=False)}
    assert got == {"2025-03-30": 23, "2025-04-21": 24, "2025-04-28": 24, "2025-04-29": 24, "2025-10-26": 25}
