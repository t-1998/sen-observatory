# sen-observatory

A learning project for exploring public data on the Portuguese electricity system (SEN — *Sistema Elétrico Nacional*).

It loads 15-minute generation and consumption data from the [REN Data Hub](https://datahub.ren.pt) and, later, forecasts, prices and Iberian data from the [ENTSO-E Transparency Platform](https://transparency.entsoe.eu). The data is normalised to UTC, checked against the energy balance and analysed in Python (pandas, DuckDB) — including the system conditions around the 28 April 2025 Iberian blackout.

See [SOURCES.md](SOURCES.md) for the data inventory, known issues and loading conventions.

## Setup

```bash
uv sync
```

Create a `.env` file in the project root with your ENTSO-E token:

```
ENTSOE_API_TOKEN=your-token-here
```

Raw data lives in `data/` and is never committed.
