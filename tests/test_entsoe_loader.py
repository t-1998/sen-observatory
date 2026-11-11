"""Tests for sen_observatory.entsoe_loader.

No network: XML documents are synthetic and the HTTP session is a fake.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest
import requests

from sen_observatory import entsoe_loader as el

START = datetime(2025, 4, 1, tzinfo=timezone.utc)
END = datetime(2025, 4, 2, tzinfo=timezone.utc)
TOKEN = "SECRET-TOKEN-123"
NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def period(start: str, end: str, res: str, points: dict[int, float], tag: str = "quantity") -> str:
    pts = "".join(
        f"<Point><position>{p}</position><{tag}>{v}</{tag}></Point>" for p, v in points.items()
    )
    return (
        f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>{res}</resolution>{pts}</Period>"
    )


def series(periods: str, *, psr: str | None = None, zone: str | None = None,
           curve: str = "A01", unit: str = "MAW", price_unit: bool = False) -> str:
    psr_xml = f"<MktPSRType><psrType>{psr}</psrType></MktPSRType>" if psr else ""
    zone_xml = f"<{zone}_Domain.mRID>10YPT-REN------W</{zone}_Domain.mRID>" if zone else ""
    unit_xml = (
        "<currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
        if price_unit
        else f"<quantity_Measure_Unit.name>{unit}</quantity_Measure_Unit.name>"
    )
    return (f"<TimeSeries><curveType>{curve}</curveType>{psr_xml}{zone_xml}{unit_xml}"
            f"{periods}</TimeSeries>")


def doc(*all_series: str) -> str:
    return f'<?xml version="1.0"?><GL_MarketDocument xmlns="{NS}">{"".join(all_series)}</GL_MarketDocument>'


NO_DATA = (
    '<?xml version="1.0"?><Acknowledgement_MarketDocument '
    'xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">'
    "<Reason><code>999</code><text>No matching data found</text></Reason>"
    "</Acknowledgement_MarketDocument>"
)

GEN = el.Query("PT_generation", "generation", "PT", {"documentType": "A75"})


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code, self.text = status_code, text


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(el.time, "sleep", lambda s: None)


# --------------------------------------------------------------------------
# parsing: resolution and curve types
# --------------------------------------------------------------------------

def test_hourly_resolution_expands_to_24_rows():
    xml = doc(series(period("2025-04-01T00:00Z", "2025-04-02T00:00Z", "PT60M",
                            {i: 100.0 + i for i in range(1, 25)}), psr="B19", zone="inBiddingZone"))
    df = el.parse_document(xml, GEN)
    assert len(df) == 24
    assert set(df["resolution_min"]) == {60}
    assert df["ts_utc"].iloc[1] - df["ts_utc"].iloc[0] == pd_timedelta(60)


def test_quarter_hourly_resolution_expands_to_96_rows():
    xml = doc(series(period("2025-04-01T00:00Z", "2025-04-02T00:00Z", "PT15M",
                            {i: 1.0 for i in range(1, 97)}), psr="B16", zone="inBiddingZone"))
    df = el.parse_document(xml, GEN)
    assert len(df) == 96
    assert set(df["resolution_min"]) == {15}


def test_mixed_resolutions_in_one_document_keep_their_own_resolution():
    xml = doc(
        series(period("2025-04-01T00:00Z", "2025-04-01T02:00Z", "PT60M", {1: 1, 2: 2}), psr="B19", zone="inBiddingZone"),
        series(period("2025-04-01T00:00Z", "2025-04-01T01:00Z", "PT15M", {1: 5, 2: 6, 3: 7, 4: 8}), psr="B16", zone="inBiddingZone"),
    )
    df = el.parse_document(xml, GEN)
    by_var = df.groupby("variable")["resolution_min"].first().to_dict()
    assert by_var == {"generation_wind_onshore": 60, "generation_solar": 15}


def test_a03_omitted_points_repeat_previous_value():
    # 8 hourly slots; points only at positions 1 and 5 -> 1..4 = 10, 5..8 = 20
    xml = doc(series(period("2025-04-01T00:00Z", "2025-04-01T08:00Z", "PT60M", {1: 10.0, 5: 20.0}),
                     psr="B19", zone="inBiddingZone", curve="A03"))
    df = el.parse_document(xml, GEN)
    assert df["value"].tolist() == [10.0] * 4 + [20.0] * 4


def test_a01_missing_points_are_not_invented():
    xml = doc(series(period("2025-04-01T00:00Z", "2025-04-01T04:00Z", "PT60M", {1: 10.0, 4: 40.0}),
                     psr="B19", zone="inBiddingZone", curve="A01"))
    df = el.parse_document(xml, GEN)
    assert df["value"].tolist() == [10.0, 40.0]


def test_unsupported_resolution_is_an_error():
    xml = doc(series(period("2025-04-01T00:00Z", "2025-04-02T00:00Z", "P1D", {1: 1.0}), psr="B19", zone="inBiddingZone"))
    with pytest.raises(ValueError, match="resolution"):
        el.parse_document(xml, GEN)


# --------------------------------------------------------------------------
# parsing: variables, units, empty answers
# --------------------------------------------------------------------------

def test_generation_vs_consumption_variable_names():
    p = period("2025-04-01T00:00Z", "2025-04-01T01:00Z", "PT60M", {1: 7.0})
    xml = doc(series(p, psr="B10", zone="inBiddingZone"), series(p, psr="B10", zone="outBiddingZone"))
    df = el.parse_document(xml, GEN)
    assert sorted(df["variable"]) == ["consumption_hydro_pumped_storage", "generation_hydro_pumped_storage"]
    assert set(df["unit"]) == {"MW"}


def test_price_unit_and_variable():
    q = el.Query("PT_price_da", "price", "PT", {})
    xml = doc(series(period("2025-04-01T00:00Z", "2025-04-01T02:00Z", "PT60M", {1: 50.5, 2: 40.0}, tag="price.amount"),
                     price_unit=True))
    df = el.parse_document(xml, q)
    assert set(df["variable"]) == {"price_day_ahead"}
    assert set(df["unit"]) == {"EUR/MWh"}
    assert df["value"].tolist() == [50.5, 40.0]


def test_flow_uses_fixed_variable_and_origin_country():
    q = el.Query("PT_flow_to_ES", "flow", "PT", {}, variable="physical_flow_to_ES")
    xml = doc(series(period("2025-04-01T00:00Z", "2025-04-01T01:00Z", "PT60M", {1: 300.0})))
    df = el.parse_document(xml, q)
    assert df[["country", "variable"]].iloc[0].tolist() == ["PT", "physical_flow_to_ES"]


def test_no_data_acknowledgement_gives_empty_frame():
    df = el.parse_document(NO_DATA, GEN)
    assert df.empty and list(df.columns) == el.COLUMNS


def test_other_acknowledgement_is_an_error():
    bad = NO_DATA.replace("<code>999</code>", "<code>401</code>")
    with pytest.raises(ValueError, match="API error"):
        el.parse_document(bad, GEN)


def test_area_codes():
    # Spain's TSO is REE (not REN, which is Portugal's); the API accepts a wrong
    # code silently and just returns "no matching data".
    assert el.AREAS == {"PT": "10YPT-REN------W", "ES": "10YES-REE------0"}


def test_price_uses_same_zone_for_in_and_out_domain():
    for q in el.build_queries():
        if q.kind == "price":
            assert q.params["in_Domain"] == q.params["out_Domain"] == el.AREAS[q.country]


def test_query_set_matches_the_request():
    names = {q.name for q in el.build_queries()}
    assert "PT_load_forecast_da" in names and "ES_load_forecast_da" not in names  # DA load: PT only
    flows = {q.name: q.params for q in el.build_queries() if q.kind == "flow"}
    assert flows["PT_flow_to_ES"]["out_Domain"] == el.AREAS["PT"]
    assert flows["PT_flow_to_ES"]["in_Domain"] == el.AREAS["ES"]
    assert flows["ES_flow_to_PT"]["out_Domain"] == el.AREAS["ES"]
    for cc in ("PT", "ES"):
        assert {f"{cc}_generation", f"{cc}_load_actual", f"{cc}_res_forecast_da", f"{cc}_price_da"} <= names


# --------------------------------------------------------------------------
# fetching: cache and token safety
# --------------------------------------------------------------------------

GOOD = doc(series(period("2025-04-01T00:00Z", "2025-04-01T02:00Z", "PT60M", {1: 1.0, 2: 2.0}),
                  psr="B19", zone="inBiddingZone"))


def test_second_fetch_comes_from_cache_without_api_call(tmp_path):
    s = FakeSession(FakeResponse(200, GOOD))
    text1, cached1 = el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s)
    text2, cached2 = el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s)
    assert (cached1, cached2) == (False, True)
    assert s.calls == 1 and text1 == text2
    assert len(list(tmp_path.glob("*.xml"))) == 1


def test_raw_cache_never_contains_the_token(tmp_path):
    el.fetch_raw(GEN, START, END, tmp_path, TOKEN, FakeSession(FakeResponse(200, GOOD)))
    for f in tmp_path.iterdir():
        assert TOKEN not in f.name and TOKEN not in f.read_text()


def test_no_data_answer_is_cached_and_refreshable(tmp_path):
    s = FakeSession(FakeResponse(200, NO_DATA), FakeResponse(200, GOOD))
    el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s)
    el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s)
    assert s.calls == 1  # empty answer cached
    text, cached = el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s, refresh_empty=True)
    assert s.calls == 2 and not cached and "<Point>" in text
    el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s, refresh_empty=True)
    assert s.calls == 2  # real data is never re-downloaded


def test_http_400_no_data_is_treated_as_empty(tmp_path):
    s = FakeSession(FakeResponse(400, NO_DATA))
    text, _ = el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s)
    assert el.parse_document(text, GEN).empty


def test_unauthorized_error_does_not_leak_token(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        el.fetch_raw(GEN, START, END, tmp_path, TOKEN, FakeSession(FakeResponse(401, "")))
    assert TOKEN not in str(exc.value) and "401" in str(exc.value)
    assert not list(tmp_path.glob("*.xml"))


def test_connection_error_does_not_leak_token(tmp_path):
    boom = requests.ConnectionError(f"https://x/api?securityToken={TOKEN}")
    with pytest.raises(RuntimeError) as exc:
        el.fetch_raw(GEN, START, END, tmp_path, TOKEN, FakeSession(boom))
    assert TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_retries_on_429_then_succeeds(tmp_path):
    s = FakeSession(FakeResponse(429, ""), FakeResponse(200, GOOD))
    text, cached = el.fetch_raw(GEN, START, END, tmp_path, TOKEN, s)
    assert s.calls == 2 and not cached and "<Point>" in text


def test_missing_token_gives_clear_error(monkeypatch):
    monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
    monkeypatch.setattr(el, "load_dotenv", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="ENTSOE_API_TOKEN"):
        el.get_token()


# --------------------------------------------------------------------------
# DuckDB
# --------------------------------------------------------------------------

def test_loading_twice_does_not_duplicate_rows(tmp_path):
    db, raw = tmp_path / "t.duckdb", tmp_path / "raw"
    queries = [GEN]
    for _ in range(2):
        el.load(START, END, db, raw, TOKEN, FakeSession(FakeResponse(200, GOOD)), queries)
    con = duckdb.connect(str(db))
    assert con.execute(f"SELECT count(*) FROM {el.TABLE}").fetchone()[0] == 2
    cols = [r[0] for r in con.execute(f"DESCRIBE {el.TABLE}").fetchall()]
    assert cols == ["ts_utc", "country", "variable", "value", "unit", "resolution_min"]
    con.close()


def test_same_timestamp_allowed_for_different_countries(tmp_path):
    db = tmp_path / "t.duckdb"
    q_es = el.Query("ES_generation", "generation", "ES", {"documentType": "A75"})
    s = FakeSession(FakeResponse(200, GOOD))
    el.load(START, END, db, tmp_path / "raw", TOKEN, s, [GEN, q_es])
    con = duckdb.connect(str(db))
    assert con.execute(f"SELECT count(*) FROM {el.TABLE}").fetchone()[0] == 4
    con.close()


def test_reloading_updates_changed_values(tmp_path):
    db = tmp_path / "t.duckdb"
    changed = GOOD.replace("<quantity>2.0</quantity>", "<quantity>99.0</quantity>")
    el.load(START, END, db, tmp_path / "raw1", TOKEN, FakeSession(FakeResponse(200, GOOD)), [GEN])
    el.load(START, END, db, tmp_path / "raw2", TOKEN, FakeSession(FakeResponse(200, changed)), [GEN])
    con = duckdb.connect(str(db))
    assert con.execute(f"SELECT max(value), count(*) FROM {el.TABLE}").fetchone() == (99.0, 2)
    con.close()


def pd_timedelta(minutes: int):
    import pandas as pd
    return pd.Timedelta(minutes=minutes)
