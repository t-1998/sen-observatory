"""Download ENTSO-E Transparency Platform data into DuckDB.

Raw XML responses are cached in ``data/raw/entsoe/`` so the API is never asked
twice for the same thing. Parsed data goes to a long-format table
``entsoe_timeseries`` (ts_utc, country, variable, value, unit, resolution_min).

Usage:
    uv run python -m sen_observatory.entsoe_loader --start 2025-04-01 --end 2025-05-01

The API token is read from ``ENTSOE_API_TOKEN`` in ``.env``. It is never
printed or stored.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd
import requests
from dotenv import find_dotenv, load_dotenv

API_URL = "https://web-api.tp.entsoe.eu/api"
DEFAULT_DB_PATH = Path("data/sen.duckdb")
DEFAULT_RAW_DIR = Path("data/raw/entsoe")
TABLE = "entsoe_timeseries"

REQUEST_TIMEOUT_S = 120
MAX_ATTEMPTS = 4

# Energy Identification Codes of the bidding zones. The last part is the TSO:
# REN for Portugal, REE (Red Eléctrica) for Spain. Note the different endings.
AREAS = {
    "PT": "10YPT-REN------W",
    "ES": "10YES-REE------0",
}

# ENTSO-E production type codes (psrType) -> snake_case names.
PSR_NAMES = {
    "B01": "biomass",
    "B02": "fossil_brown_coal_lignite",
    "B03": "fossil_coal_derived_gas",
    "B04": "fossil_gas",
    "B05": "fossil_hard_coal",
    "B06": "fossil_oil",
    "B07": "fossil_oil_shale",
    "B08": "fossil_peat",
    "B09": "geothermal",
    "B10": "hydro_pumped_storage",
    "B11": "hydro_run_of_river",
    "B12": "hydro_reservoir",
    "B13": "marine",
    "B14": "nuclear",
    "B15": "other_renewable",
    "B16": "solar",
    "B17": "waste",
    "B18": "wind_offshore",
    "B19": "wind_onshore",
    "B20": "other",
    "B25": "energy_storage",
}

UNIT_NAMES = {"MAW": "MW", "MWH": "MWh"}


@dataclass(frozen=True)
class Query:
    """One API request (without the period and the token)."""

    name: str  # used in the cache file name and in messages
    kind: str  # generation | load_actual | load_forecast | res_forecast | price | flow
    country: str  # country the rows are attributed to
    params: dict = field(default_factory=dict, hash=False)
    variable: str | None = None  # fixed variable name (used by flows)


def build_queries() -> list[Query]:
    queries: list[Query] = []
    for cc, eic in AREAS.items():
        queries += [
            Query(f"{cc}_generation", "generation", cc,
                  {"documentType": "A75", "processType": "A16", "in_Domain": eic}),
            Query(f"{cc}_load_actual", "load_actual", cc,
                  {"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": eic}),
            Query(f"{cc}_res_forecast_da", "res_forecast", cc,
                  {"documentType": "A69", "processType": "A01", "in_Domain": eic}),
            Query(f"{cc}_price_da", "price", cc,
                  {"documentType": "A44", "in_Domain": eic, "out_Domain": eic,
                   "contract_MarketAgreement.Type": "A01"}),
        ]
    # Day-ahead load forecast: Portugal only.
    queries.append(
        Query("PT_load_forecast_da", "load_forecast", "PT",
              {"documentType": "A65", "processType": "A01",
               "outBiddingZone_Domain": AREAS["PT"]})
    )
    # Physical flows: a flow goes from out_Domain to in_Domain.
    for origin, dest in (("PT", "ES"), ("ES", "PT")):
        queries.append(
            Query(f"{origin}_flow_to_{dest}", "flow", origin,
                  {"documentType": "A11", "out_Domain": AREAS[origin], "in_Domain": AREAS[dest]},
                  variable=f"physical_flow_to_{dest}")
        )
    return queries


# --------------------------------------------------------------------------
# Fetching (with raw-response cache)
# --------------------------------------------------------------------------

def get_token() -> str:
    load_dotenv(find_dotenv(usecwd=True))
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ENTSOE_API_TOKEN is not set (add it to .env)")
    return token


def _fmt_period(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def cache_path(query: Query, start: datetime, end: datetime, raw_dir: Path) -> Path:
    digest = hashlib.sha1(repr(sorted(query.params.items())).encode()).hexdigest()[:8]
    return raw_dir / f"{query.name}_{_fmt_period(start)}_{_fmt_period(end)}_{digest}.xml"


def _is_no_data(xml_text: str) -> bool:
    root = _parse_xml(xml_text)
    if _local(root.tag) != "Acknowledgement_MarketDocument":
        return False
    return any(_local(e.tag) == "code" and (e.text or "").strip() == "999" for e in root.iter())


def _ack_reason(xml_text: str) -> str:
    try:
        root = _parse_xml(xml_text)
        texts = [(e.text or "").strip() for e in root.iter() if _local(e.tag) == "text"]
        return "; ".join(t for t in texts if t) or "no reason given"
    except ValueError:
        return "unparseable response"


def fetch_raw(
    query: Query,
    start: datetime,
    end: datetime,
    raw_dir: Path,
    token: str,
    session: requests.Session | None = None,
    refresh_empty: bool = False,
) -> tuple[str, bool]:
    """Return ``(xml_text, from_cache)``. Hits the API only when not cached.

    With ``refresh_empty``, cached "no matching data" answers are requested again
    (data may have been published since); real data is never re-downloaded.
    """
    path = cache_path(query, start, end, raw_dir)
    if path.exists():
        cached = path.read_text(encoding="utf-8")
        if not (refresh_empty and _is_no_data_safe(cached)):
            return cached, True

    session = session or requests.Session()
    params = {
        **query.params,
        "periodStart": _fmt_period(start),
        "periodEnd": _fmt_period(end),
        "securityToken": token,
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as exc:
            # The exception text contains the full URL, token included: drop it.
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"{query.name}: request failed ({type(exc).__name__})"
                ) from None
            time.sleep(2**attempt)
            continue

        if resp.status_code == 401:
            raise RuntimeError(f"{query.name}: HTTP 401, token rejected")
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"{query.name}: HTTP {resp.status_code} after {attempt} attempts")
            time.sleep(2**attempt)
            continue

        text = resp.text
        if resp.status_code == 200 or _is_no_data_safe(text):
            # Valid data, or an explicit "no matching data": cache both.
            _write_atomic(path, text)
            return text, False
        raise RuntimeError(
            f"{query.name}: HTTP {resp.status_code}: {_ack_reason(text)}"
        )
    raise RuntimeError(f"{query.name}: unreachable")  # pragma: no cover


def _is_no_data_safe(text: str) -> bool:
    try:
        return _is_no_data(text)
    except ValueError:
        return False


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML: {exc}") from None


def _child_text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return None


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def _parse_resolution(s: str) -> timedelta:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", s)
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(f"unsupported resolution {s!r}")
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


def _variable_name(query: Query, series: ET.Element) -> str:
    if query.variable:
        return query.variable
    psr_el = _child(series, "MktPSRType")
    psr = _child_text(psr_el, "psrType") if psr_el is not None else None
    tech = PSR_NAMES.get(psr, (psr or "unknown").lower())
    if query.kind == "generation":
        # outBiddingZone = the unit consumes (e.g. pumping), inBiddingZone = generation.
        if _child(series, "outBiddingZone_Domain.mRID") is not None:
            return f"consumption_{tech}"
        return f"generation_{tech}"
    if query.kind == "load_actual":
        return "load_actual"
    if query.kind == "load_forecast":
        return "load_forecast_day_ahead"
    if query.kind == "res_forecast":
        return f"{tech}_forecast_day_ahead"
    if query.kind == "price":
        return "price_day_ahead"
    raise ValueError(f"unknown kind {query.kind!r}")


def _unit(series: ET.Element) -> str:
    if _child(series, "currency_Unit.name") is not None:
        cur = _child_text(series, "currency_Unit.name")
        per = _child_text(series, "price_Measure_Unit.name") or ""
        return f"{cur}/{UNIT_NAMES.get(per, per)}"
    raw = _child_text(series, "quantity_Measure_Unit.name") or ""
    return UNIT_NAMES.get(raw, raw)


COLUMNS = ["ts_utc", "country", "variable", "value", "unit", "resolution_min"]


def parse_document(xml_text: str, query: Query) -> pd.DataFrame:
    """Turn one ENTSO-E XML document into long-format rows."""
    root = _parse_xml(xml_text)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        if _is_no_data(xml_text):
            return pd.DataFrame(columns=COLUMNS)
        raise ValueError(f"{query.name}: API error: {_ack_reason(xml_text)}")

    rows: list[tuple] = []
    for series in root:
        if _local(series.tag) != "TimeSeries":
            continue
        variable = _variable_name(query, series)
        unit = _unit(series)
        curve = _child_text(series, "curveType")  # A01 fixed blocks, A03 variable blocks

        for period in series:
            if _local(period.tag) != "Period":
                continue
            interval = _child(period, "timeInterval")
            p_start = _parse_time(_child_text(interval, "start"))
            p_end = _parse_time(_child_text(interval, "end"))
            res = _parse_resolution(_child_text(period, "resolution"))
            n_points = int((p_end - p_start) / res)

            values: dict[int, float] = {}
            for pt in period:
                if _local(pt.tag) != "Point":
                    continue
                pos = int(_child_text(pt, "position"))
                val = _child_text(pt, "quantity")
                if val is None:
                    val = _child_text(pt, "price.amount")
                values[pos] = float(val)

            prev = None
            for pos in range(1, n_points + 1):
                if pos in values:
                    prev = values[pos]
                elif curve == "A03" and prev is not None:
                    pass  # variable block: an omitted point repeats the previous one
                else:
                    continue  # genuinely missing
                rows.append((p_start + (pos - 1) * res, query.country, variable,
                             prev, unit, int(res.total_seconds() // 60)))

    df = pd.DataFrame(rows, columns=COLUMNS)
    if df.empty:
        return df
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)

    key = ["ts_utc", "country", "variable"]
    dup = df.duplicated(key, keep=False)
    if dup.any():
        conflicts = df[dup].groupby(key)["value"].nunique().gt(1).sum()
        if conflicts:
            warnings.warn(f"{query.name}: {conflicts} timestamps with conflicting values; keeping the last",
                          stacklevel=2)
        df = df.drop_duplicates(key, keep="last")
    return df.sort_values(key).reset_index(drop=True)


# --------------------------------------------------------------------------
# DuckDB
# --------------------------------------------------------------------------

def ensure_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            ts_utc TIMESTAMPTZ,
            country VARCHAR,
            variable VARCHAR,
            value DOUBLE,
            unit VARCHAR,
            resolution_min INTEGER,
            PRIMARY KEY (ts_utc, country, variable)
        )
        """
    )


def upsert(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> None:
    if df.empty:
        return
    con.register("incoming", df)
    try:
        cols = ", ".join(COLUMNS)
        con.execute(f"INSERT OR REPLACE INTO {TABLE} ({cols}) SELECT {cols} FROM incoming")
    finally:
        con.unregister("incoming")


def load(
    start: datetime,
    end: datetime,
    db_path: Path = DEFAULT_DB_PATH,
    raw_dir: Path = DEFAULT_RAW_DIR,
    token: str | None = None,
    session: requests.Session | None = None,
    queries: list[Query] | None = None,
    refresh_empty: bool = False,
) -> None:
    token = token or get_token()
    queries = queries if queries is not None else build_queries()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("SET TimeZone = 'UTC'")
        ensure_table(con)
        for q in queries:
            xml_text, cached = fetch_raw(q, start, end, raw_dir, token, session, refresh_empty)
            df = parse_document(xml_text, q)
            upsert(con, df)
            res = sorted(df["resolution_min"].unique().tolist()) if not df.empty else []
            print(
                f"{q.name:<22} {'cache' if cached else 'API  '} | {len(df):>6} rows | "
                f"{df['variable'].nunique() if not df.empty else 0:>2} variables | resolution(min) {res}"
            )
        total = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        print(f"Table {TABLE} in {db_path}: {total} rows in total")
    finally:
        con.close()


def _date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", type=_date, default="2025-04-01", help="UTC date, inclusive")
    parser.add_argument("--end", type=_date, default="2025-05-01", help="UTC date, exclusive")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--refresh-empty", action="store_true",
                        help="re-request cached 'no matching data' answers")
    args = parser.parse_args(argv)
    start = args.start if isinstance(args.start, datetime) else _date(args.start)
    end = args.end if isinstance(args.end, datetime) else _date(args.end)
    if end <= start:
        parser.error("--end must be after --start")
    load(start, end, args.db, args.raw_dir, refresh_empty=args.refresh_empty)
    return 0


if __name__ == "__main__":
    sys.exit(main())
