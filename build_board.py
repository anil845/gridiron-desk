"""Build an offline auction draft board for one league: board_data_<league>.json
+ board_<league>.html (generated from the board.html template).

Standalone. No database, no players table. League settings come from
leagues/<league>.json (transcribed from Yahoo — treated as source of truth).

Sources (all public, all cached under data/, each degrades gracefully):
  1. Sleeper 2026 season projections  -> PRIMARY points, scored through the
                                         league's own scoring function
  2. FantasyCalc redraft values       -> market value, tiers, id crosswalk
  3. FFC ADP (ppr)                    -> market rank (attribution: Fantasy
                                         Football Calculator)
  4. nflverse weekly stats 2023-25    -> historical points curve (fallback for
                                         players without projections) + per-QB
                                         sack rates + fumble ratio
  5. nflverse play-by-play 2023-25    -> pick-sixes thrown (distilled once into
                                         data/pick_six.json)
  6. nflverse games.csv               -> 2026 bye weeks
  7. Sleeper player list              -> K / DEF, deep-bench names, id crosswalk
  8. FantasyPros ECR                  -> only if FANTASYPROS_API_KEY is set

Run from the repo root:
    python build_board.py --league papi-chulo
    python build_board.py --league the-league
    python build_board.py --league papi-chulo --force   # re-fetch sources
"""

import argparse
import csv
import datetime
import glob
import gzip
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

from ingest.common import DATA_DIR, USER_AGENT, http_get, log  # no DB import happens here

# =============================================================================
# Cross-league constants. League-specific settings live in leagues/*.json.
# =============================================================================
SEASON = 2026
STATS_SEASONS = [2023, 2024, 2025]   # completed seasons for curve + rates
SKILL = ("QB", "RB", "WR", "TE")
ALL_POS = ("QB", "RB", "WR", "TE", "K", "DEF")

# How flex slots split by position in practice. Renormalized over each slot's
# eligible positions, so one table serves every slot shape:
#   W/R/T -> RB .35 / WR .55 / TE .10           (as-is)
#   W/T   -> WR .55/.65=.846, TE .10/.65=.154   (~ WR 85% / TE 15%, RB excluded)
BASE_FLEX_USAGE = {"RB": 0.35, "WR": 0.55, "TE": 0.10}
# Bench slots split by position (K/DEF benches are ~0 in practice).
BENCH_SHARE = {"QB": 0.10, "RB": 0.40, "WR": 0.40, "TE": 0.10}

# Dollar conversion uses the roster-level replacement (starters + bench share):
# starter-level VORP put the #1 player at ~$124 with only ~66 players above $1,
# failing the $55-75 sanity band; roster-level matches real auction shape.
# "starter" remains available for comparison. `vorp` in the output is ALWAYS
# the starter-level scarcity number.
DOLLAR_REPLACEMENT = "roster"

# Tiers: per position, a new tier starts at a >=10% drop in FantasyCalc value
# (min 2 / max 6 players per tier).
TIER_DROP, TIER_MIN, TIER_MAX = 0.10, 2, 6

# ESPN public news feed (headline dump + per-player markers).
ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?limit=50"
# A backup RB is a "high-value handcuff" when his team's starter is worth at
# least this much — the lottery ticket that inherits a bell-cow role.
HANDCUFF_MIN_STARTER = 25
# Market classification: our price vs avg(ESPN AAV, WalterFootball $).
#   >=75% consensus (fairly priced) / >=40% validated (real support, unpriced
#   16-team premium = the buy list) / <40% mirage (only our model likes them).
MKT_CLASS_CONSENSUS, MKT_CLASS_VALIDATED = 0.75, 0.40

# Disagreement is spread scaled by draft slot: a 20-rank spread on a 2nd-round
# player is a story, on a 12th-round player it's noise.
# disagreement = min(1, spread / (consensus_rank + 15))
DISAGREE_NORM = 15

SLEEPER_FILL_DEPTH = 400
TEAM_ALIAS = {"LA": "LAR", "WSH": "WAS", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}

# FantasyCalc numTeams: supported counts are 10/12/14 ONLY. numTeams=16 returns
# a clean HTTP 200 whose values are IDENTICAL to numTeams=12 — a silent
# fallback, verified with same-minute fetches 2026-08-31 (top-30 mean |diff|
# 16-vs-12 = 0.0 while 14-vs-12 = ~40). Request the closest supported count
# and let the roster-derived replacement math do the 16-team correction; the
# build verifies effectiveness empirically on every run (see fetch_fantasycalc).
FC_SUPPORTED = [10, 12, 14]
FC_URL = ("https://api.fantasycalc.com/values/current"
          "?isDynasty=false&numQbs=1&numTeams={n}&ppr=1")
# FFC supports teams 8/10/12/14 (16 -> HTTP 400, verified 2026-08-31); auction
# format is not exposed by the API (HTTP 400), so ppr ADP is used.
FFC_SUPPORTED_TEAMS = [8, 10, 12, 14]
FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams={n}&year={year}"
SLEEPER_PROJ_URL = ("https://api.sleeper.app/projections/nfl/{year}?season_type=regular"
                    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&order_by=adp_ppr")
NFLVERSE_WEEKLY = ("https://github.com/nflverse/nflverse-data/releases/download/"
                   "stats_player/stats_player_week_{season}.csv")
NFLVERSE_PBP = ("https://github.com/nflverse/nflverse-data/releases/download/"
                "pbp/play_by_play_{season}.csv.gz")
GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
FP_URL = ("https://api.fantasypros.com/public/v2/json/nfl/{season}/consensus-rankings"
          "?position=ALL&scoring=PPR")
# WalterFootball's PPR cheat sheet: server-rendered HTML, ~310 players in rank
# order with Walt's auction $ values (robots.txt allows; cached daily).
WF_URL = "https://debacled.walterfootball.com/fantasy/cheatsheet/ppr"
# FantasyPros public PPR cheat-sheet page embeds the full ECR dataset (~510
# players, ~100 experts) as `var ecrData = {...}` — no API key required.
FP_PAGE_URL = "https://www.fantasypros.com/nfl/rankings/ppr-cheatsheets.php"
# ESPN's fantasy read API: live ownership data including auctionValueAverage —
# the average price actually paid in real ESPN auction drafts. No auth.
ESPN_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
            f"{SEASON}/segments/0/leaguedefaults/3?view=kona_player_info")
ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

TEMPLATE_HTML = "board.html"


# =============================================================================
# League config
# =============================================================================
SLOT_LETTER = {"Q": "QB", "R": "RB", "W": "WR", "T": "TE"}


def load_league(slug):
    path = os.path.join("leagues", f"{slug}.json")
    if not os.path.exists(path):
        avail = [os.path.splitext(os.path.basename(p))[0] for p in glob.glob("leagues/*.json")]
        raise SystemExit(f"no such league config: {path} (available: {', '.join(avail) or 'none'})")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["slug"] = slug
    cfg["roster_slots"] = parse_roster(cfg["roster"])
    return cfg


def parse_roster(roster):
    """Ordered [{slot, elig}] from the config's {slot_name: count} mapping.
    Eligibility derives from the slot name itself — 'W/R/T' -> [WR, RB, TE],
    'W/T' -> [WR, TE] — so no league's slot shape is hardcoded."""
    slots = []
    for name, count in roster.items():
        if name.startswith("_"):
            continue
        if name in ALL_POS:
            elig = [name]
        elif name == "BN":
            elig = list(ALL_POS)
        elif "/" in name:
            elig = []
            for part in name.split("/"):
                if part not in SLOT_LETTER:
                    raise SystemExit(f"unknown flex slot letter {part!r} in roster slot {name!r}")
                elig.append(SLOT_LETTER[part])
        else:
            raise SystemExit(f"unknown roster slot {name!r}")
        slots.extend({"slot": name, "elig": elig} for _ in range(int(count)))
    # Config key order is preserved (dicts are ordered): a pick fills the first
    # open eligible slot, and both league files list dedicated slots before
    # flex before bench, which keeps dedicated slots from being wasted.
    return slots


def flex_shares(elig):
    """BASE_FLEX_USAGE renormalized over the slot's eligible positions."""
    tot = sum(BASE_FLEX_USAGE.get(p, 0.0) for p in elig)
    if tot <= 0:
        return {p: 1.0 / len(elig) for p in elig}
    return {p: BASE_FLEX_USAGE.get(p, 0.0) / tot for p in elig}


def replacement_ranks(cfg):
    """(starter_rank, roster_rank) per position, derived from the config.

      dedicated       = teams * (slots naming only that position)
      flex            = teams * count, split by flex_shares(elig)
      starter_rank    = dedicated + flex share
      roster_rank     = starter_rank + bench share (the last player anyone
                        would roster — what the dollar conversion uses)

    The whole point of the tool: e.g. Papi Chulo (16tm, dedicated TE) puts TE
    replacement at ~17.6 while The League (12tm, no TE slot) puts it at ~4.8.
    Public 10/12-team rankings are calibrated to neither.
    """
    n = cfg["teams"]
    starter = defaultdict(float)
    bench_slots = 0
    for s in cfg["roster_slots"]:
        if s["slot"] == "BN":
            bench_slots += n
        elif len(s["elig"]) == 1:
            starter[s["elig"][0]] += n
        else:
            for pos, share in flex_shares(s["elig"]).items():
                starter[pos] += n * share
    roster = dict(starter)
    for pos, share in BENCH_SHARE.items():
        roster[pos] = roster.get(pos, 0.0) + bench_slots * share
    return dict(starter), roster


# =============================================================================
# Scoring — one function, driven entirely by the league's scoring dict.
# Canonical stat keys; every source is mapped onto these before scoring.
# =============================================================================
def score(st, sc):
    """Score a canonical stat dict with a league scoring dict.

    Fumble semantics (per Yahoo): BOTH fields fire on a lost fumble.
      The League: fumble -1, fumble_lost -1 -> lost = -2 total, recovered = -1.
      Papi Chulo: fumble 0, fumble_lost -2 -> lost = -2 total, recovered = 0.
    `fumbles` below is TOTAL fumbles (lost + recovered own).
    """
    g = lambda k: st.get(k) or 0.0  # noqa: E731
    return (
        g("pass_yd") * sc.get("pass_yd", 0)
        + g("pass_td") * sc.get("pass_td", 0)
        + g("pass_int") * sc.get("interception", 0)
        + g("sacks") * sc.get("sack_taken", 0)
        + g("pick_six") * sc.get("pick_six_thrown", 0)
        + g("rush_yd") * sc.get("rush_yd", 0)
        + g("rush_td") * sc.get("rush_td", 0)
        + g("rec") * sc.get("reception", 0)
        + g("rec_yd") * sc.get("rec_yd", 0)
        + g("rec_td") * sc.get("rec_td", 0)
        + g("two_pt") * sc.get("two_pt", 0)
        + g("fumbles") * sc.get("fumble", 0)
        + g("fumbles_lost") * sc.get("fumble_lost", 0)
    )


# =============================================================================
# Fetch helpers (disk-cached)
# =============================================================================
def _cached(path, fetch, force, max_age_h=None, binary=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    if not force and os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if max_age_h is None or age_h < max_age_h:
            log.info("using cache (%.1fh old): %s", age_h, path)
            with open(path, "rb" if binary else "r", encoding=None if binary else "utf-8") as fh:
                return fh.read()
    data = fetch()
    tmp = path + ".tmp"
    with open(tmp, "wb" if binary else "w", encoding=None if binary else "utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return data


def _fc_fetch(n, force):
    path = os.path.join(DATA_DIR, f"fantasycalc_{n}tm.json")
    text = _cached(path, lambda: json.dumps(http_get(FC_URL.format(n=n))),
                   force, max_age_h=12)
    data = json.loads(text)
    if not (isinstance(data, list) and data):
        raise ValueError(f"numTeams={n}: unexpected payload")
    return data


def _fc_mean_diff(a, b, top=30):
    """Mean |value difference| over the common top-`top` players of two payloads."""
    va = {e["player"]["id"]: e["value"] for e in a[:top]}
    vb = {e["player"]["id"]: e["value"] for e in b[:top]}
    common = set(va) & set(vb)
    if not common:
        return None
    return sum(abs(va[i] - vb[i]) for i in common) / len(common)


def fetch_fantasycalc(cfg, force, notes):
    """Fetch market values at the closest SUPPORTED team count, then verify
    empirically which count the API actually served.

    The API answers any numTeams with HTTP 200; unsupported counts silently
    get the 12-team data. So the requested count alone proves nothing — after
    fetching, the payload is compared value-by-value against a same-vintage
    numTeams=12 payload. If they match, both are force-refetched once (a
    stale-cache pair would also match) and re-compared; a persistent match is
    reported loudly as a silent 12-team fallback. The requested AND effective
    counts always appear in the log and in meta.sources.
    """
    n = max([t for t in FC_SUPPORTED if t <= cfg["teams"]] or [FC_SUPPORTED[0]])
    data = _fc_fetch(n, force)
    if n == 12:
        notes.append("FantasyCalc: requested numTeams=12, effective=12 (baseline count)")
        return data
    try:
        ref = _fc_fetch(12, force)
        diff = _fc_mean_diff(data, ref)
        if not diff:
            log.warning("FantasyCalc: numTeams=%d values match 12-team payload — "
                        "refetching a fresh same-vintage pair to rule out stale caches", n)
            data, ref = _fc_fetch(n, True), _fc_fetch(12, True)
            diff = _fc_mean_diff(data, ref)
        if diff:
            msg = (f"FantasyCalc: requested numTeams={n}, effective={n} "
                   f"(top-30 mean |diff| vs 12tm = {diff:.0f})")
            log.info(msg)
        else:
            msg = (f"FantasyCalc: requested numTeams={n}, EFFECTIVE=12 - SILENT FALLBACK "
                   f"detected; roster-derived replacement math is the only "
                   f"{cfg['teams']}-team correction in play")
            log.warning(msg)
    except Exception as exc:  # noqa: BLE001 — verification is best-effort
        msg = f"FantasyCalc: requested numTeams={n}, effective UNVERIFIED ({exc})"
        log.warning(msg)
    notes.append(msg)
    return data


def fetch_ffc(cfg, force, notes):
    """FFC ADP. teams=16 is not supported (HTTP 400) — use the closest
    supported count and record it. Never aborts the build."""
    n = max([t for t in FFC_SUPPORTED_TEAMS if t <= cfg["teams"]] or [FFC_SUPPORTED_TEAMS[-1]])
    path = os.path.join(DATA_DIR, f"ffc_adp_ppr_{n}.json")
    try:
        text = _cached(path, lambda: http_get(FFC_URL.format(n=n, year=SEASON), expect="text"),
                       force, max_age_h=24)   # FFC updates once daily — do not poll
        data = json.loads(text)
        players = data.get("players") or []
        if not players:
            raise ValueError(f"status={data.get('status')}")
        notes.append(f"FFC ADP: ppr teams={n}"
                     + (f" (league has {cfg['teams']}; 16 unsupported by FFC)" if n != cfg["teams"] else "")
                     + f", {len(players)} players. Auction format not exposed by the API (HTTP 400).")
        return players
    except Exception as exc:  # noqa: BLE001
        notes.append(f"FFC ADP: unavailable ({exc}) - ffc rank skipped")
        return []


def fetch_projections(force, notes):
    """Sleeper 2026 season projections. Verified to return season=2026 rows.
    Refuses to use the payload if the season doesn't match — better no
    projections than last year's silently."""
    path = os.path.join(DATA_DIR, f"sleeper_projections_{SEASON}.json")
    try:
        text = _cached(path, lambda: json.dumps(http_get(SLEEPER_PROJ_URL.format(year=SEASON))),
                       force, max_age_h=24)
        rows = json.loads(text)
        good = [r for r in rows if str(r.get("season")) == str(SEASON) and r.get("stats")]
        if not good:
            raise ValueError(f"no season={SEASON} rows in payload")
        notes.append(f"Sleeper projections: {len(good)} players, season {SEASON} verified")
        return {str(r["player_id"]): r["stats"] for r in good}
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Sleeper projections: unavailable ({exc}) - falling back to "
                     "historical rank curve for ALL players")
        return {}


def fetch_weekly(season, force):
    path = os.path.join(DATA_DIR, f"nflverse_stats_player_week_{season}.csv")
    return _cached(path, lambda: http_get(NFLVERSE_WEEKLY.format(season=season), expect="text"), force)


def fetch_games(force):
    path = os.path.join(DATA_DIR, "nflverse_games.csv")
    return _cached(path, lambda: http_get(GAMES_URL, expect="text"), force, max_age_h=24 * 7)


def fetch_sleeper_players(force):
    path = os.path.join(DATA_DIR, "sleeper_players_nfl.json")
    text = _cached(path, lambda: json.dumps(http_get(SLEEPER_URL)), force, max_age_h=24)
    return json.loads(text)


def fetch_fantasypros(force):
    """ECR + expert spread. Uses the official API when FANTASYPROS_API_KEY is
    set; otherwise scrapes the `var ecrData = {...}` JSON embedded in the
    public PPR cheat-sheet page (~510 players, ~100 experts, updated daily).

    The `ecr` value stored per player is the SKILL-ONLY rank (K/DST removed
    and re-ranked) so it is comparable to the other sources' overall ranks;
    rank_min / rank_max / rank_std are kept on FantasyPros's own scale as the
    expert-disagreement signal."""
    key = os.environ.get("FANTASYPROS_API_KEY")
    import requests
    if key:
        try:
            resp = requests.get(FP_URL.format(season=SEASON), headers={"x-api-key": key}, timeout=30)
            resp.raise_for_status()
            players = resp.json().get("players", [])
            note = f"FantasyPros: {len(players)} ECR rows from the API"
        except Exception as exc:  # noqa: BLE001
            return {}, f"FantasyPros: API fetch failed ({exc}) - ECR fields left null"
    else:
        path = os.path.join(DATA_DIR, "fantasypros_ppr.html")

        def _get():
            r = requests.get(FP_PAGE_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
            r.raise_for_status()
            if "ecrData" not in r.text:
                raise ValueError("no ecrData in page")
            return r.text

        try:
            text = _cached(path, _get, force, max_age_h=12)
            m = re.search(r"var ecrData\s*=\s*(\{.*?\});\s*\n", text, re.S)
            data = json.loads(m.group(1))
            players = data.get("players", [])
            note = (f"FantasyPros: {len(players)} ECR rows scraped from the public page "
                    f"({data.get('total_experts')} experts, updated {data.get('last_updated')})")
        except Exception as exc:  # noqa: BLE001 — optional source, must not fail the build
            return {}, f"FantasyPros: page scrape failed ({exc}) - ECR fields left null"
    skill = sorted((p for p in players if p.get("player_position_id") in SKILL),
                   key=lambda p: p.get("rank_ecr") or 9999)
    rows = {}
    for i, p in enumerate(skill):
        rows.setdefault(norm_name(p.get("player_name", "")), {
            "ecr": float(i + 1),
            "ecr_stddev": _num(p.get("rank_std")),
            "ecr_best": _num(p.get("rank_min")), "ecr_worst": _num(p.get("rank_max")),
        })
    return rows, note


def fetch_espn(force, notes):
    """ESPN live draft market -> {(norm_name, pos): {rank, aav}}.

    auctionValueAverage is the average price paid in real completed ESPN
    auction drafts — the only real-money market signal available anywhere
    without Yahoo OAuth. rank is the skill-only order by ESPN ADP. Cached 6h
    because these move intraday as more drafts complete."""
    import requests
    path = os.path.join(DATA_DIR, "espn_kona.json")

    def _get():
        flt = json.dumps({"players": {"limit": 400,
                                      "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}})
        r = requests.get(ESPN_URL, headers={"x-fantasy-filter": flt, "User-Agent": USER_AGENT},
                         timeout=60)
        r.raise_for_status()
        return r.text
    try:
        d = json.loads(_cached(path, _get, force, max_age_h=6))
        rows = []
        for p in d.get("players", []):
            pl = p.get("player") or {}
            own = pl.get("ownership") or {}
            pos = ESPN_POS.get(pl.get("defaultPositionId"))
            if pos in SKILL and own.get("auctionValueAverage") is not None \
                    and own.get("averageDraftPosition") is not None:
                rows.append((own["averageDraftPosition"], norm_name(pl.get("fullName", "")),
                             pos, own["auctionValueAverage"]))
        rows.sort()
        out = {}
        for i, (adp, nn, pos, aav) in enumerate(rows):
            out.setdefault((nn, pos), {"rank": i + 1, "aav": int(round(aav))})
        if len(out) < 100:
            raise ValueError(f"only {len(out)} usable rows")
        notes.append(f"ESPN: {len(out)} skill players with live auction-value averages "
                     "(rank by ESPN ADP, cached 6h)")
        return out
    except Exception as exc:  # noqa: BLE001 — optional source, must not fail the build
        notes.append(f"ESPN: unavailable ({exc}) - espn rank/AAV skipped")
        return {}


def fetch_walterfootball(force, notes):
    """WalterFootball PPR cheat sheet -> {(norm_name, pos): {rank, value}}.

    rank is the skill-only overall rank (K/DEF excluded from the count so it
    is comparable to the other sources' overall ranks); value is Walt's
    auction dollar figure. Parsed with a narrow regex over the rendered list
    items; if the layout changes the parse count collapses and the source is
    skipped with a note rather than joining garbage."""
    import html as htmllib
    path = os.path.join(DATA_DIR, "walterfootball_ppr.html")
    try:
        text = _cached(path, lambda: http_get(WF_URL, expect="text"), force, max_age_h=24)
        pat = re.compile(
            r'<li data-player-id="\d+" class="player">\s*<span class="player-summary">\s*'
            r'([^,<]+),\s*([A-Z/]+),\s*[^.<]+\.\s*Bye:\s*\S*\s*'
            r'<strong class="player-value">\$(\d+)</strong>', re.S)
        rows = pat.findall(text)
        if len(rows) < 100:
            raise ValueError(f"only {len(rows)} players parsed - page layout changed?")
        out, skill_rank = {}, 0
        for name, pos, val in rows:
            if pos not in SKILL:
                continue
            skill_rank += 1
            key = (norm_name(htmllib.unescape(name)), pos)
            if key not in out:
                out[key] = {"rank": skill_rank, "value": int(val)}
        notes.append(f"WalterFootball: {len(rows)} players parsed from the PPR cheat sheet "
                     f"({len(out)} skill ranks + auction $)")
        return out
    except Exception as exc:  # noqa: BLE001 — optional source, must not fail the build
        notes.append(f"WalterFootball: unavailable ({exc}) - wf rank skipped")
        return {}


def fetch_news(force, notes):
    """ESPN NFL headlines with linked athlete ids. Cached 3h; degrades to []"""
    import requests
    path = os.path.join(DATA_DIR, "espn_news.json")

    def _get():
        r = requests.get(ESPN_NEWS_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        return r.text
    try:
        d = json.loads(_cached(path, _get, force, max_age_h=3))
        out = []
        for a in d.get("articles", []):
            out.append({"date": (a.get("published") or "")[:10],
                        "headline": a.get("headline") or "",
                        "espn_ids": [str(c.get("athleteId")) for c in a.get("categories", [])
                                     if c.get("type") == "athlete" and c.get("athleteId")]})
        notes.append(f"ESPN news: {len(out)} headlines (cached 3h)")
        return out
    except Exception as exc:  # noqa: BLE001 — optional source, must not fail the build
        notes.append(f"ESPN news: unavailable ({exc}) - news markers skipped")
        return []


def assign_intel(players, news):
    """Handcuffs, market classification, and news markers (see constants).

    Handcuff: for each NFL team, the second-best RB by projected points behind
    a starter worth >= HANDCUFF_MIN_STARTER. mkt_class only for players worth
    >= $8 with at least one real-market dollar figure. News: latest headline
    matched by ESPN athlete id, else by full name appearing in the headline.
    """
    by_team = defaultdict(list)
    for p in players:
        p["handcuff_of"] = p["handcuff_pid"] = p["mkt_class"] = p["news"] = None
        if p["position"] == "RB" and p["team"] and p.get("points"):
            by_team[p["team"]].append(p)
    for team, rbs in by_team.items():
        rbs.sort(key=lambda q: -q["points"])
        if len(rbs) >= 2 and rbs[0]["value"] >= HANDCUFF_MIN_STARTER:
            rbs[1]["handcuff_of"] = rbs[0]["name"]
            rbs[1]["handcuff_pid"] = rbs[0]["player_id"]
    for p in players:
        if p["position"] in SKILL and p["value"] >= 8:
            ms = [x for x in (p.get("espn_aav"), p.get("wf_value")) if x]
            if ms:
                r = (sum(ms) / len(ms)) / p["value"]
                p["mkt_class"] = ("consensus" if r >= MKT_CLASS_CONSENSUS
                                  else "validated" if r >= MKT_CLASS_VALIDATED else "mirage")
    if news:
        by_espn = {}
        for p in players:
            if p.get("espn_id"):
                by_espn.setdefault(p["espn_id"], p)
        for item in news:   # feed is newest-first; first match per player wins
            for eid in item["espn_ids"]:
                q = by_espn.get(eid)
                if q is not None and not q["news"]:
                    q["news"] = item["date"] + ": " + item["headline"]
            hl = " " + norm_name(item["headline"]) + " "
            for p in players:
                if not p["news"] and len(p["name"].split()) >= 2 and (" " + norm_name(p["name"]) + " ") in hl:
                    p["news"] = item["date"] + ": " + item["headline"]
    log.info("intel: %d handcuffs, %d news-tagged, classes %s",
             sum(1 for p in players if p["handcuff_of"]),
             sum(1 for p in players if p["news"]),
             dict(Counter(p["mkt_class"] for p in players if p["mkt_class"])))


def pick_six_data(force, notes):
    """data/pick_six.json: per-season per-passer pick-six counts + league
    INT->pick-six rate, distilled from nflverse play-by-play (~19MB gz per
    season, parsed once and cached)."""
    path = os.path.join(DATA_DIR, "pick_six.json")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {}
        try:
            for season in STATS_SEASONS:
                gz = os.path.join(DATA_DIR, f"pbp_{season}.csv.gz")
                _cached(gz, lambda s=season: http_get(NFLVERSE_PBP.format(season=s),
                                                      expect="response", stream=True).content,
                        force=False, binary=True)
                p6, ints, sixes = Counter(), 0, 0
                with gzip.open(gz, "rt", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        if row.get("season_type") != "REG" or row.get("interception") != "1":
                            continue
                        ints += 1
                        if row.get("return_touchdown") == "1":
                            sixes += 1
                            if row.get("passer_player_id"):
                                p6[row["passer_player_id"]] += 1
                data[str(season)] = {"ints": ints, "pick_sixes": sixes,
                                     "rate": round(sixes / ints, 4), "by_passer": dict(p6)}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"pick-six: play-by-play unavailable ({exc}) - pick_six_thrown "
                         "scored as ZERO for all QBs (documented omission)")
            return {}, 0.0
    rates = [v["rate"] for v in data.values()]
    rate = sum(rates) / len(rates) if rates else 0.0
    notes.append(f"pick-six: derived from pbp {STATS_SEASONS}; league rate "
                 f"{rate:.3f} pick-sixes per INT applied to projected INTs")
    return data, rate


# =============================================================================
# Small utils
# =============================================================================
def _num(v):
    try:
        return None if v in (None, "", "NA") else float(v)
    except (TypeError, ValueError):
        return None


def norm_name(s):
    s = (s or "").lower()
    s = re.sub(r"[.'’\-]", "", s)
    s = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def team_abbr(t):
    t = (t or "").upper()
    return TEAM_ALIAS.get(t, t)


# =============================================================================
# Historical stats: canonical per-player-season dicts (league-agnostic),
# scored per league later. Also yields sack rates and the fumble ratio.
# =============================================================================
def load_history(force, p6_data):
    f = lambda v: float(v) if v not in ("", "NA", None) else 0.0  # noqa: E731
    seasons = {}
    for season in STATS_SEASONS:
        agg = defaultdict(lambda: defaultdict(float))
        meta = {}
        for x in csv.DictReader(io.StringIO(fetch_weekly(season, force))):
            if x["season_type"] != "REG" or x["position"] not in SKILL:
                continue
            a = agg[x["player_id"]]
            meta[x["player_id"]] = (x["player_display_name"], x["position"])
            a["pass_yd"] += f(x["passing_yards"]); a["pass_td"] += f(x["passing_tds"])
            a["pass_int"] += f(x["passing_interceptions"])
            a["pass_att"] += f(x["attempts"]); a["sacks"] += f(x["sacks_suffered"])
            a["rush_yd"] += f(x["rushing_yards"]); a["rush_td"] += f(x["rushing_tds"])
            a["rec"] += f(x["receptions"]); a["rec_yd"] += f(x["receiving_yards"])
            a["rec_td"] += f(x["receiving_tds"])
            a["two_pt"] += (f(x["passing_2pt_conversions"]) + f(x["rushing_2pt_conversions"])
                            + f(x["receiving_2pt_conversions"]))
            a["fumbles"] += (f(x["sack_fumbles"]) + f(x["rushing_fumbles"])
                             + f(x["receiving_fumbles"]))
            a["fumbles_lost"] += (f(x["sack_fumbles_lost"]) + f(x["rushing_fumbles_lost"])
                                  + f(x["receiving_fumbles_lost"]))
        by_passer = (p6_data.get(str(season)) or {}).get("by_passer", {})
        for pid, n in by_passer.items():
            if pid in agg:
                agg[pid]["pick_six"] = float(n)
        seasons[season] = {"stats": dict(agg), "meta": meta}

    # sacks-taken column must exist — the Papi Chulo edge depends on it.
    total_sacks = sum(a["sacks"] for s in seasons.values() for a in s["stats"].values())
    if total_sacks == 0:
        raise SystemExit("FATAL: sacks_suffered summed to 0 across all seasons - "
                         "the nflverse column moved. Refusing to score sacks at zero silently.")

    # per-QB sack rate (sacks per dropback), last two seasons weighted equally;
    # league-average fallback for QBs without history (rookies).
    rate_num, rate_den = defaultdict(float), defaultdict(float)
    lg_num = lg_den = 0.0
    fum_tot = fum_lost = 0.0
    for season in STATS_SEASONS:
        for pid, a in seasons[season]["stats"].items():
            name, pos = seasons[season]["meta"][pid]
            fum_tot += a["fumbles"]; fum_lost += a["fumbles_lost"]
            if pos == "QB" and a["pass_att"] >= 100 and season >= STATS_SEASONS[-2]:
                key = norm_name(name)
                rate_num[key] += a["sacks"]; rate_den[key] += a["pass_att"] + a["sacks"]
                lg_num += a["sacks"]; lg_den += a["pass_att"] + a["sacks"]
    sack_rates = {k: rate_num[k] / rate_den[k] for k in rate_num}
    lg_sack_rate = lg_num / lg_den if lg_den else 0.07
    fumble_ratio = fum_tot / fum_lost if fum_lost else 1.9   # total fumbles per lost fumble
    return seasons, sack_rates, lg_sack_rate, fumble_ratio


def points_curve(seasons, scoring):
    """{pos: [pts at rank 1, 2, ...]} under THIS league's scoring, averaged
    rank-by-rank across STATS_SEASONS (one outlier year can't dictate a
    position's spread). Fallback scale for players without projections."""
    per = []
    for season in STATS_SEASONS:
        by = defaultdict(list)
        s = seasons[season]
        for pid, a in s["stats"].items():
            by[s["meta"][pid][1]].append(score(a, scoring))
        for pos in by:
            by[pos].sort(reverse=True)
        per.append(by)
    curve = {}
    for pos in SKILL:
        arrs = [c[pos] for c in per if c.get(pos)]
        n = min(len(a) for a in arrs)
        curve[pos] = [sum(a[i] for a in arrs) / len(arrs) for i in range(n)]
    return curve


def curve_at(curve, pos, rank):
    arr = curve.get(pos) or []
    if not arr:
        return 0.0   # K / DEF: no curve, no VORP
    r = max(1.0, min(float(rank), float(len(arr))))
    lo = int(r); hi = min(lo + 1, len(arr)); frac = r - lo
    return arr[lo - 1] * (1 - frac) + arr[hi - 1] * frac


# =============================================================================
# Projections -> canonical stats (sacks / pick-sixes / total fumbles estimated,
# since no projection source carries them).
# =============================================================================
def canonical_projection(st, name, sack_rates, lg_sack_rate, p6_rate, fumble_ratio):
    g = lambda k: st.get(k) or 0.0  # noqa: E731
    att = g("pass_att")
    rate = sack_rates.get(norm_name(name), lg_sack_rate)
    sacks = att * rate / (1 - rate) if att else 0.0   # sacks = rate * dropbacks
    fum_lost = g("fum_lost")
    return {
        "pass_yd": g("pass_yd"), "pass_td": g("pass_td"), "pass_int": g("pass_int"),
        "sacks": sacks, "pick_six": g("pass_int") * p6_rate,
        "rush_yd": g("rush_yd"), "rush_td": g("rush_td"),
        "rec": g("rec"), "rec_yd": g("rec_yd"), "rec_td": g("rec_td"),
        "two_pt": g("pass_2pt") + g("rush_2pt") + g("rec_2pt"),
        "fumbles": fum_lost * fumble_ratio,   # recovered-own estimated from league ratio
        "fumbles_lost": fum_lost,
    }


# =============================================================================
# Player assembly
# =============================================================================
def build_players(fc, sleeper, byes, fp):
    players, seen = [], set()
    for e in fc:
        pl = e["player"]
        sid = str(pl.get("sleeperId") or f"fc{pl['id']}")
        seen.add(sid)
        players.append({
            "player_id": sid, "name": pl["name"], "position": pl["position"],
            "team": team_abbr(pl.get("maybeTeam")), "pos_rank": e["positionRank"],
            "fc_rank": e["overallRank"], "fc_value": e["value"], "tier": None,
            "trend30": e.get("trend30Day"), "roster_pct": e.get("maybeRosterPercent"),
        })
    fill = []
    for sid, s in sleeper.items():
        pos = s.get("position")
        if pos == "DEF":
            fill.append((0, sid, f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(),
                         "DEF", team_abbr(sid)))
        elif pos in ("K",) + SKILL and s.get("team") and s.get("active") \
                and s.get("status") == "Active" and sid not in seen:
            rank = s.get("search_rank") or 9_999_999
            if pos == "K" or rank <= SLEEPER_FILL_DEPTH:
                fill.append((rank, sid, s.get("full_name") or "", pos, team_abbr(s.get("team"))))
    fill.sort()
    next_rank = defaultdict(int)
    for p in players:
        next_rank[p["position"]] = max(next_rank[p["position"]], p["pos_rank"])
    for rank, sid, name, pos, team in fill:
        next_rank[pos] += 1
        players.append({"player_id": sid, "name": name, "position": pos, "team": team,
                        "pos_rank": next_rank[pos], "fc_rank": None, "fc_value": None,
                        "tier": None, "trend30": None, "roster_pct": None})
    for p in players:
        p["bye"] = byes.get(p["team"])
        s = sleeper.get(p["player_id"]) or {}
        p["injury_status"] = s.get("injury_status")
        p["injury_note"] = s.get("injury_body_part")
        p["espn_id"] = str(s.get("espn_id")) if s.get("espn_id") else None
        f = fp.get(norm_name(p["name"]), {})
        p.update(ecr=f.get("ecr"), ecr_stddev=f.get("ecr_stddev"),
                 ecr_best=f.get("ecr_best"), ecr_worst=f.get("ecr_worst"))
    return players


def assign_points(players, proj, curve, scoring, sack_rates, lg_sack_rate, p6_rate, fumble_ratio):
    """points per player: league-scored Sleeper projection when one exists with
    real volume; historical rank curve otherwise. Marks points_source."""
    volume = {"QB": "pass_att", "RB": "rush_att", "WR": "rec", "TE": "rec"}
    n_proj = 0
    for p in players:
        if p["position"] not in SKILL:
            p["points"], p["points_source"] = None, None
            continue
        st = proj.get(p["player_id"])
        if st and (st.get(volume[p["position"]]) or 0) > 0:
            canon = canonical_projection(st, p["name"], sack_rates, lg_sack_rate,
                                         p6_rate, fumble_ratio)
            p["points"] = round(score(canon, scoring), 1)
            p["points_source"] = "proj"
            p["proj_gp"] = st.get("gp")
            n_proj += 1
        else:
            p["points"] = round(curve_at(curve, p["position"], p["pos_rank"]), 1)
            p["points_source"] = "curve"
    return n_proj


def assign_tiers(players):
    for pos in SKILL:
        ps = sorted((p for p in players if p["position"] == pos and p["fc_value"]),
                    key=lambda p: -p["fc_value"])
        tier, size, prev = 1, 0, None
        for p in ps:
            if prev is not None:
                drop = (prev - p["fc_value"]) / prev
                if (drop >= TIER_DROP and size >= TIER_MIN) or size >= TIER_MAX:
                    tier, size = tier + 1, 0
            p["tier"] = tier
            prev, size = p["fc_value"], size + 1


# =============================================================================
# Source disagreement — separate ranks kept, never blended into the price.
# =============================================================================
def assign_sources(players, ffc, proj, wf, espn):
    ffc_rank = {}
    for i, r in enumerate(ffc):
        ffc_rank.setdefault((norm_name(r.get("name")), r.get("position")), i + 1)
    skill = [p for p in players if p["position"] in SKILL]
    # Rank the projection source by roster-level VORP, not raw points: raw
    # points put ~20 QBs above every RB, which would show as fake "spread"
    # against the market sources (which are draft-order scaled). VORP is the
    # scarcity-adjusted equivalent of draft order. Requires vorp_to_dollars to
    # have run first.
    proj_sorted = sorted((p for p in skill if p["points_source"] == "proj"),
                         key=lambda p: -p["vorp_roster"])
    proj_rank = {p["player_id"]: i + 1 for i, p in enumerate(proj_sorted)}
    with_adp = sorted((p for p in skill if (proj.get(p["player_id"]) or {}).get("adp_ppr")),
                      key=lambda p: proj[p["player_id"]]["adp_ppr"])
    sadp_rank = {p["player_id"]: i + 1 for i, p in enumerate(with_adp)}
    for p in players:
        if p["position"] not in SKILL:
            p["sources"] = p["consensus_rank"] = p["spread"] = p["disagreement"] = None
            p["mkt_rank"] = p["wf_value"] = p["espn_aav"] = None
            continue
        src = {}
        if p["fc_rank"]:
            src["fc"] = p["fc_rank"]
        r = ffc_rank.get((norm_name(p["name"]), p["position"]))
        if r:
            src["ffc"] = r
        if p["player_id"] in proj_rank:
            src["proj"] = proj_rank[p["player_id"]]
        if p["player_id"] in sadp_rank:
            src["sadp"] = sadp_rank[p["player_id"]]
        w = wf.get((norm_name(p["name"]), p["position"]))
        p["wf_value"] = w["value"] if w else None
        if w:
            src["wf"] = w["rank"]
        e = espn.get((norm_name(p["name"]), p["position"]))
        p["espn_aav"] = e["aav"] if e else None
        if e:
            src["espn"] = e["rank"]
        if p.get("ecr"):
            src["ecr"] = int(p["ecr"])
        p["sources"] = src or None
        # market consensus = mean of the market sources only (proj excluded),
        # so the board can show "what the market thinks" vs "what the model
        # thinks" as two numbers instead of one blended spread.
        mkt = [v for k, v in src.items() if k != "proj"]
        p["mkt_rank"] = int(round(sum(mkt) / len(mkt))) if mkt else None
        if len(src) >= 2:
            import statistics
            ranks = list(src.values())
            p["consensus_rank"] = round(sum(ranks) / len(ranks), 1)
            p["spread"] = max(ranks) - min(ranks)
            # disagreement uses 2*stddev, not max-min: with 7 sources the raw
            # range inflates mechanically and one outlier source flags half
            # the board. 2*pstdev equals |a-b| for exactly two sources (so the
            # 0.4 threshold keeps its original meaning) and damps lone
            # outliers when there are many.
            p["disagreement"] = round(min(1.0, 2 * statistics.pstdev(ranks)
                                          / (p["consensus_rank"] + DISAGREE_NORM)), 2)
        else:
            p["consensus_rank"] = src.get("fc") if src else None
            p["spread"] = p["disagreement"] = None


# =============================================================================
# VORP -> dollars (same validated approach, replacement from final points)
# =============================================================================
def vorp_to_dollars(cfg, players, starter_rank, roster_rank):
    n, budget = cfg["teams"], cfg["budget"]
    total_money = n * budget
    total_slots = n * len(cfg["roster_slots"])
    discretionary = total_money - total_slots * 1
    kd_slots = n * sum(1 for s in cfg["roster_slots"] if s["elig"] in (["K"], ["DEF"]))
    pool_size = total_slots - kd_slots

    # replacement points come from the SAME points scale the players carry
    # (projection-first), so proj vs curve never mixes scales inside a position
    pos_points = defaultdict(list)
    for p in players:
        if p["position"] in SKILL:
            pos_points[p["position"]].append(p["points"])
    for pos in pos_points:
        pos_points[pos].sort(reverse=True)
    repl = lambda pos, rank: curve_at(pos_points, pos, rank)  # noqa: E731

    for p in players:
        if p["position"] not in SKILL:
            p["value"], p["vorp"], p["vorp_roster"] = 1, 0.0, 0.0
            continue
        p["vorp"] = round(p["points"] - repl(p["position"], starter_rank[p["position"]]), 1)
        p["vorp_roster"] = round(p["points"] - repl(p["position"], roster_rank[p["position"]]), 1)

    key = "vorp_roster" if DOLLAR_REPLACEMENT == "roster" else "vorp"
    skill = [p for p in players if p["position"] in SKILL]
    ranked = sorted(skill, key=lambda p: -p[key])
    pool = [p for p in ranked[:pool_size] if p[key] > 0]
    pool_sum = sum(p[key] for p in pool)
    for p in skill:
        p["value"] = 1
    for p in pool:
        p["value"] = int(round(1 + p[key] / pool_sum * discretionary))

    total = sum(p["value"] for p in sorted(players, key=lambda p: -p["value"])[:total_slots])
    top = max(players, key=lambda p: p["value"])
    if abs(total - total_money) > 0.03 * total_money:
        log.warning("SANITY [%s]: top-%d values sum to $%d, expected ~$%d",
                    cfg["slug"], total_slots, total, total_money)
    if not 55 <= top["value"] <= 75:
        log.warning("SANITY [%s]: top player %s = $%d, expected $55-75",
                    cfg["slug"], top["name"], top["value"])
    log.info("[%s] dollars: pool=%d, sum(top %d)=$%d, top=%s $%d, >$1: %d",
             cfg["slug"], len(pool), total_slots, total, top["name"], top["value"],
             sum(1 for p in players if p["value"] > 1))


# =============================================================================
# Part 3 report: QBs WITH vs WITHOUT the sack/INT penalties
# =============================================================================
def qb_penalty_report(cfg, players, proj, sack_rates, lg_sack_rate, p6_rate, fumble_ratio):
    if not (cfg["scoring"].get("sack_taken") or cfg["scoring"].get("pick_six_thrown")):
        return
    baseline = dict(cfg["scoring"], sack_taken=0, pick_six_thrown=0, interception=-1)
    rows = []
    for p in players:
        if p["position"] != "QB" or p["points_source"] != "proj":
            continue
        st = proj[p["player_id"]]
        canon = canonical_projection(st, p["name"], sack_rates, lg_sack_rate, p6_rate, fumble_ratio)
        rows.append((p["name"], score(canon, cfg["scoring"]), score(canon, baseline), canon["sacks"]))
    withr = {n: i + 1 for i, (n, w, _, _) in enumerate(sorted(rows, key=lambda r: -r[1]))}
    wout = {n: i + 1 for i, (n, _, wo, _) in enumerate(sorted(rows, key=lambda r: -r[2]))}
    log.info("[%s] top-20 QBs WITH league penalties (sack %s / INT %s / pick-six %s) "
             "vs WITHOUT (sack 0 / INT -1 / p6 0):", cfg["slug"],
             cfg["scoring"].get("sack_taken"), cfg["scoring"].get("interception"),
             cfg["scoring"].get("pick_six_thrown"))
    log.info("  %-24s %9s %6s | %9s %6s | %5s %6s", "QB", "with_pts", "rk", "base_pts", "rk", "sacks", "moved")
    for name, w, wo, sk in sorted(rows, key=lambda r: -r[1])[:20]:
        d = wout[name] - withr[name]
        log.info("  %-24s %9.1f %6d | %9.1f %6d | %5.0f %+6d",
                 name, w, withr[name], wo, wout[name], sk, d)


# =============================================================================
# Output
# =============================================================================
EMBED_DATA_RE = re.compile(r"(/\*BOARD_DATA\*/)(.*?)(/\*END_BOARD_DATA\*/)", re.S)
EMBED_CFG_RE = re.compile(r"(/\*LEAGUE_CONFIG\*/)(.*?)(/\*END_LEAGUE_CONFIG\*/)", re.S)


def board_config(cfg):
    teams = cfg.get("team_names") or [f"Team {i+1}" for i in range(cfg["teams"])]
    teams = (teams + [f"Team {i+1}" for i in range(len(teams), cfg["teams"])])[:cfg["teams"]]
    return {
        "LEAGUE_NAME": cfg["name"], "LEAGUE_ID": cfg["yahoo_league_id"],
        "NUM_TEAMS": cfg["teams"], "BUDGET": cfg["budget"],
        "TEAMS": teams, "ME": 0,
        "ROSTER": cfg["roster_slots"],
        "FLEX_USAGE": BASE_FLEX_USAGE, "BENCH_SHARE": BENCH_SHARE,
        "OVERPAY_FLAG": 1.15, "OVERPAY_MIN_VALUE": 20, "DISAGREE_MIN": 0.4,
        "CUFF_INSURANCE": 0.15, "CUFF_INS_CAP": 12,
        "QUALITY_MIN": 10, "URGENT_REMAINING": 3, "URGENT_CAP": 12,
        "POSITIONS": list(ALL_POS),
    }


def emit(cfg, out, no_embed):
    data_path = f"board_data_{cfg['slug']}.json"
    with open(data_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)
    log.info("[%s] wrote %s (%d players)", cfg["slug"], data_path, len(out["players"]))
    if no_embed:
        return
    with open(TEMPLATE_HTML, encoding="utf-8") as fh:
        html = fh.read()
    for rx, obj, label in ((EMBED_CFG_RE, board_config(cfg), "LEAGUE_CONFIG"),
                           (EMBED_DATA_RE, out, "BOARD_DATA")):
        blob = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).replace("</script", "<\\/script")
        html, n = rx.subn(lambda m, b=blob: m.group(1) + b + m.group(3), html)
        if n != 1:
            raise SystemExit(f"{TEMPLATE_HTML}: expected exactly one {label} marker, found {n}")
    out_path = f"board_{cfg['slug']}.html"
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, out_path)
    log.info("[%s] wrote %s", cfg["slug"], out_path)


def league_history_file(players):
    """Optional data/league_history.json (past prices). Unchanged from v1."""
    path = os.path.join("data", "league_history.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        recs = json.load(fh)
    by_year_mgr = defaultdict(lambda: defaultdict(list))
    for r in recs:
        by_year_mgr[r["year"]][r["manager"]].append(r)
    league_star = defaultdict(list)
    for year, mgrs in by_year_mgr.items():
        for m, rs in mgrs.items():
            top3 = sorted((float(x["price"]) for x in rs), reverse=True)[:3]
            if top3:
                league_star[year].append(sum(top3) / len(top3))
    league_avg = {y: sum(v) / len(v) for y, v in league_star.items() if v}
    managers = {}
    all_mgrs = {m for mgrs in by_year_mgr.values() for m in mgrs}
    for m in all_mgrs:
        num = den = paid = val = 0.0
        for year, mgrs in by_year_mgr.items():
            rs = mgrs.get(m)
            if not rs:
                continue
            top3 = sorted((float(x["price"]) for x in rs), reverse=True)[:3]
            num += sum(top3) / len(top3); den += league_avg[year]
            for x in rs:
                if x.get("value") is not None:
                    paid += float(x["price"]); val += float(x["value"])
        managers[m] = {"star_premium": round(num / den, 2) if den else None,
                       "overpay": round(paid / val, 2) if val else None,
                       "picks": sum(len(mgrs.get(m, [])) for mgrs in by_year_mgr.values())}
    return {"managers": managers, "records": len(recs)}


# =============================================================================
# Main
# =============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a league's auction board")
    ap.add_argument("--league", required=True, help="slug of leagues/<name>.json")
    ap.add_argument("--force", action="store_true", help="re-fetch all sources")
    ap.add_argument("--no-embed", action="store_true", help="write JSON only")
    args = ap.parse_args(argv)

    cfg = load_league(args.league)
    notes = []
    log.info("=== %s (%s): %d teams, $%d, %d active slots ===",
             cfg["name"], cfg["slug"], cfg["teams"], cfg["budget"], len(cfg["roster_slots"]))

    fc = fetch_fantasycalc(cfg, args.force, notes)
    ffc = fetch_ffc(cfg, args.force, notes)
    proj = fetch_projections(args.force, notes)
    wf = fetch_walterfootball(args.force, notes)
    espn = fetch_espn(args.force, notes)
    p6_data, p6_rate = pick_six_data(args.force, notes)
    seasons, sack_rates, lg_sack_rate, fumble_ratio = load_history(args.force, p6_data)
    notes.append(f"sacks: per-QB rate from nflverse sacks_suffered (league avg "
                 f"{lg_sack_rate:.3f}/dropback); total fumbles = fum_lost x {fumble_ratio:.2f}")
    byes_csv = fetch_games(args.force)
    sleeper = fetch_sleeper_players(args.force)
    fp, fp_note = fetch_fantasypros(args.force)
    notes.append(fp_note)

    curve = points_curve(seasons, cfg["scoring"])
    starter_rank, roster_rank = replacement_ranks(cfg)
    for pos in sorted(starter_rank):
        log.info("[%s] replacement %-3s starter %5.1f  roster %5.1f",
                 cfg["slug"], pos, starter_rank[pos], roster_rank[pos])

    byes = {}
    for r in csv.DictReader(io.StringIO(byes_csv)):
        if r["season"] == str(SEASON) and r["game_type"] == "REG":
            for t in (r["home_team"], r["away_team"]):
                byes.setdefault(team_abbr(t), set()).add(int(r["week"]))
    if byes:
        all_weeks = set(range(1, max(max(w) for w in byes.values()) + 1))
        byes = {t: (sorted(all_weeks - w) or [None])[0] for t, w in byes.items()}

    players = build_players(fc, sleeper, byes, fp)
    n_proj = assign_points(players, proj, curve, cfg["scoring"], sack_rates,
                           lg_sack_rate, p6_rate, fumble_ratio)
    notes.append(f"points: {n_proj} players from Sleeper projections, "
                 f"{sum(1 for p in players if p.get('points_source') == 'curve')} from historical curve")
    assign_tiers(players)
    vorp_to_dollars(cfg, players, starter_rank, roster_rank)
    assign_sources(players, ffc, proj, wf, espn)   # after dollars: proj rank uses vorp_roster
    news = fetch_news(args.force, notes)
    assign_intel(players, news)
    qb_penalty_report(cfg, players, proj, sack_rates, lg_sack_rate, p6_rate, fumble_ratio)
    history = league_history_file(players)

    for n in notes:
        log.info("[%s] %s", cfg["slug"], n)

    players.sort(key=lambda p: (-p["value"], -(p["vorp"] or 0), p["pos_rank"]))
    out = {
        "meta": {
            "league": cfg["name"], "slug": cfg["slug"], "yahoo_league_id": cfg["yahoo_league_id"],
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "num_teams": cfg["teams"], "budget": cfg["budget"],
            "roster": cfg["roster_slots"], "scoring": cfg["scoring"],
            "replacement_rank_starter": {k: round(v, 1) for k, v in starter_rank.items()},
            "replacement_rank_roster": {k: round(v, 1) for k, v in roster_rank.items()},
            "dollar_replacement": DOLLAR_REPLACEMENT,
            "news": [{"date": n["date"], "headline": n["headline"]} for n in news[:14]],
            "sources": notes, "players": len(players),
        },
        "players": [{
            "player_id": p["player_id"], "name": p["name"], "position": p["position"],
            "team": p["team"], "bye": p["bye"], "value": p["value"], "vorp": p["vorp"],
            "points": p["points"], "points_source": p.get("points_source"),
            "tier": p["tier"], "sources": p.get("sources"),
            "consensus_rank": p.get("consensus_rank"), "mkt_rank": p.get("mkt_rank"),
            "spread": p.get("spread"), "disagreement": p.get("disagreement"),
            "wf_value": p.get("wf_value"), "espn_aav": p.get("espn_aav"),
            "mkt_class": p.get("mkt_class"), "injury_status": p.get("injury_status"),
            "injury_note": p.get("injury_note"), "handcuff_of": p.get("handcuff_of"),
            "handcuff_pid": p.get("handcuff_pid"), "news": p.get("news"),
            "ecr": p["ecr"], "ecr_stddev": p["ecr_stddev"],
            "ecr_best": p["ecr_best"], "ecr_worst": p["ecr_worst"],
            "trend30": p["trend30"], "roster_pct": p["roster_pct"],
        } for p in players],
        "history": history,
    }
    emit(cfg, out, args.no_embed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
