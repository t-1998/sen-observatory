# Data sources

Inventory of the public data used in `sen-observatory`, with known issues and the conventions applied when loading it.

## 1. REN Data Hub — [datahub.ren.pt](https://datahub.ren.pt)

| Item | Details |
|---|---|
| Access | Manual download as CSV from the website (no API used yet) |
| Resolution | 15 minutes |
| Units | MW (average power over each interval) |
| Timestamps | Portuguese local time; each row is the **start** of its 15-min interval |
| Coverage checked | 2025-03-30 (92 rows), 2025-04-21, 2025-04-28 and 2025-04-29 (96 rows each, 00:00–23:45), 2025-10-26 (100 rows). The 30 March and 26 October files are the daylight-saving days |

### Series available

| Series (site name) | Contents | Unit |
|---|---|---|
| Abastecimento do consumo – Repartição da produção | Hydro, wind, solar, biomass, wave, natural gas (CCGT), natural gas (cogeneration), coal, other thermal, imports, exports, pumping, battery injection, battery consumption, consumption | MW |
| Repartição de fontes de geração | Same generation sources as above, without consumption, storage and net imports | MW |
| Balanço energético | Daily production/consumption peaks by source and net imports (MW); daily energy by source and net imports (GWh) | MW, GWh |

### CSV format

- First 2 lines are metadata (units, access timestamp): skip them when reading.
- Comma separator, dot as decimal mark.
- Column names are in Portuguese with accents; renamed to English snake_case on load (`hydro`, `wind`, `solar`, `natural_gas`, `imports`, `exports`, `pumping`, `battery_discharge`, `battery_charge`, `consumption`, …).
- Imports and exports come in separate columns, both positive. Pumping and battery consumption are loads.

## 2. ENTSO-E Transparency Platform — [transparency.entsoe.eu](https://transparency.entsoe.eu)

| Item | Details |
|---|---|
| Access | RESTful API (`web-api.tp.entsoe.eu`) with a personal security token, called directly with `requests` and parsed with `xml.etree`. The `entsoe-py` library is not used (it does not expose the raw responses, and the resolution has to stay visible) |
| Token | `ENTSOE_API_TOKEN` in `.env` |
| Status | Loaded for April 2025 with `sen_observatory.entsoe_loader` |
| Area codes (EIC) | Portugal `10YPT-REN------W` (TSO: REN); Spain `10YES-REE------0` (TSO: REE) |
| Period loaded | 2025-04-01 00:00 to 2025-05-01 00:00 UTC for every series below. Also Portugal generation only for 2025-03-29/30 and 2025-10-25/26 (UTC), to cover the 30 March and 26 October REN days |
| Resolution | Varies by country and series; read from each response period and kept in `resolution_min`. In April 2025: Portugal 60 min for every series; Spain 15 min for actual generation, actual load and wind/solar forecasts, 60 min for prices and PT↔ES flows. PT↔ES flows are also available at 15 min on 2025-10-25/26. Not checked for other dates |
| Timestamps | UTC, as delivered. Day-ahead prices come aligned to the market day in CEST, so their rows span 2025-03-31 22:00 to 2025-05-01 21:00 UTC |
| Raw cache | `data/raw/entsoe/`: one XML per query, so the API is not asked twice. "No matching data" answers are cached too; `--refresh-empty` asks for them again |
| Table | `entsoe_timeseries` in `data/sen.duckdb`, long format: `ts_utc`, `country`, `variable`, `value`, `unit`, `resolution_min`; primary key `(ts_utc, country, variable)`, so reloading does not duplicate rows |

### Series downloaded

| Series | Countries | ENTSO-E document | Variable(s) in the table | Unit |
|---|---|---|---|---|
| Actual generation per production type | PT, ES | A75 / A16 | `generation_<type>` and `consumption_<type>` (units' own consumption, e.g. pumping) | MW |
| Actual total load | PT, ES | A65 / A16 | `load_actual` | MW |
| Day-ahead wind and solar forecast | PT, ES | A69 / A01 | `wind_onshore_forecast_day_ahead`, `solar_forecast_day_ahead`; PT also `wind_offshore_forecast_day_ahead` | MW |
| Day-ahead prices | PT, ES | A44 (day-ahead contract) | `price_day_ahead` | EUR/MWh |
| Day-ahead total load forecast | PT only | A65 / A01 | `load_forecast_day_ahead` | MW |
| Physical cross-border flows | PT→ES, ES→PT | A11 | `physical_flow_to_ES` (country `PT`), `physical_flow_to_PT` (country `ES`) | MW |

For flows, `country` is the country the power leaves.

## Possible future sources

- **REE e·sios API** (Red Eléctrica): possible alternative source for Spain during the 28–29 April 2025 gap in ENTSO-E. Requires its own token; not evaluated yet.

## Known issues

| Date found | Source | Issue | Handling |
|---|---|---|---|
| 2026-11-10 | REN Data Hub CSV | The cogeneration column duplicates the CCGT column on every row, on both days checked (2025-04-21 and 2025-04-28). The energy balance only closes with gas counted once (mean residual ~8 MW on 04-21 and ~14 MW on 04-28 before the blackout, against ~770 MW and ~350 MW if counted twice). Site figures for 2025-04-21 show cogeneration at a 3 MW daily peak and 0 GWh daily energy (rounded), so the single value is in practice CCGT; any real cogeneration is below the balance residual. | The cogeneration column is dropped and gas is kept as a single `natural_gas` series, counted once. |
| 2026-11-11 | ENTSO-E API, Spain (actual series) | No Spanish data from 2025-04-28 10:30 UTC (last point 10:15) to 2025-04-29 22:00 UTC: 35.5 h, 142 points at 15 min. The gap is in the raw XML (the actual-load response holds two separate periods, ending 04-28 10:30 and restarting 04-29 22:00) and affects all 22 actual 15-min series (generation and consumption per type, and load). Day-ahead forecasts, prices and flows are complete. It coincides with the 28 April 2025 blackout; why the data is missing was not verified. | Not filled or interpolated: the rows are absent. Spanish actuals cannot be used for that window. Portuguese data (REN and ENTSO-E) are complete for the same period. |
| 2026-11-11 | REN Data Hub CSV vs ENTSO-E (PT) | REN `Hídrica` includes the turbine generation of pumped-storage (reversible) plants; the pumping appears separately as a load (`pumping`). The typical cycle is to pump off-peak and generate at peak. On 2025-03-30, 04-21, 04-28 and 10-26, REN hydro minus ENTSO-E hydro (run-of-river + reservoir) equals ENTSO-E `generation_hydro_pumped_storage` to within 0.1 MW on the daily mean (444.1, 1307.2, 315.7 and 409.9 vs 410.0 MW). With pumped storage included, total PT generation agrees within 1.7 MW on the daily mean (largest hourly difference 16.7 MW) and every other technology within 0.1 MW. Two sources: how pumped-storage plants operate (pump, then generate), and this comparison with the ENTSO-E "Hydro Pumped Storage" category. | `ren_generation.hydro` is kept as published. When comparing with ENTSO-E, add `generation_hydro_pumped_storage` to ENTSO-E hydro. |
| 2026-11-11 | ENTSO-E API | The API accepts a well-formed but wrong EIC code without an error and answers "No matching data found" (reason code 999). The first version used `10YES-REN------W` for Spain (REN is the Portuguese TSO; Spain's is REE), so every Spanish series and the PT↔ES flows came back empty while the data existed on the website. After the fix, the Spanish day-ahead price for 2025-04-21 at 12:00 Lisbon time is 8.12 EUR/MWh, as seen on the website. | Codes corrected in `AREAS` (Spain `10YES-REE------0`). `tests/test_entsoe_loader.py::test_area_codes` pins both codes, and another test checks that prices use the same zone in `in_Domain` and `out_Domain`. A country that returns nothing at all should be treated as suspect. |
| 2026-11-11 | REN Data Hub CSV vs ENTSO-E (PT) | ENTSO-E `consumption_hydro_pumped_storage` is higher than REN `pumping` on all 4 days: +245.9, +75.1, +115.7 and +214.8 MWh/day (0.8–2.2 %; 2025-03-30, 04-21, 04-28, 10-26). Hour by hour the difference is always positive (smallest +0.3 MW): mean +2.0 MW in the 31 hours where REN pumping is below 1 MW, +4.7 MW when REN pumping is 1–500 MW and +11.6 MW (about 0.8 %) when it is 500 MW or more. **Main hypothesis (not verified):** the difference is the plants' auxiliary consumption, counted in one source and not in the other, consistent with how pumped-storage plants operate. The pattern is consistent with it (a gap even when REN shows no pumping, larger when the plants run) but does not prove it. | No correction. Pumping is compared as is (`compared` in `pt_component_map.sql`) and the difference is reported. |
| 2026-11-11 | ENTSO-E API (PT↔ES physical flows) vs REN Data Hub | ENTSO-E physical flows are offset by 15 min against REN imports/exports: the value stamped at T equals the REN value at T−15 min. Evidence: (a) on 2025-10-26, where the flows come at 15 min, ENTSO-E at T+15 min vs REN at T gives a mean absolute error of 10.2 MW and correlation 0.9991, against 152.4 MW and 0.9532 with the same stamp; (b) on the hourly-flow days (2025-03-30, 04-21, 04-28), the ENTSO-E hour vs the REN mean over the 4 quarters starting 15 min earlier gives 13.8 MW and 0.9989, against 128.6 MW and 0.9913 with the aligned hour; (c) hour shifts of ±1 h or ±2 h are clearly worse (pooled mean absolute error 347–899 MW vs 121.6 MW at zero shift). Raw XML checked by hand (`ES_flow_to_PT`, 2025-10-26): `timeInterval` starts 2025-10-25T00:00Z, resolution PT15M, and position 162 (3719.4) maps to 16:15Z, the same value and stamp as in `entsoe_timeseries`; so `entsoe_loader` converts positions correctly. The offset is 15 min on both 15-min and hourly data, whereas a position error would shift by one resolution unit (1 h on hourly data). Generation and load from ENTSO-E match REN without any offset, and the REN balance closes (residual below 50 MW) even on ramps above 2000 MW in 15 min, so REN flows are in step with the rest of the REN data. **Exception:** on 2025-04-28, local hour 11:00–12:00 (the blackout hour), the values match with no offset (1451.3 MW in both) and the 15-min alignment breaks (+660 MW). Cause unknown; not verified with REN. | No correction in the raw table: `entsoe_timeseries` keeps the values as published. For Portugal's interconnection balance the main source is REN (see *Conventions*). |

## Conventions

- **Time:** all timestamps converted to UTC on load. Local time breaks on daylight-saving days (92 or 100 rows instead of 96).
- **Validation:** every file loaded is checked against the energy balance:
  `generation + imports − exports − pumping − battery charge ≈ consumption`.
  Rows with a deviation above 50 MW are flagged.
- **Renewable share:** REN `hydro` includes pumped-storage turbine generation (see *Known issues*). Part of that energy comes from pumping, which may have been imported or non-renewable, so counting all hydro as renewable overestimates the renewable share.
- **Interconnection balance:** for Portugal, the main source is REN (imports − exports). ENTSO-E is used for the split by direction and for Spain. ENTSO-E physical flows are offset by 15 min against REN (see *Known issues*).

## Known limits

15-minute averages show the system conditions before the 28 April 2025 blackout and the restoration afterwards. They do not show the collapse itself, which happened within seconds. For those dynamics, see the ENTSO-E Expert Panel report on the 28 April 2025 blackout.
