# Gridiron Desk

A personal tool for preparing for one fantasy football league: a 16-team PPR
redraft league on Yahoo. It pulls public NFL data into a local Postgres
database and recalibrates it against that league's actual scoring settings and
roster requirements.

The premise is narrow on purpose. Almost all public fantasy analysis — rankings,
tiers, "replacement level," start/sit advice — is implicitly calibrated for
10- and 12-team leagues. In a 16-team league the math is different: many more
starters are drawn from the same player pool, so replacement level sits much
lower, positional scarcity is sharper, and waiver wire value shifts. A running
back who is a fringe flex in a 12-team league can be a locked-in starter in a
16-team one. This project exists to take public numbers and re-express them for
the league I actually play in, rather than eyeballing the adjustment every week.

## Status

- Single-user. Built and run by one person for one league.
- Non-commercial. No revenue, no ads, no paid tier, nothing for sale.
- Not published and not distributed. There is no hosted site, no app, no
  package release. This repository is the whole project.
- Low volume. It syncs a handful of public endpoints on a nightly schedule and
  caches large, static payloads to disk instead of re-requesting them.

## Data sources

| Source        | Auth required     | Status                                             |
|---------------|-------------------|----------------------------------------------------|
| Sleeper       | None              | Working — player list / ID crosswalk ingested      |
| nflverse      | None              | Working — weekly player stats ingested             |
| FantasyPros   | API key           | Key requested; ingestion not built yet             |
| Yahoo Fantasy | OAuth 2.0         | Access pending approval; no integration code exists |

Sleeper and nflverse are fully public and require no credentials. FantasyPros
requires an API key, which has been requested. Yahoo Fantasy access is pending
approval — there is deliberately no Yahoo code in this repository yet, and none
of the code here calls Yahoo. Once (and only if) access is granted, league
scoring and roster settings will be read through the Yahoo Fantasy API to drive
the recalibration; until then those settings are configured by hand.

## Architecture

The shape is straightforward: ingestion scripts pull from public sources into
Postgres, and analysis reads back out of Postgres.

```
public sources ──> ingestion (Python) ──> Postgres ──> analysis / recalibration
  Sleeper                                   players
  nflverse                                  stats
  (FantasyPros, Yahoo: planned)             signals
```

- **Ingestion** is a set of small Python scripts under `ingest/`. Each fetches
  one source, with exponential-backoff retries and request logging, and upserts
  into a table. They are idempotent — re-running updates rows in place.
- **Postgres** is the single source of truth. `players` is the ID crosswalk
  (Sleeper's player list maps each player to their id on other platforms, so
  `players.gsis_id = stats.player_id` joins roster data to performance data).
  `stats` holds weekly box-score numbers. `signals` is a normalized table for
  expert opinions, ready for FantasyPros ingestion later.
- **Scheduling and caching.** Syncs are meant to run nightly (e.g. via cron).
  Static or large payloads are cached to disk under `data/` and reused rather
  than re-requested: the Sleeper player list (~15 MB) is fetched at most once a
  day, and completed-season nflverse files are cached indefinitely. This keeps
  the request volume low and predictable.

## What runs today

Working now:

- `ingest/sleeper_players.py` — fetches Sleeper's full NFL player list, caches
  it to disk, and upserts ~12k players (including the cross-platform id
  crosswalk) into `players`.
- `ingest/nflverse_stats.py` — pulls nflverse weekly player-stats CSVs from
  their public GitHub data releases and upserts the PPR-relevant columns into
  `stats`.
- `schema.sql` — the full database schema, including the `signals` table.

Not built yet (in rough order):

- Recalibration logic: compute replacement level and positional tiers from this
  league's scoring/roster settings instead of generic defaults.
- FantasyPros ingestion into `signals` (waiting on the API key).
- Yahoo Fantasy integration to read live league settings (waiting on API
  access approval).
- A Next.js draft-board UI on top of the recalibrated data.

## Auction draft boards (no database)

`build_board.py` + the `board.html` template are standalone: no Postgres, no
`players` table. League settings (teams, budget, roster, scoring, team names)
live in `leagues/<league>.json`; everything — slot eligibility, replacement
ranks, the scoring function — derives from that file.

```bash
pip install requests
python build_board.py --league papi-chulo   # -> board_data_papi-chulo.json, board_papi-chulo.html
python build_board.py --league the-league   # -> board_data_the-league.json, board_the-league.html
# --force re-fetches sources (cached under data/)
# optional: FANTASYPROS_API_KEY=... adds ECR
# optional: data/league_history.json [{player_name, price, manager, year}] adds manager tendencies
```

Points come from Sleeper 2026 season projections scored through the league's
own settings (including sacks taken, pick-sixes thrown, and both fumble
fields), with a 2023-25 historical rank curve as fallback; market ranks from
FantasyCalc, Fantasy Football Calculator ADP, and Sleeper ADP are kept
separate and turned into a per-player disagreement score, never blended.
Open the generated `board_<league>.html` from disk; it needs no network.
In the room: type `gib 47 kev` (player prefix, price, team prefix or `me`),
Enter; Ctrl+Z undoes; state persists in localStorage (namespaced per league)
and can be copied as JSON.

ADP data courtesy of [Fantasy Football Calculator](https://fantasyfootballcalculator.com).
Rankings include [WalterFootball](https://walterfootball.com)'s PPR cheat sheet.

## Setup

Requires Python 3.9+ and Postgres.

```bash
# 1. Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Create a database and load the schema
createdb gridiron
psql gridiron -f schema.sql

# 3. Configure connection
cp .env.example .env
# edit .env: set DATABASE_URL (or the PG* variables)

# 4. Run the ingestion
python -m ingest.sleeper_players       # players / crosswalk
python -m ingest.nflverse_stats        # current-season weekly stats
python -m ingest.nflverse_stats --seasons 2023 2024   # backfill
```

Both scripts read the database connection from `DATABASE_URL` (or the standard
`PGHOST`/`PGUSER`/`PGDATABASE`/... variables) and write cached payloads to
`DATA_DIR` (default `./data`, gitignored). Re-running is safe; the upserts keep
existing rows current.
