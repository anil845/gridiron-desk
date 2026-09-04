"""Waiver intelligence engine: the five-leg triangulation core.

    python intel_week.py --league papi-chulo --week 1

Builds the free-agent pool (relevant players NOT on any roster in the league),
joins five INDEPENDENT signals onto each candidate, and prints a verdict +
FAB bid — not a projections list. The intelligence is the disagreement
between legs, exactly like the draft board's source model.

Legs:
  ours   weekly points + improvement over MY weakest startable player
  expert FantasyPros weekly ECR (scraped ecrData) + rank stddev
  market ESPN roster% + 24h velocity  (what the crowd is DOING)
  usage  nflverse snap% delta week-over-week (dormant until wk>=2)
  ctx    injury to the man ahead (handcuff), trending adds, news
"""

import argparse
import json
import os
import re
from collections import defaultdict

import build_board as bb
import build_week as bw

FP_WEEKLY_URL = "https://www.fantasypros.com/nfl/rankings/ppr-flex.php"
ESPN_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
            f"{bb.SEASON}/segments/0/leaguedefaults/3?view=kona_player_info")
SNAP_URL = "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_{season}.csv.gz"

QUALITY = 6.0   # weekly points floor to even be a "relevant" FA


# --- signal fetchers (cached, fail-soft) ------------------------------------
def fetch_fp_weekly(force):
    """FantasyPros weekly PPR-flex ECR from the embedded ecrData JSON."""
    import requests
    path = os.path.join(bb.DATA_DIR, "fp_weekly_flex.html")

    def _get():
        r = requests.get(FP_WEEKLY_URL, headers={"User-Agent": bb.USER_AGENT}, timeout=60)
        r.raise_for_status()
        if "ecrData" not in r.text:
            raise ValueError("no ecrData")
        return r.text
    try:
        text = bb._cached(path, _get, force, max_age_h=12)
        data = json.loads(re.search(r"var ecrData\s*=\s*(\{.*?\});\s*\n", text, re.S).group(1))
        return {bb.norm_name(p["player_name"]): {"ecr": bb._num(p.get("rank_ecr")),
                "std": bb._num(p.get("rank_std")), "pos": p.get("player_position_id")}
                for p in data.get("players", [])}, f"FantasyPros weekly: {len(data.get('players', []))} rows"
    except Exception as exc:  # noqa: BLE001
        return {}, f"FantasyPros weekly: unavailable ({exc})"


def fetch_espn_market(force):
    import requests
    path = os.path.join(bb.DATA_DIR, "espn_market.json")

    def _get():
        flt = json.dumps({"players": {"limit": 600, "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}})
        r = requests.get(ESPN_URL, headers={"x-fantasy-filter": flt, "User-Agent": bb.USER_AGENT}, timeout=60)
        r.raise_for_status()
        return r.text
    try:
        d = json.loads(bb._cached(path, _get, force, max_age_h=6))
        out = {}
        for p in d.get("players", []):
            pl = p.get("player") or {}
            own = pl.get("ownership") or {}
            out[bb.norm_name(pl.get("fullName", ""))] = {
                "owned": own.get("percentOwned"), "started": own.get("percentStarted"),
                "vel": own.get("percentChange")}
        return out, f"ESPN market: {len(out)} rows"
    except Exception as exc:  # noqa: BLE001
        return {}, f"ESPN market: unavailable ({exc})"


def fetch_usage(week, force):
    """Snap% delta from prior week. Empty (dormant) for week 1."""
    import csv
    import gzip
    if week < 2:
        return {}, "usage: dormant (need >=2 weeks played)"
    path = os.path.join(bb.DATA_DIR, f"snap_counts_{bb.SEASON}.csv.gz")
    try:
        content = bb._cached(path, lambda: bb.http_get(SNAP_URL.format(season=bb.SEASON),
                             expect="response", stream=True).content, force, binary=True)
        by = defaultdict(dict)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for x in csv.DictReader(fh):
                pct = x.get("offense_pct")
                if pct:
                    try:
                        by[bb.norm_name(x["player"])][int(x["week"])] = float(pct)
                    except ValueError:
                        pass
        out = {}
        for nm, wk in by.items():
            if week - 1 in wk and week - 2 in wk:
                out[nm] = round(wk[week - 1] - wk[week - 2], 2)
            elif week - 1 in wk:
                out[nm] = None
        return out, f"usage: snap deltas for {len(out)} players (wk{week-1} vs wk{week-2})"
    except Exception as exc:  # noqa: BLE001
        return {}, f"usage: unavailable ({exc})"


# --- the engine -------------------------------------------------------------
def verdict(c, mode):
    """Triangulate the five legs into an action. mode: papi (triage) / league (speculation)."""
    up = c["usage"] is not None and c["usage"] >= 0.08
    hot = c["vel"] is not None and c["vel"] >= 0.5
    owned = c["owned"] or 0
    exp_good = c["ecr"] is not None and c["ecr"] <= 120
    ours_good = c["improve"] > 0
    if c.get("handcuff_of_mine"):
        return "INSURANCE", f"backs up your {c['handcuff_of_mine']}"
    if exp_good and not ours_good and c["our_pts"] and c["our_pts"] < QUALITY + 2:
        return "FORMAT MIRAGE", "experts like him, our scoring/roster doesn't"
    if up and not hot and owned < 40:
        return "EARLY", "usage rising, market hasn't caught up — bid low"
    if up and hot:
        return "CONTESTED", "usage + market both rising — window closing, pay fair"
    if hot and not up:
        return "FRENZY", "market surging on no usage change — fade"
    if ours_good and exp_good:
        return "SOLID", "improves your lineup, experts agree"
    return "WATCH", "monitor"


def fab_bid(c, budget, mode):
    """Suggested FAB as % of remaining budget, scaled by improvement + verdict."""
    imp = max(0, c["improve"])
    base = min(0.25, imp / 40.0)                      # up to 25% for a big upgrade
    mult = {"INSURANCE": 0.4, "EARLY": 0.8, "CONTESTED": 1.2, "SOLID": 1.0,
            "FRENZY": 0.1, "FORMAT MIRAGE": 0.05, "WATCH": 0.2}.get(c["verdict"], 0.5)
    bid = round(budget * base * mult)
    return max(0, bid), max(bid + 2, round(bid * 1.5))  # (open, walk-away)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    cfg = bb.load_league(args.league)
    mode = "papi" if cfg["teams"] >= 14 or len([s for s in cfg["roster_slots"] if s["slot"] == "BN"]) <= 3 else "league"
    rosters, src = bw.load_rosters(args.league)
    board = {p["player_id"]: p for p in json.load(open(f"board_data_{args.league}.json", encoding="utf-8"))["players"]}
    proj = bw.fetch_week_proj(args.week, args.force)
    notes = []
    p6d, p6r = bb.pick_six_data(False, notes)
    _, srates, lgr, fr = bb.load_history(False, p6d)
    fp, n1 = fetch_fp_weekly(args.force)
    market, n2 = fetch_espn_market(args.force)
    usage, n3 = fetch_usage(args.week, args.force)
    for n in (n1, n2, n3):
        print("·", n)

    rostered = {pk["pid"] for t in rosters["teams"] for pk in t["picks"]}
    me = next(t for t in rosters["teams"] if t.get("is_me"))
    my_budget = me.get("remaining", 100) if False else 100  # FAB budget; Yahoo sync will make exact
    # my weakest startable weekly points per position (the bar a FA must clear)
    mine = []
    for pk in me["picks"]:
        pts, _ = bw.week_points(pk, proj, cfg["scoring"], srates, lgr, p6r, fr)
        mine.append(dict(pk, wk=pts))
    slots, _ = bw.optimal_lineup([dict(p, wk_pts=p["wk"]) for p in mine], cfg["roster_slots"])
    weakest = {}
    for s in slots:
        if s["pick"]:
            for pos in s["elig"]:
                weakest[pos] = min(weakest.get(pos, 1e9), s["pick"]["wk_pts"])
    my_studs = {board.get(pk["pid"], {}).get("name") for pk in me["picks"]}

    cands = []
    for pid, e in proj.items():
        if pid in rostered:
            continue
        pl = e.get("player", {})
        pos = pl.get("position")
        if pos not in bb.SKILL + ("K", "DEF"):
            continue
        name = f"{pl.get('first_name', '')} {pl.get('last_name', '')}".strip() if pos != "DEF" else pl.get("last_name", "")
        pk = {"pid": pid, "name": name, "position": pos}
        pts, opp = bw.week_points(pk, proj, cfg["scoring"], srates, lgr, p6r, fr)
        if pts is None or pts < QUALITY:
            continue
        nn = bb.norm_name(name)
        bd = board.get(pid, {})
        improve = round(pts - weakest.get(pos, pts), 1) if pos in weakest else 0.0
        c = {"name": name, "pos": pos, "opp": opp, "our_pts": pts, "improve": improve,
             "ecr": fp.get(nn, {}).get("ecr"), "ecr_std": fp.get(nn, {}).get("std"),
             "owned": market.get(nn, {}).get("owned"), "started": market.get(nn, {}).get("started"),
             "vel": market.get(nn, {}).get("vel"), "usage": usage.get(nn),
             "trending": bd.get("trending"), "injury": bd.get("injury_status"),
             "handcuff_of_mine": bd.get("handcuff_of") if bd.get("handcuff_of") in my_studs else None}
        c["verdict"], c["why"] = verdict(c, mode)
        c["bid"], c["walk"] = fab_bid(c, my_budget, mode)
        cands.append(c)

    order = {"INSURANCE": 0, "EARLY": 1, "CONTESTED": 2, "SOLID": 3, "WATCH": 4, "FRENZY": 5, "FORMAT MIRAGE": 6}
    skill = [c for c in cands if c["pos"] in bb.SKILL]
    stream = [c for c in cands if c["pos"] in ("K", "DEF")]
    skill.sort(key=lambda c: (order.get(c["verdict"], 9), -c["improve"], -(c["our_pts"] or 0)))

    print(f"\n=== {cfg['name']} · week {args.week} waiver intelligence ({mode} mode, rosters: {src}) ===")
    print("my weakest starters: " + " ".join(f"{p}={round(v,1)}" for p, v in sorted(weakest.items())))

    print(f"\nWAIVER BOARD (skill)\n{'VERDICT':13s} {'player':22s} {'pos':3s} {'wkPts':>5s} {'+me':>4s} {'FP':>4s} {'own%':>4s} {'vel':>5s} {'usg':>5s}  bid  why")
    for c in skill[:args.top]:
        ecr = str(int(c["ecr"])) if c["ecr"] else "-"
        own = str(round(c["owned"])) if c["owned"] is not None else "-"
        vel = f"{c['vel']:+.1f}" if c["vel"] is not None else "-"
        usg = f"{c['usage']:+.2f}" if c["usage"] is not None else "-"
        print(f"{c['verdict']:13s} {c['name'][:22]:22s} {c['pos']:3s} {c['our_pts']:>5.1f} {c['improve']:>+4.1f} "
              f"{ecr:>4s} {own:>4s} {vel:>5s} {usg:>5s}  ${c['bid']:>2d}  {c['why']}")

    need_kd = [s["slot"] for s in slots if not s["pick"] and s["slot"] in ("K", "DEF")]
    print(f"\nK/DEF STREAM CARD" + (f"  (you NEED: {' '.join(need_kd)})" if need_kd else ""))
    for pos in ("K", "DEF"):
        best = sorted([c for c in stream if c["pos"] == pos], key=lambda c: -c["our_pts"])[:4]
        print(f"  {pos}: " + " · ".join(f"{c['name']} {c['our_pts']:.1f} vs {c['opp'] or '?'}" for c in best))


if __name__ == "__main__":
    main()
