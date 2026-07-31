"""Ingest the Sleeper NFL player list into the `players` table.

Sleeper's /v1/players/nfl is the ID crosswalk for the whole project: each
player record carries that player's id on the other platforms (gsis for
nflverse, plus espn/yahoo/sportradar/...). It is a large payload (~15 MB) and
Sleeper explicitly asks callers to fetch it at most once a day, so this script
caches the raw JSON to disk and reuses today's copy instead of re-requesting.

Run:
    python -m ingest.sleeper_players            # use cache if fresh, else fetch
    python -m ingest.sleeper_players --force    # always re-fetch
"""

import argparse
import json
import os
import sys
import time

from psycopg2.extras import execute_values

from .common import DATA_DIR, db_connect, http_get, log

SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
CACHE_PATH = os.path.join(DATA_DIR, "sleeper_players_nfl.json")
MAX_CACHE_AGE_SECONDS = 24 * 60 * 60  # fetch at most daily


def _cache_is_fresh(path):
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < MAX_CACHE_AGE_SECONDS


def load_players(force=False):
    """Return the Sleeper player dict, from today's cache if available."""
    os.makedirs(DATA_DIR, exist_ok=True)

    if not force and _cache_is_fresh(CACHE_PATH):
        age_h = (time.time() - os.path.getmtime(CACHE_PATH)) / 3600
        log.info("using cached Sleeper players (%.1fh old): %s", age_h, CACHE_PATH)
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    data = http_get(SLEEPER_URL, expect="json")
    # write atomically so an interrupted run can't leave a half file behind
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, CACHE_PATH)
    log.info("cached %d players to %s", len(data), CACHE_PATH)
    return data


def _row(player):
    """Map one Sleeper player record to a players-table row tuple."""
    def s(v):
        # normalize the crosswalk ids to text; Sleeper mixes ints and strings
        return None if v is None else str(v)

    return (
        player.get("player_id"),
        player.get("full_name"),
        player.get("first_name"),
        player.get("last_name"),
        player.get("position"),
        player.get("team"),
        player.get("status"),
        player.get("age"),
        player.get("years_exp"),
        player.get("search_rank"),
        s(player.get("gsis_id")),
        s(player.get("espn_id")),
        s(player.get("yahoo_id")),
        s(player.get("sportradar_id")),
        s(player.get("rotowire_id")),
        s(player.get("fantasy_data_id")),
    )


UPSERT = """
INSERT INTO players (
    player_id, full_name, first_name, last_name, position, team, status,
    age, years_exp, search_rank,
    gsis_id, espn_id, yahoo_id, sportradar_id, rotowire_id, fantasy_data_id,
    updated_at
) VALUES %s
ON CONFLICT (player_id) DO UPDATE SET
    full_name       = EXCLUDED.full_name,
    first_name      = EXCLUDED.first_name,
    last_name       = EXCLUDED.last_name,
    position        = EXCLUDED.position,
    team            = EXCLUDED.team,
    status          = EXCLUDED.status,
    age             = EXCLUDED.age,
    years_exp       = EXCLUDED.years_exp,
    search_rank     = EXCLUDED.search_rank,
    gsis_id         = EXCLUDED.gsis_id,
    espn_id         = EXCLUDED.espn_id,
    yahoo_id        = EXCLUDED.yahoo_id,
    sportradar_id   = EXCLUDED.sportradar_id,
    rotowire_id     = EXCLUDED.rotowire_id,
    fantasy_data_id = EXCLUDED.fantasy_data_id,
    updated_at      = now();
"""


def upsert_players(players):
    # keep only real players (Sleeper includes team defenses keyed by abbrev,
    # which have no numeric player_id / position — skip anything without a
    # player_id and a position).
    rows = [
        _row(p)
        for p in players.values()
        if p.get("player_id") and p.get("position")
    ]
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            # execute_values appends the "now()" trailing column via template
            execute_values(cur, UPSERT, rows,
                           template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                                    "%s,%s,%s,%s,%s,%s, now())")
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest Sleeper NFL players.")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch even if today's cache exists")
    args = parser.parse_args(argv)

    players = load_players(force=args.force)
    log.info("loaded %d player records from Sleeper", len(players))
    n = upsert_players(players)
    log.info("upserted %d players", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
