# CLAUDE.md

Conventions for working on `sen-observatory`. Read `SOURCES.md` for the data inventory and known issues.

## Language
- Code, comments, docstrings, commit messages and README: **English**.
- Conversation with the user: **European Portuguese**.

## Data conventions
- **Time:** all timestamps are converted to **UTC** on load. REN data comes in Portuguese local time (interval start), which breaks on daylight-saving days.
- **Natural gas:** a single series, `natural_gas`, counted once. The REN cogeneration column duplicates CCGT and is dropped (see *Known issues* in `SOURCES.md`).
- **Validation:** check every loaded file against the energy balance and flag rows deviating more than 50 MW (see `SOURCES.md`).

## Secrets and data
- The ENTSO-E token is read from the `ENTSOE_API_TOKEN` variable in `.env`, loaded with `python-dotenv`. Never hard-code it or print it.
- **Never commit** `data/`, `*.duckdb` or `.env` (all are in `.gitignore`).

## Project layout
- `src/sen_observatory/` — Python package (managed with `uv`).
- `data/` — local raw and processed data (git-ignored).
- `tests/` — pytest suite (`uv run pytest`).

## Testing
- Run the tests with `uv run pytest`.
- **All tests must pass before any commit.** If a test fails, fix the cause (or the test, if it is wrong) before committing; never commit on red.
- Tests use synthetic CSVs so they run without `data/`. Tests on real files are skipped automatically when the files are missing.

## Working with the user
- The user is learning: for every file created, explain in 2–3 sentences what it is and why it exists.
- **Never commit** (or push) unless the user explicitly asks.
