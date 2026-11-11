"""Tests for sen_observatory.analysis.blackout.

The helpers are tested on tiny synthetic series. The last tests pin the results on the real
database and are skipped when it is not available.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from sen_observatory.analysis import blackout as b

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "sen.duckdb"


def series(values, start="2025-04-28 12:00", freq="15min") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.Series(values, index=idx, dtype=float)


# --------------------------------------------------------------------------
# reference days
# --------------------------------------------------------------------------

def test_reference_days_are_april_weekdays_without_holidays_event_and_day_after():
    days = b.reference_days()
    assert [d.day for d in days] == [1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16, 17, 21, 22, 23, 24, 30]
    assert len(days) == 18
    assert all(d.weekday() < 5 for d in days)
    assert date(2025, 4, 18) not in days  # Good Friday
    assert date(2025, 4, 25) not in days  # Freedom Day
    assert date(2025, 4, 28) not in days  # the event
    assert date(2025, 4, 29) not in days  # the day after
    assert {date(2025, 4, 21), date(2025, 4, 30)} <= set(days)


# --------------------------------------------------------------------------
# synchronous / inverter-based groups
# --------------------------------------------------------------------------

def test_groups_sum_the_right_components_and_leave_out_pumping():
    comp = pd.Series({"hydro": 100.0, "wind": 30.0, "solar": 20.0, "biomass": 5.0, "natural_gas": 10.0,
                      "other_thermal": 1.0, "net_interconnection": 50.0, "pumping": 80.0, "consumption": 200.0})
    out = b.with_groups(comp)
    assert out["synchronous_generation"] == pytest.approx(100 + 10 + 5 + 1)  # no pumping, no imports
    assert out["inverter_based_generation"] == pytest.approx(30 + 20)
    assert out["consumption"] == 200.0  # the original components are untouched
    assert set(comp.index) <= set(out.index)


def test_event_frame_needs_the_ren_data_of_the_day_after():
    import duckdb as ddb
    from sen_observatory import ren_loader

    con = ddb.connect()
    con.execute("SET TimeZone = 'UTC'")
    ren_loader.ensure_table(con)  # empty table: no REN data at all
    with pytest.raises(ValueError, match="load the REN files"):
        b.event_frame(con)
    con.close()


# --------------------------------------------------------------------------
# percentages and crossings
# --------------------------------------------------------------------------

def test_pct_of_consumption():
    assert b.pct_of(50, 200) == pytest.approx(25.0)
    assert b.pct_of(-100, 200) == pytest.approx(-50.0)
    assert pd.isna(b.pct_of(10, 0))


def test_first_crossing_is_strictly_after_and_inclusive_of_the_threshold():
    s = series([10, 30, 50, 40, 60])
    start = s.index[0]
    assert b.first_crossing(s, 50, start) == s.index[2]  # >= 50
    assert b.first_crossing(s, 30, s.index[1]) == s.index[2]  # strictly after the given time
    assert b.first_crossing(s, 99, start) is None


def test_stays_above():
    s = series([10, 60, 70, 55])
    assert b.stays_above(s, s.index[1], 50)
    assert not b.stays_above(s, s.index[0], 50)
    assert not b.stays_above(series([60, 40, 70]), pd.Timestamp("2025-04-28 12:00"), 50)


# --------------------------------------------------------------------------
# "back above 50 MW" rule
# --------------------------------------------------------------------------

def test_return_above_finds_the_first_interval_above_the_limit_after_a_low():
    s = series([200, 10, 0, 20, 49, 51, 80])
    r = b.return_above(s, 50, s.index[0])
    assert r["status"] == "returned"
    assert r["time"] == s.index[5]  # 51 > 50
    assert r["first_low"] == s.index[1]  # the 200 at the start is not a restoration


def test_return_above_needs_strictly_more_than_the_limit():
    s = series([100, 20, 50, 50, 50])
    assert b.return_above(s, 50, s.index[0])["status"] == "not_reached"


def test_return_above_when_it_never_dropped():
    s = series([300, 120, 80, 90])
    r = b.return_above(s, 50, s.index[0])
    assert r == {"status": "stayed_above", "minimum": 80.0}


def test_return_above_inclusive_counts_the_limit_itself():
    s = series([100, 4, 4.9, 5, 9])
    r = b.return_above(s, 5, s.index[0], inclusive=True)  # >= 5 after a value < 5
    assert r["status"] == "returned" and r["time"] == s.index[3]
    assert b.return_above(series([100, 4, 4.9, 4.99]), 5, pd.Timestamp("2025-04-28 12:00"), inclusive=True)["status"] == "not_reached"


def test_return_above_inclusive_when_it_never_dropped_below_the_limit():
    s = series([300, 14, 15, 90])
    assert b.return_above(s, 5, s.index[0], inclusive=True) == {"status": "stayed_above", "minimum": 14.0}


def test_hours_text_rounds_to_the_half_hour():
    assert b.hours_text(12.5) == "12½"
    assert b.hours_text(12.0) == "12"
    assert b.hours_text(12.3) == "12½"
    assert b.hours_text(12.2) == "12"
    assert b.hours_text(0.5) == "0½"


def test_return_above_ignores_values_before_the_start():
    s = series([0, 0, 90, 10, 70])
    r = b.return_above(s, 50, s.index[2])  # starts at the 90
    assert r["status"] == "returned" and r["time"] == s.index[4]


# --------------------------------------------------------------------------
# real data
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


def test_real_operating_point_at_11_15(real_con):
    t = b.operating_point(real_con)
    col = "28/04 11:15"
    assert t.loc["Consumption", (col, "MW")] == pytest.approx(5861.1, abs=0.1)
    assert t.loc["Net imports", (col, "MW")] == pytest.approx(2764.8, abs=0.1)
    assert t.loc["Pumping (load)", (col, "MW")] == pytest.approx(2098.4, abs=0.1)
    assert t.loc["Consumption", (col, "% of consumption")] == pytest.approx(100.0)
    avg = [c for c in t.columns.get_level_values(0).unique() if c.startswith("April weekdays")][0]
    assert "n=18" in avg
    # descriptive groups: synchronous = hydro + gas + biomass + other thermal; inverter = wind + solar
    sync, inv = t.loc[b.LABELS["synchronous_generation"], (col, "MW")], t.loc[b.LABELS["inverter_based_generation"], (col, "MW")]
    assert sync == pytest.approx(718.2 + 424.4 + 313.4 + 25.4, abs=0.2)
    assert inv == pytest.approx(1484.7 + 2250.4, abs=0.2)
    assert t.loc[b.LABELS["synchronous_generation"], (col, "% of consumption")] == pytest.approx(100 * sync / 5861.1, abs=0.1)


def test_real_restoration_thresholds_are_ordered_and_use_ren_only(real_con):
    res = b.restoration(real_con)
    times = [res["crossings"][t]["time"] for t in b.THRESHOLDS_PCT]
    assert times == sorted(times)
    assert res["crossings"][25]["time"] == pd.Timestamp("2025-04-28 21:00")
    assert res["crossings"][50]["time"] == pd.Timestamp("2025-04-28 22:00")
    assert res["crossings"][75]["time"] == pd.Timestamp("2025-04-28 23:15")
    assert res["crossings"][90]["time"] == pd.Timestamp("2025-04-29 00:00")
    assert res["frame"].index.max() == pd.Timestamp("2025-04-29 23:45")  # REN only, 15 min
    assert "source" not in res["frame"].columns


def test_real_titles_follow_the_data(real_con):
    res = b.restoration(real_con)
    assert b.restoration_title(res) == "Consumption reached 90 % of a normal day about 12½ hours after the drop"
    assert "below 90 % in 7 intervals" in b.dip_note(res) and "87.1 %" in b.dip_note(res)
    day = b.load_ren_range(real_con, b.EVENT_DAY, b.EVENT_DAY)
    assert b.operation_title(day) == (
        "At 11:15 on 28 April, Portugal was importing 47 % of its consumption and pumping 36 %")


def test_real_first_production_after_the_drop(real_con):
    r5 = b.restoration(real_con)["returns_5"]
    assert r5["hydro"]["time"] == pd.Timestamp("2025-04-28 15:30")
    assert r5["natural_gas"]["status"] == "stayed_above" and r5["natural_gas"]["minimum"] == pytest.approx(14.1, abs=0.1)
    assert r5["biomass"]["status"] == "stayed_above"
    assert r5["pumping"]["time"] == pd.Timestamp("2025-04-29 03:00")


def test_real_technology_returns(real_con):
    r = b.restoration(real_con)["returns"]
    assert r["natural_gas"]["time"] == pd.Timestamp("2025-04-28 17:30")
    assert r["hydro"]["time"] == pd.Timestamp("2025-04-28 18:00")
    assert r["wind"]["time"] == pd.Timestamp("2025-04-28 22:30")
    assert r["biomass"]["status"] == "stayed_above"
    assert r["other_thermal"]["status"] == "below_before_event"
    assert r["solar"]["time"] == pd.Timestamp("2025-04-29 07:15")
    assert r["pumping"]["time"] == pd.Timestamp("2025-04-29 03:00")
    assert r["net_interconnection"]["status"] == "not_reached"
