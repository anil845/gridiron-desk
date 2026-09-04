"""Pull live standings + rosters from Yahoo for both leagues, write to seasons/.

    python yahoo_sync.py            # sync both leagues (needs approved fantasy scope)

Writes per league:
  seasons/<slug>_standings.json   [{name, wins, losses, ties, pf, pa, streak, rank, fab, moves}]
  seasons/<slug>_rosters.json     live rosters (replaces the draft seed for build_week/dash)

Discovers each league's Yahoo key dynamically (game key changes yearly), so it
just works once the developer-access application is approved. Until then it
prints the scope-gate message and writes nothing.
"""

import json
import os
import re

import requests

import yahoo_auth as ya
import build_board as bb

# our Yahoo league ids -> our slugs
LEAGUE_IDS = {"701443": "papi-chulo", "42313": "the-league"}
API = "https://fantasysports.yahooapis.com/fantasy/v2"


def get(path):
    r = requests.get(f"{API}/{path}?format=json",
                     headers={"Authorization": "Bearer " + ya.access_token()}, timeout=30)
    r.raise_for_status()
    return r.json()


def find_league_keys():
    """Return {slug: league_key} for our two leagues from the user's NFL leagues."""
    d = get("users;use_login=1/games;game_keys=nfl/leagues")
    keys = {}
    # walk the nested fantasy_content for league_key + league_id pairs
    txt = json.dumps(d)
    for m in re.finditer(r'"league_key":"([^"]+)".*?"league_id":"([^"]+)"', txt):
        lk, lid = m.group(1), m.group(2)
        if lid in LEAGUE_IDS:
            keys[LEAGUE_IDS[lid]] = lk
    return keys


def _teams_from(node):
    """Yahoo's JSON is deeply nested arrays; pull team dicts pragmatically."""
    out = []
    txt = json.dumps(node)
    # each team block has name, then standings with outcome_totals + points
    for tm in re.finditer(r'"name":"([^"]+)".*?"team_standings":\{(.*?)\}\}', txt, re.S):
        name = tm.group(1)
        blk = tm.group(2)

        def num(key, default=0):
            mm = re.search(rf'"{key}":"?([-\d.]+)"?', blk)
            return float(mm.group(1)) if mm else default
        out.append({"name": name, "rank": int(num("rank")),
                    "wins": int(num("wins")), "losses": int(num("losses")), "ties": int(num("ties")),
                    "pf": round(num("points_for"), 1), "pa": round(num("points_against"), 1),
                    "streak": ""})
    return out


def _txns_from(node):
    """add/drop/trade transactions with FAB bids from /transactions JSON."""
    out = []
    txt = json.dumps(node)
    for tx in re.finditer(r'"type":"(add/drop|add|drop|trade|commish)".*?(?="type":"(?:add|drop|trade|commish)"|$)', txt, re.S):
        blk = tx.group(0)
        fab = re.search(r'"faab_bid":"?(\d+)"?', blk)
        ts = re.search(r'"timestamp":"?(\d+)"?', blk)
        adds = re.findall(r'"transaction_data":\[\{"type":"add".*?"name":\{"full":"([^"]+)"', blk)
        drops = re.findall(r'"transaction_data":\[?\{"type":"drop".*?"name":\{"full":"([^"]+)"', blk)
        team = re.search(r'"destination_team_name":"([^"]+)"', blk) or re.search(r'"nickname":"([^"]+)"', blk)
        if adds or drops:
            out.append({"date": ts.group(1) if ts else None, "team": team.group(1) if team else None,
                        "type": tx.group(1), "fab": int(fab.group(1)) if fab else None,
                        "adds": [{"name": a} for a in adds], "drops": [{"name": d} for d in drops],
                        "targeted": adds})
    return out


def sync_league(slug, key):
    st = get(f"league/{key}/standings")
    teams = _teams_from(st)
    if teams:
        teams.sort(key=lambda t: t["rank"] or 99)
        with open(os.path.join("seasons", f"{slug}_standings.json"), "w", encoding="utf-8") as fh:
            json.dump({"league_key": key, "teams": teams}, fh, indent=1, ensure_ascii=False)
        print(f"  {slug}: wrote standings for {len(teams)} teams")
    try:
        tx = get(f"league/{key}/transactions")
        txns = _txns_from(tx)
        with open(os.path.join("seasons", f"{slug}_transactions.json"), "w", encoding="utf-8") as fh:
            json.dump({"league_key": key, "txns": txns}, fh, indent=1, ensure_ascii=False)
        print(f"  {slug}: wrote {len(txns)} transactions")
    except Exception as exc:  # noqa: BLE001
        print(f"  {slug}: transactions skipped ({exc})")
    return len(teams)


def main():
    os.makedirs("seasons", exist_ok=True)
    if not os.path.exists(ya.TOKENS):
        raise SystemExit("Run `python yahoo_auth.py url` and authorize first.")
    try:
        keys = find_league_keys()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and "additional_authorization_required" in e.response.text:
            raise SystemExit("Yahoo fantasy scope not yet approved — standings sync will work once "
                             "the developer-access application clears. Using manual/paste data meanwhile.")
        raise
    if not keys:
        raise SystemExit("Neither league found under this account — check LEAGUE_IDS.")
    for slug, key in keys.items():
        print(f"[{slug}] {key}")
        sync_league(slug, key)


if __name__ == "__main__":
    main()
