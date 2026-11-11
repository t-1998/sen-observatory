"""First analysis of the 28 April 2025 blackout in Portugal.

REN Data Hub (15 min) is the main source, for 28 and 29 April. ENTSO-E (hourly) is used only
for the April weekday average. Describes what the data show; no causes.

Usage:
    uv run python -m sen_observatory.analysis.blackout
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, time, timedelta
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from sen_observatory import comparison  # noqa: E402

DEFAULT_DB_PATH = Path("data/sen.duckdb")
DEFAULT_FIGURES_DIR = Path("figures")

EVENT_DAY = date(2025, 4, 28)
REFERENCE_DAY = date(2025, 4, 21)
RESTORATION_END_DAY = date(2025, 4, 29)  # last day of REN data used for the recovery
# Portuguese public holidays in April 2025 (Good Friday, Freedom Day).
APRIL_HOLIDAYS = (date(2025, 4, 18), date(2025, 4, 25))
# Left out of the April weekday average: the holidays, the event day and the day after it.
APRIL_EXCLUDED_DAYS = (*APRIL_HOLIDAYS, EVENT_DAY, date(2025, 4, 29))
LAST_FULL_INTERVAL = time(11, 15)
FIRST_AFFECTED_INTERVAL = time(11, 30)
THRESHOLDS_PCT = (25, 50, 75, 90)
RETURN_LIMIT_MW = 50.0
FIRST_PRODUCTION_MW = 5.0  # lower threshold: first sign of production after the drop

TECHNOLOGIES = ("hydro", "wind", "solar", "biomass", "natural_gas", "other_thermal")
TABLE_COMPONENTS = (*TECHNOLOGIES, "net_interconnection", "pumping", "consumption")
# Descriptive groups (by technology). Pumping groups are synchronous machines running as motors:
# they are a load and are not part of synchronous generation.
SYNCHRONOUS = ("hydro", "natural_gas", "biomass", "other_thermal")
INVERTER_BASED = ("wind", "solar")
GROUPS = {"synchronous_generation": SYNCHRONOUS, "inverter_based_generation": INVERTER_BASED}
TABLE_ROWS = (*TABLE_COMPONENTS, *GROUPS)
LABELS = {
    "hydro": "Hydro",
    "wind": "Wind",
    "solar": "Solar",
    "biomass": "Biomass",
    "natural_gas": "Natural gas",
    "other_thermal": "Other thermal",
    "net_interconnection": "Net imports",
    "pumping": "Pumping (load)",
    "consumption": "Consumption",
    "synchronous_generation": "Synchronous generation (hydro + gas + biomass + other thermal)",
    "inverter_based_generation": "Inverter-based generation (wind + solar)",
}

# Fixed categorical slots (palette.md, light mode), assigned by entity in stacking order.
SERIES_COLORS = {
    "hydro": "#2a78d6",
    "wind": "#eb6834",
    "solar": "#1baf7a",
    "biomass": "#eda100",
    "natural_gas": "#e87ba4",
    "other_thermal": "#008300",
    "net_interconnection": "#6250d6",
    "pumping": "#e34948",
}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"


# --------------------------------------------------------------------------
# Pure helpers (tested in tests/test_blackout.py)
# --------------------------------------------------------------------------

def reference_days() -> list[date]:
    """April 2025 weekdays used as the 'normal' reference.

    Excludes the public holidays (18/04, 25/04), the event day (28/04) and the day after (29/04).
    """
    days = [date(2025, 4, d) for d in range(1, 31)]
    return [d for d in days if d.weekday() < 5 and d not in APRIL_EXCLUDED_DAYS]


def pct_of(value: float, consumption: float) -> float:
    return float("nan") if not consumption else 100.0 * value / consumption


def first_crossing(values: pd.Series, threshold: float, after: pd.Timestamp) -> pd.Timestamp | None:
    """First timestamp strictly after ``after`` whose value is >= threshold."""
    s = values[values.index > after].dropna()
    hit = s[s >= threshold]
    return None if hit.empty else hit.index[0]


def stays_above(values: pd.Series, since: pd.Timestamp, threshold: float) -> bool:
    s = values[values.index >= since].dropna()
    return bool((s >= threshold).all())


def return_above(values: pd.Series, limit: float, start: pd.Timestamp, inclusive: bool = False) -> dict:
    """When does a series come back above ``limit`` after the event?

    With ``inclusive`` the limit itself counts as back (value >= limit, "low" means < limit);
    otherwise it must be strictly above (value > limit, "low" means <= limit).

    * stayed_above: no low interval from ``start`` on (the minimum is reported);
    * returned:     first interval back after the first low one;
    * not_reached:  it was low and never came back in the data.
    """
    s = values[values.index >= start].dropna()
    low = s[s < limit] if inclusive else s[s <= limit]
    if low.empty:
        return {"status": "stayed_above", "minimum": float(s.min())}
    first_low = low.index[0]
    later = s[s.index > first_low]
    hit = later[later >= limit] if inclusive else later[later > limit]
    if hit.empty:
        return {"status": "not_reached", "first_low": first_low}
    return {"status": "returned", "time": hit.index[0], "first_low": first_low}


def hours_text(hours: float) -> str:
    """Hours rounded to the nearest half hour, e.g. 12.5 -> '12½', 12.0 -> '12'."""
    half = round(hours * 2) / 2
    whole = int(half)
    return f"{whole}½" if half - whole else f"{whole}"


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def load_ren_range(con: duckdb.DuckDBPyConnection, first: date, last: date) -> pd.DataFrame:
    """REN 15-min rows for local days first..last, indexed by local (naive) interval start."""
    df = con.execute(
        """
        SELECT timezone('Europe/Lisbon', ts_utc) AS local_ts,
               hydro, wind, solar, biomass, natural_gas, other_thermal, coal, wave,
               battery_discharge, pumping, consumption, imports - exports AS net_interconnection
        FROM ren_generation
        WHERE CAST(timezone('Europe/Lisbon', ts_utc) AS DATE) BETWEEN ? AND ?
        ORDER BY ts_utc
        """,
        [first, last],
    ).df()
    return df.set_index("local_ts")


def load_entsoe_hourly(con: duckdb.DuckDBPyConnection, first: date, last: date) -> pd.DataFrame:
    """ENTSO-E Portugal, hourly MW per component, indexed by local (naive) hour start."""
    comparison.load_component_map(con)
    long = con.execute(comparison.read_sql("entsoe_hourly_components_pt.sql"), [first, last]).df()
    return long.pivot(index="local_hour", columns="component", values="mw")


def april_weekday_reference(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, list[date]]:
    """Mean, min and max of each component over the reference days, local hour 11:00-12:00."""
    days = reference_days()
    hourly = load_entsoe_hourly(con, days[0], days[-1])
    at_11 = hourly[(hourly.index.hour == 11) & hourly.index.normalize().isin(pd.to_datetime(days))]
    return at_11.agg(["mean", "min", "max", "count"]).T, days


def with_groups(components: pd.Series) -> pd.Series:
    """Add the synchronous and inverter-based generation groups to a series of components."""
    out = components.copy()
    for name, members in GROUPS.items():
        out[name] = components[list(members)].sum()
    return out


def operating_point(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Table 1: each component in MW and % of consumption at 11:15 local."""
    ren = load_ren_range(con, REFERENCE_DAY, EVENT_DAY)
    at = pd.Timestamp.combine(EVENT_DAY, LAST_FULL_INTERVAL)
    at_ref = pd.Timestamp.combine(REFERENCE_DAY, LAST_FULL_INTERVAL)
    hour_start = pd.Timestamp.combine(REFERENCE_DAY, time(11, 0))
    ref_hour = ren.loc[hour_start : hour_start + timedelta(minutes=45)]
    assert len(ref_hour) == 4
    avg, days = april_weekday_reference(con)

    columns = {
        f"{EVENT_DAY:%d/%m} 11:15": ren.loc[at, list(TABLE_COMPONENTS)],
        f"{REFERENCE_DAY:%d/%m} 11:15": ren.loc[at_ref, list(TABLE_COMPONENTS)],
        f"{REFERENCE_DAY:%d/%m} 11:00-12:00 mean": ref_hour[list(TABLE_COMPONENTS)].mean(),
        f"April weekdays 11:00-12:00 mean (n={len(days)})": avg.loc[list(TABLE_COMPONENTS), "mean"],
    }
    columns = {name: with_groups(series) for name, series in columns.items()}
    out = {}
    for name, series in columns.items():
        out[(name, "MW")] = series
        out[(name, "% of consumption")] = series.map(lambda v, c=series["consumption"]: pct_of(v, c))
    table = pd.DataFrame(out)
    table.index = [LABELS[c] for c in table.index]
    return table


def event_frame(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Event-day and following-day REN series (15 min), 28/04 00:00 to 29/04 23:45."""
    frame = load_ren_range(con, EVENT_DAY, RESTORATION_END_DAY)[list(TABLE_COMPONENTS)]
    last = pd.Timestamp.combine(RESTORATION_END_DAY, time(23, 45))
    if frame.empty or frame.index.max() < last:
        ends = "no REN data" if frame.empty else f"REN data end at {frame.index.max():%d/%m %H:%M}"
        raise ValueError(
            f"{ends}; load the REN files for {EVENT_DAY:%d/%m} to {RESTORATION_END_DAY:%d/%m} "
            "(data/raw/ren/) before running the recovery analysis"
        )
    return frame


def reference_consumption(con: duckdb.DuckDBPyConnection, frame: pd.DataFrame) -> pd.Series:
    """Consumption of the reference day at the same time of day."""
    ref = load_ren_range(con, REFERENCE_DAY, REFERENCE_DAY)["consumption"]
    by_time = {t.time(): v for t, v in ref.items()}
    return pd.Series([by_time[ts.time()] for ts in frame.index], index=frame.index)


def restoration(con: duckdb.DuckDBPyConnection) -> dict:
    """Consumption in % of the reference day, threshold crossings and technology returns."""
    frame = event_frame(con)
    frame = frame[frame.index >= pd.Timestamp.combine(EVENT_DAY, FIRST_AFFECTED_INTERVAL)]
    frame = frame.assign(reference=reference_consumption(con, frame))
    frame["pct"] = 100.0 * frame["consumption"] / frame["reference"]
    trough = frame["pct"].idxmin()

    crossings = {}
    for thr in THRESHOLDS_PCT:
        ts = first_crossing(frame["pct"], thr, trough)
        crossings[thr] = None if ts is None else {
            "time": ts,
            "pct": float(frame.loc[ts, "pct"]),
            "stays_above": stays_above(frame["pct"], ts, thr),
        }

    before = pd.Timestamp.combine(EVENT_DAY, LAST_FULL_INTERVAL)
    event_day = load_ren_range(con, EVENT_DAY, EVENT_DAY)

    def returns_for(limit: float, inclusive: bool) -> dict:
        out = {}
        for comp in (*TECHNOLOGIES, "net_interconnection", "pumping"):
            pre = float(event_day.loc[before, comp])
            if (pre < limit) if inclusive else (pre <= limit):
                out[comp] = {"status": "below_before_event", "pre_event_mw": pre}
            else:
                out[comp] = return_above(frame[comp], limit, frame.index[0], inclusive)
        return out

    returns = returns_for(RETURN_LIMIT_MW, inclusive=False)
    returns_5 = returns_for(FIRST_PRODUCTION_MW, inclusive=True)
    return {"frame": frame, "trough": trough, "crossings": crossings, "returns": returns, "returns_5": returns_5}


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------

def _style_axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=INK_MUTED, labelsize=9, length=3, width=0.6)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _step(index: pd.DatetimeIndex, minutes: int = 15):
    return list(index) + [index[-1] + timedelta(minutes=minutes)]


def _ext(values) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    return np.append(v, v[-1])


def _draw_day(ax, day: pd.DataFrame, title: str, ymax: float, ymin: float, mark_event: bool) -> None:
    x = _step(day.index)
    net = day["net_interconnection"]
    layers_pos = [(c, day[c].clip(lower=0)) for c in TECHNOLOGIES] + [("net_interconnection", net.clip(lower=0))]
    layers_neg = [("pumping", day["pumping"]), ("net_interconnection", (-net).clip(lower=0))]

    base = np.zeros(len(day))
    for comp, vals in layers_pos:
        top = base + vals.to_numpy()
        ax.fill_between(x, _ext(base), _ext(top), step="post", facecolor=SERIES_COLORS[comp],
                        edgecolor=SURFACE, linewidth=0.7, zorder=2)
        base = top
    base = np.zeros(len(day))
    for comp, vals in layers_neg:
        bottom = base - vals.to_numpy()
        ax.fill_between(x, _ext(base), _ext(bottom), step="post", facecolor=SERIES_COLORS[comp],
                        edgecolor=SURFACE, linewidth=0.7, zorder=2)
        base = bottom
    ax.step(x, _ext(day["consumption"]), where="post", color=INK, linewidth=1.5, zorder=4)
    ax.axhline(0, color=BASELINE, linewidth=0.8, zorder=3)

    if mark_event:
        t = pd.Timestamp.combine(EVENT_DAY, FIRST_AFFECTED_INTERVAL)
        ax.axvline(t, color=INK_SECONDARY, linewidth=0.8, zorder=5)
        ax.text(t + timedelta(minutes=12), ymax * 0.97, "11:30: first interval\nwith a drop",
                color=INK_SECONDARY, fontsize=8.5, va="top", ha="left", zorder=6)

    at = pd.Timestamp.combine(day.index[0].date(), LAST_FULL_INTERVAL)
    cons = day.loc[at, "consumption"]
    for k, (comp, text) in enumerate((
        ("net_interconnection", f"net imports {day.loc[at, 'net_interconnection']:,.0f} MW "
                                f"({pct_of(day.loc[at, 'net_interconnection'], cons):.0f} %)"),
        ("pumping", f"pumping {day.loc[at, 'pumping']:,.0f} MW ({pct_of(day.loc[at, 'pumping'], cons):.0f} %)"),
    )):
        y = 0.955 - 0.065 * (k + 1)
        ax.text(0.012, y + 0.005, "■", transform=ax.transAxes, color=SERIES_COLORS[comp], fontsize=9, va="center")
        ax.text(0.035, y, f"11:15  {text}", transform=ax.transAxes, color=INK, fontsize=8.8, va="center", zorder=6)

    start = pd.Timestamp.combine(day.index[0].date(), time(0, 0))
    ax.set_xlim(start, start + timedelta(hours=24))
    ax.set_ylim(ymin, ymax)
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.xaxis.set_major_formatter(plt.FuncFormatter(
        lambda v, _, d0=start.date(): (lambda t: "24:00" if t.date() > d0 else f"{t:%H:%M}")(mdates.num2date(v))))
    ax.set_title(title, loc="left", fontsize=11.5, color=INK, pad=8)
    _style_axes(ax)


def plot_operation(con: duckdb.DuckDBPyConnection, out_dir: Path) -> pd.DataFrame:
    """Figure 1: 28/04 and 21/04 side by side, 00:00-24:00 local."""
    days = {d: load_ren_range(con, d, d) for d in (EVENT_DAY, REFERENCE_DAY)}
    ymax = max(
        (d[list(TECHNOLOGIES)].clip(lower=0).sum(axis=1) + d["net_interconnection"].clip(lower=0)).max()
        for d in days.values()
    )
    ymin = min((-(d["pumping"] + (-d["net_interconnection"]).clip(lower=0))).min() for d in days.values())
    ymax, ymin = float(np.ceil(ymax * 1.08 / 1000) * 1000), float(np.floor(ymin * 1.08 / 1000) * 1000)

    max_residual = con.execute(
        "SELECT max(abs(balance_residual_mw)) FROM ren_generation "
        "WHERE CAST(timezone('Europe/Lisbon', ts_utc) AS DATE) IN (?, ?)", [EVENT_DAY, REFERENCE_DAY]
    ).fetchone()[0]

    fig, axes = plt.subplots(1, 2, figsize=(13, 6.8), sharey=True, dpi=200)
    fig.patch.set_facecolor(SURFACE)
    for ax, (d, df), is_event in zip(axes, days.items(), (True, False), strict=True):
        _draw_day(ax, df, f"{d:%A %d %B %Y}", ymax, ymin, is_event)
    axes[0].set_ylabel("MW", color=INK_SECONDARY, fontsize=9.5)
    axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    legend_labels = {**LABELS, "net_interconnection": "Net imports (below zero: net exports)"}
    handles = [Patch(facecolor=SERIES_COLORS[c], label=legend_labels[c]) for c in (*TECHNOLOGIES, "net_interconnection", "pumping")]
    handles.append(Line2D([0], [0], color=INK, linewidth=1.5, label="Consumption"))
    fig.legend(handles=handles, loc="lower center", ncol=5, frameon=False, fontsize=9,
               labelcolor=INK_SECONDARY, handlelength=1.4, columnspacing=1.4, bbox_to_anchor=(0.5, 0.075))
    fig.text(0.06, 0.965, operation_title(days[EVENT_DAY]), fontsize=13.5, color=INK, va="top", fontweight="bold")
    fig.text(0.06, 0.925, "Supply and load by 15-minute interval, 28 April vs 21 April 2025, local time. "
             "Above zero: generation by technology plus net imports. Below zero: pumping and net exports.",
             fontsize=9.5, color=INK_SECONDARY, va="top")
    fig.text(0.06, 0.898, "Line: consumption. Consumption = everything above zero minus everything below zero. "
             "Percentages: share of consumption.", fontsize=9.5, color=INK_SECONDARY, va="top")
    fig.text(0.06, 0.034, "Source: REN Data Hub. Hydro includes turbine generation of pumped-storage plants.",
             fontsize=8.3, color=INK_MUTED, va="bottom")
    fig.text(0.06, 0.010, "Coal, wave and battery storage are omitted: absent or negligible in the Portuguese system in this period. "
             f"The balance holds within the REN residual (at most {max_residual:.0f} MW).",
             fontsize=8.3, color=INK_MUTED, va="bottom")
    fig.subplots_adjust(left=0.06, right=0.985, top=0.83, bottom=0.23, wspace=0.07)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "fig1_operation_2025-04-28_vs_2025-04-21.png", facecolor=SURFACE)
    plt.close(fig)

    table = pd.concat({f"{d:%Y-%m-%d}": df for d, df in days.items()})
    table.index.names = ["day", "local_interval_start"]
    return table


def operation_title(day: pd.DataFrame) -> str:
    """Figure 1 headline: net imports and pumping as % of consumption at 11:15."""
    at = pd.Timestamp.combine(day.index[0].date(), LAST_FULL_INTERVAL)
    cons, net, pump = (float(day.loc[at, c]) for c in ("consumption", "net_interconnection", "pumping"))
    verb = "importing" if net >= 0 else "exporting"
    return (f"At 11:15 on {at:%d %B}, Portugal was {verb} {abs(pct_of(net, cons)):.0f} % of its consumption "
            f"and pumping {pct_of(pump, cons):.0f} %")


def restoration_title(res: dict) -> str:
    """Figure 2 headline: how long after the drop consumption first reached 90 % of the reference."""
    c = res["crossings"].get(90)
    if c is None:
        return "Consumption had not reached 90 % of a normal day by the end of the data"
    hours = (c["time"] - pd.Timestamp.combine(EVENT_DAY, FIRST_AFFECTED_INTERVAL)) / timedelta(hours=1)
    return f"Consumption reached 90 % of a normal day about {hours_text(hours)} hours after the drop"


def dip_note(res: dict) -> str:
    """Sentence for the figure 2 subtitle: does consumption fall back under 90 % after reaching it?"""
    c = res["crossings"].get(90)
    if c is None:
        return ""
    after = res["frame"][res["frame"].index > c["time"]]
    below = after[after["pct"] < 90]
    if below.empty:
        return ""
    low = below["pct"].idxmin()
    return (f" After that it is below 90 % in {len(below)} intervals, between {below.index[0]:%H:%M} and "
            f"{below.index[-1]:%H:%M} on {below.index[0]:%d/%m} (minimum {below['pct'].min():.1f} % at {low:%H:%M}).")


def _fmt_time(ts: pd.Timestamp) -> str:
    return f"{ts:%H:%M}" + ("" if ts.date() == EVENT_DAY else f" {ts:%d/%m}")


def plot_restoration(res: dict, out_dir: Path) -> pd.DataFrame:
    """Figure 2: consumption in % of 21/04 after the drop, with threshold times and technology returns."""
    frame, crossings, returns, returns_5 = res["frame"], res["crossings"], res["returns"], res["returns_5"]
    event_times = [c["time"] for c in crossings.values() if c] + [
        r["time"] for rs in (returns, returns_5) for r in rs.values() if r["status"] == "returned"
    ]
    x_end = max(max(event_times) + timedelta(hours=3), pd.Timestamp.combine(EVENT_DAY + timedelta(days=1), time(0, 0)))
    x0 = pd.Timestamp.combine(EVENT_DAY, time(11, 0))

    fig = plt.figure(figsize=(13, 7.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    gs = fig.add_gridspec(2, 1, height_ratios=[3.2, 2.0], hspace=0.28, left=0.145, right=0.975, top=0.86, bottom=0.12)
    ax = fig.add_subplot(gs[0])
    lane = fig.add_subplot(gs[1], sharex=ax)
    _style_axes(ax)
    _style_axes(lane)

    ax.step(_step(frame.index), _ext(frame["pct"]), where="post", color=INK, linewidth=1.5, zorder=4)
    for thr in THRESHOLDS_PCT:
        ax.axhline(thr, color=GRID, linewidth=0.6, zorder=1)
    for thr, c in crossings.items():
        if not c:
            continue
        ax.plot([c["time"]], [c["pct"]], marker="o", markersize=7, color=INK, markeredgecolor=SURFACE,
                markeredgewidth=1.6, zorder=6)
        ax.annotate(f"first \u2265 {thr} %: {_fmt_time(c['time'])}", (c["time"], c["pct"]), xytext=(9, -13),
                    textcoords="offset points", fontsize=9, color=INK, ha="left", va="top", zorder=7)
    ax.set_ylim(0, max(105.0, float(frame["pct"].max()) + 5))
    ax.set_yticks([0, 25, 50, 75, 90, 100])
    ax.set_ylabel("Consumption, % of 21 April\nat the same time of day", color=INK_SECONDARY, fontsize=9.5)
    plt.setp(ax.get_xticklabels(), visible=False)

    # lanes: hollow marker = first interval >= 5 MW after the drop, filled marker = first interval > 50 MW
    comps = [*TECHNOLOGIES, "net_interconnection", "pumping"]
    for i, comp in enumerate(comps):
        r50, r5 = returns[comp], returns_5[comp]
        y = len(comps) - 1 - i
        color = SERIES_COLORS[comp]
        same_time = (r5["status"] == "returned" and r50["status"] == "returned" and r5["time"] == r50["time"])
        if r5["status"] == "returned":
            lane.plot([r5["time"]], [y], marker="o", markersize=8, markerfacecolor=SURFACE, markeredgecolor=color,
                      markeredgewidth=2.0, linestyle="none", zorder=6)
            if not same_time:
                lane.text(r5["time"] - timedelta(minutes=14), y, _fmt_time(r5["time"]),
                          color=INK, fontsize=9, va="center", ha="right")
        if r50["status"] == "returned":
            lane.plot([r50["time"]], [y], marker="o", markersize=4 if same_time else 8, markerfacecolor=color,
                      markeredgecolor=color if same_time else SURFACE, markeredgewidth=0.0 if same_time else 1.6,
                      linestyle="none", zorder=7)
            lane.text(r50["time"] + timedelta(minutes=14), y, _fmt_time(r50["time"]),
                      color=INK, fontsize=9, va="center", ha="left")
        note = {"stayed_above": f"stayed above {RETURN_LIMIT_MW:.0f} MW (minimum {r50.get('minimum', 0):.0f} MW)",
                "below_before_event": f"never above {RETURN_LIMIT_MW:.0f} MW (was {r50.get('pre_event_mw', 0):.0f} MW at 11:15)",
                "not_reached": f"not above {RETURN_LIMIT_MW:.0f} MW through 29/04"}.get(r50["status"])
        if r50["status"] != "stayed_above" and r5["status"] == "stayed_above":
            note = f"minimum after the drop: {r5.get('minimum', 0):.0f} MW"
        if note:
            lane.text(x0 + timedelta(minutes=14), y, note, color=INK_SECONDARY, fontsize=9, va="center", ha="left")
    lane.set_yticks(range(len(comps)))
    lane.set_yticklabels([LABELS[c] for c in reversed(comps)], color=INK_SECONDARY, fontsize=9.5)
    lane.set_ylim(-0.7, len(comps) - 0.3)
    lane.yaxis.grid(True, color=GRID, linewidth=0.6)
    lane.set_xlim(x0, x_end)
    lane.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    lane.xaxis.set_major_formatter(plt.FuncFormatter(
        lambda v, _: (lambda t: f"{t:%H:%M}" + (f"\n{t:%d/%m}" if t.hour == 0 else ""))(mdates.num2date(v))))
    lane.set_title("After the drop: when each series comes back", loc="left", fontsize=10.5, color=INK, pad=6)
    lane.legend(
        handles=[
            Line2D([], [], marker="o", linestyle="none", markersize=8, markerfacecolor=INK, markeredgecolor=SURFACE,
                   markeredgewidth=1.6, label=f"first interval above {RETURN_LIMIT_MW:.0f} MW"),
            Line2D([], [], marker="o", linestyle="none", markersize=8, markerfacecolor=SURFACE, markeredgecolor=INK,
                   markeredgewidth=2.0, label=f"first interval at or above {FIRST_PRODUCTION_MW:.0f} MW"),
        ],
        loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=2, frameon=False, fontsize=9, labelcolor=INK_SECONDARY,
        handletextpad=0.4, columnspacing=1.6,
    )

    fig.text(0.06, 0.965, restoration_title(res), fontsize=13.5, color=INK, va="top", fontweight="bold")
    fig.text(0.06, 0.925, "Portugal, 28 April 2025. Consumption in % of 21 April at the same time of day; REN, 15 min; "
             "first interval at or above each level, local time (interval start).", fontsize=9.5,
             color=INK_SECONDARY, va="top")
    if dip_note(res):
        fig.text(0.06, 0.897, dip_note(res).strip(), fontsize=9.5, color=INK_SECONDARY, va="top")
    fig.text(0.06, 0.02, "Source: REN Data Hub. Reference: REN consumption on Monday 21 April at the same time "
             "of day (also for 29 April, a Tuesday).", fontsize=8.3, color=INK_MUTED, va="bottom")
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / "fig2_restoration_curve.png", facecolor=SURFACE)
    plt.close(fig)

    out = frame[["consumption", "reference", "pct"]].copy()
    out.index.name = "local_interval_start"
    return out


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _md(df: pd.DataFrame, digits: int = 0) -> str:
    return df.round(digits).to_string()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--figures-dir", type=Path, default=DEFAULT_FIGURES_DIR)
    args = parser.parse_args(argv)

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")
        table = operating_point(con)
        res = restoration(con)
        data1 = plot_operation(con, args.figures_dir)
        data2 = plot_restoration(res, args.figures_dir)
    finally:
        con.close()

    data1.to_csv(args.figures_dir / "fig1_operation_data.csv", float_format="%.1f")
    data2.to_csv(args.figures_dir / "fig2_restoration_data.csv", float_format="%.2f")

    print("== Operating point at 11:15 local (MW, % of consumption)")
    print(table.to_string(float_format=lambda v: f"{v:,.1f}"))
    print(f"\n== Trough of consumption after the drop: {res['trough']:%d/%m %H:%M}")
    print("== Consumption, % of 21/04 same time: first interval at or above each threshold (after the trough)")
    for thr, c in res["crossings"].items():
        if c is None:
            print(f"  {thr} %: not reached in the data")
        else:
            print(f"  {thr} %: {c['time']:%d/%m %H:%M} ({c['pct']:.1f} %, stays above: {c['stays_above']})")
    print(f"== First interval above {RETURN_LIMIT_MW:.0f} MW after the drop")
    for comp, r in res["returns"].items():
        when = f"{r['time']:%d/%m %H:%M}" if "time" in r else ""
        print(f"  {LABELS[comp]:<16} {r['status']:<20} {when}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
