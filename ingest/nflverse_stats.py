"""Ingest weekly player stats from nflverse into the `stats` table.

nflverse publishes tidy CSVs as GitHub release assets (no auth, no key). This
pulls the weekly player-stats file for one or more seasons and upserts the
PPR-relevant columns. nflverse's own `player_id` is the NFL GSIS id, which is
Sleeper's `gsis_id`, so these rows join to `players` on players.gsis_id.

Season data for a completed year is static, so each season's CSV is cached to
disk; the in-progress season is small and re-fetched (pass --force to refetch
all). Season is derived from the date and can be overridden.

Run:
    python -m ingest.nflverse_stats                 # current season
    python -m ingest.nflverse_stats --seasons 2023 2024
    python -m ingest.nflverse_stats --force
"""

import argparse
import csv
import datetime
import io
import os
import sys

from psycopg2.extras import execute_values

from .common import DATA_DIR, db_connect, http_get, log

# The "player_stats" release, current file format: stats_player_week_<season>.csv
RELEASE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/stats_player_week_{season}.csv"
)


def current_season(today=None):
    """NFL seasons span a calendar year but are named by the starting year.
    Before September, the most recent completed/ongoing season is last year.
    """
    today = today or datetime.date.today()
    return today.year if today.month >= 9 else today.year - 1


def _cache_path(season):
    return os.path.join(DATA_DIR, f"nflverse_stats_player_week_{season}.csv")


def load_season_csv(season, force=False):
    """Return the raw CSV text for a season, from disk cache when present."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _cache_path(season)

    if not force and os.path.exists(path):
        log.info("using cached nflverse stats for %d: %s", season, path)
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    text = http_get(RELEASE_URL.format(season=season), expect="text")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    log.info("cached nflverse stats for %d (%d bytes) to %s",
             season, len(text), path)
    return text


def _to_int(v):
    if v in (None, "", "NA"):
        return None
    try:
        return int(float(v))  # some counts arrive as "3.0"
    except ValueError:
        return None


def _to_num(v):
    if v in (None, "", "NA"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _row(rec):
    """Map one nflverse CSV row (dict) to a stats-table row tuple."""
    return (
        rec.get("player_id"),
        _to_int(rec.get("season")),
        _to_int(rec.get("week")),
        rec.get("season_type") or "REG",
        rec.get("player_display_name") or rec.get("player_name"),
        rec.get("position"),
        rec.get("team"),
        rec.get("opponent_team"),
        _to_int(rec.get("completions")),
        _to_int(rec.get("attempts")),
        _to_num(rec.get("passing_yards")),
        _to_int(rec.get("passing_tds")),
        _to_int(rec.get("passing_interceptions")),
        _to_int(rec.get("passing_2pt_conversions")),
        _to_int(rec.get("carries")),
        _to_num(rec.get("rushing_yards")),
        _to_int(rec.get("rushing_tds")),
        _to_int(rec.get("rushing_2pt_conversions")),
        _to_int(rec.get("receptions")),
        _to_int(rec.get("targets")),
        _to_num(rec.get("receiving_yards")),
        _to_int(rec.get("receiving_tds")),
        _to_int(rec.get("receiving_2pt_conversions")),
        _to_int(rec.get("sack_fumbles_lost")),
        _to_int(rec.get("rushing_fumbles_lost")),
        _to_int(rec.get("receiving_fumbles_lost")),
        _to_num(rec.get("fantasy_points")),
        _to_num(rec.get("fantasy_points_ppr")),
    )


UPSERT = """
INSERT INTO stats (
    player_id, season, week, season_type,
    player_name, position, team, opponent,
    completions, attempts, passing_yards, passing_tds, passing_interceptions, passing_2pt,
    carries, rushing_yards, rushing_tds, rushing_2pt,
    receptions, targets, receiving_yards, receiving_tds, receiving_2pt,
    sack_fumbles_lost, rushing_fumbles_lost, receiving_fumbles_lost,
    fantasy_points, fantasy_points_ppr,
    captured_at
) VALUES %s
ON CONFLICT (player_id, season, week, season_type) DO UPDATE SET
    player_name            = EXCLUDED.player_name,
    position               = EXCLUDED.position,
    team                   = EXCLUDED.team,
    opponent               = EXCLUDED.opponent,
    completions            = EXCLUDED.completions,
    attempts               = EXCLUDED.attempts,
    passing_yards          = EXCLUDED.passing_yards,
    passing_tds            = EXCLUDED.passing_tds,
    passing_interceptions  = EXCLUDED.passing_interceptions,
    passing_2pt            = EXCLUDED.passing_2pt,
    carries                = EXCLUDED.carries,
    rushing_yards          = EXCLUDED.rushing_yards,
    rushing_tds            = EXCLUDED.rushing_tds,
    rushing_2pt            = EXCLUDED.rushing_2pt,
    receptions             = EXCLUDED.receptions,
    targets                = EXCLUDED.targets,
    receiving_yards        = EXCLUDED.receiving_yards,
    receiving_tds          = EXCLUDED.receiving_tds,
    receiving_2pt          = EXCLUDED.receiving_2pt,
    sack_fumbles_lost      = EXCLUDED.sack_fumbles_lost,
    rushing_fumbles_lost   = EXCLUDED.rushing_fumbles_lost,
    receiving_fumbles_lost = EXCLUDED.receiving_fumbles_lost,
    fantasy_points         = EXCLUDED.fantasy_points,
    fantasy_points_ppr     = EXCLUDED.fantasy_points_ppr,
    captured_at            = now();
"""

_TEMPLATE = "(" + ",".join(["%s"] * 28) + ", now())"


def upsert_season(text):
    reader = csv.DictReader(io.StringIO(text))
    rows = [_row(rec) for rec in reader if rec.get("player_id")]
    if not rows:
        return 0
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT, rows, template=_TEMPLATE, page_size=1000)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest nflverse weekly stats.")
    parser.add_argument("--seasons", type=int, nargs="+",
                        help="seasons to ingest (default: current season)")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch even if a cached CSV exists")
    args = parser.parse_args(argv)

    seasons = args.seasons or [current_season()]
    total = 0
    for season in seasons:
        text = load_season_csv(season, force=args.force)
        n = upsert_season(text)
        log.info("season %d: upserted %d weekly rows", season, n)
        total += n
    log.info("done: %d rows across %d season(s)", total, len(seasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
