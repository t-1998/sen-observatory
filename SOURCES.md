# Data sources

Inventory of the public data used in `sen-observatory`, with known issues and the conventions applied when loading it.

## 1. REN Data Hub — [datahub.ren.pt](https://datahub.ren.pt)

| Item | Details |
|---|---|
| Access | Manual download as CSV from the website (no API used yet) |
| Resolution | 15 minutes |
| Units | MW (average power over each interval) |
| Timestamps | Portuguese local time; each row is the **start** of its 15-min interval |
| Coverage checked | 2025-04-28 (96 rows, 00:00–23:45) |

### Series available

| Series (site name) | Contents | Unit |
|---|---|---|
| Abastecimento do consumo – Repartição da produção | Hydro, wind, solar, biomass, wave, natural gas (CCGT), natural gas (cogeneration), coal, other thermal, imports, exports, pumping, battery injection, battery consumption, consumption | MW |
| Repartição de fontes de geração | Same generation sources as above, without consumption, storage and net imports | MW |
| Balanço energético | Daily production/consumption peaks by source and net imports (MW); daily energy by source and net imports (GWh) | MW, GWh |

### CSV format

- First 2 lines are metadata (units, access timestamp): skip them when reading.
- Comma separator, dot as decimal mark.
- Column names are in Portuguese with accents; renamed to English snake_case on load (`hydro`, `wind`, `solar`, `ccgt`, `cogeneration`, `imports`, `exports`, `pumping`, `battery_discharge`, `battery_charge`, `consumption`, …).
- Imports and exports come in separate columns, both positive. Pumping and battery consumption are loads.

## 2. ENTSO-E Transparency Platform — [transparency.entsoe.eu](https://transparency.entsoe.eu)

| Item | Details |
|---|---|
| Access | RESTful API with personal security token |
| Status | Token requested — to be added in week 2 |

Planned series: day-ahead load, wind and solar forecasts (PT); day-ahead prices; generation and load for Spain, for an Iberian comparison.

## Known issues

| Date found | Source | Issue | Handling |
|---|---|---|---|
| 2026-11-10 | REN Data Hub CSV | The cogeneration column duplicates the CCGT column on every row of 2025-04-28. Site charts show zero cogeneration that day, and the energy balance only closes (mean error ~9 MW) with gas counted once. | Cogeneration set to 0 for 2025-04-28. To check whether the duplication also happens on other days. |

## Conventions

- **Time:** all timestamps converted to UTC on load. Local time breaks on daylight-saving days (92 or 100 rows instead of 96).
- **Validation:** every file loaded is checked against the energy balance:
  `generation + imports − exports − pumping − battery charge ≈ consumption`.
  Rows with a deviation above 50 MW are flagged.

## Known limits

15-minute averages show the system conditions before the 28 April 2025 blackout and the restoration afterwards. They do not show the collapse itself, which happened within seconds. For those dynamics, see the ENTSO-E Expert Panel report on the 28 April 2025 blackout.
