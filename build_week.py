"""Weekly in-season report v1: optimal lineup, start/sit, matchup win
probability, K/DEF matchup check — all through the league's own scoring.

    python build_week.py --league papi-chulo --week 1 --opponent "JSN Bourne"

Rosters come from seasons/<slug>_rosters.json if present (maintained by the
weekly transaction paste), else fall back to the draft seed
seasons/<slug>_2026_draft.json. Weekly projections from Sleeper (per-week,
all positions), skill players scored via the same canonicalization as the
draft board (sack estimation, pick-six rate, fumble ratio included).
"""

import argparse
import json
import math
import os

import build_board as bb

# K/DEF category scoring. Verified for the-league (settings PDF). Papi's K/DEF
# table was never captured — assumed identical (Yahoo default-ish); verify.
K_SCORING = {"fgm_0_19": 3, "fgm_20_29": 3, "fgm_30_39": 3, "fgm_40_49": 4, "fgm_50p": 5,
             "fgmiss_0_19": -3, "fgmiss_20_29": -3, "fgmiss_30_39": -2, "fgmiss_40_49": -1,
             "xpm": 1, "xpmiss": -1}
WEEKLY_SIGMA = 25.0   # historical per-team weekly total stddev; win prob uses N(diff, sqrt2*sigma)

WEEK_PROJ_URL = ("https://api.sleeper.app/projections/nfl/{year}/{week}?season_type=regular"
                 "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF")


def fetch_week_proj(week, force):
    import requests
    path = os.path.join(bb.DATA_DIR, f"sleeper_proj_{bb.SEASON}_w{week}.json")

    def _get():
        r = requests.get(WEEK_PROJ_URL.format(year=bb.SEASON, week=week),
                         headers={"User-Agent": bb.USER_AGENT}, timeout=60)
        r.raise_for_status()
        return r.text
    rows = json.loads(bb._cached(path, _get, force, max_age_h=12))
    return {str(e["player_id"]): e for e in rows if e.get("stats")}


def score_k(st):
    return sum((st.get(k) or 0) * w for k, w in K_SCORING.items())


def score_def(st):
    g = lambda *ks: next(((st.get(k) or 0) for k in ks if st.get(k) is not None), 0)  # noqa: E731
    pts = (g("sack") * 1 + g("int", "def_int") * 2 + g("fum_rec", "def_fum_rec") * 2
           + g("def_td") * 6 + g("safe", "safety") * 2 + g("blk_kick") * 2
           + (g("def_kr_td") + g("def_pr_td")) * 6)
    pa = g("pts_allow", "pts_allowed") or 21
    tier = 10 if pa <= 0.5 else 7 if pa < 7 else 4 if pa < 14 else 1 if pa < 21 else 0 if pa < 28 else -1 if pa < 35 else -4
    return pts + tier


def load_rosters(slug):
    live = os.path.join("seasons", f"{slug}_rosters.json")
    seed = os.path.join("seasons", f"{slug}_2026_draft.json")
    path = live if os.path.exists(live) else seed
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), os.path.basename(path)


def week_points(pick, proj, scoring, srates, lgr, p6r, fr):
    e = proj.get(pick["pid"])
    if not e:
        return None, None
    st, pos = e["stats"], pick["position"]
    if pos in bb.SKILL:
        canon = bb.canonical_projection(st, pick["name"], srates, lgr, p6r, fr)
        return round(bb.score(canon, scoring), 1), e.get("opponent")
    if pos == "K":
        return round(score_k(st), 1), e.get("opponent")
    if pos == "DEF":
        return round(score_def(st), 1), e.get("opponent")
    return None, None


def optimal_lineup(players, roster_slots):
    slots = [dict(s, pick=None) for s in roster_slots if s["slot"] != "BN"]
    pool = sorted([p for p in players if p.get("wk_pts") is not None], key=lambda p: -p["wk_pts"])
    bench = []
    for p in pool:
        placed = False
        for s in slots:
            if s["pick"] is None and p["position"] in s["elig"]:
                s["pick"] = p
                placed = True
                break
        if not placed:
            bench.append(p)
    for p in players:
        if p.get("wk_pts") is None and p not in bench:
            bench.append(p)
    return slots, bench


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--opponent", required=True, help="opponent team name (prefix ok)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = bb.load_league(args.league)
    rosters, src = load_rosters(args.league)
    proj = fetch_week_proj(args.week, args.force)
    notes = []
    p6d, p6r = bb.pick_six_data(False, notes)
    _, srates, lgr, fr = bb.load_history(False, p6d)

    def team(name_or_me):
        if name_or_me == "me":
            return next(t for t in rosters["teams"] if t.get("is_me"))
        cands = [t for t in rosters["teams"] if t["name"].lower().startswith(name_or_me.lower())]
        if len(cands) != 1:
            raise SystemExit(f"opponent '{name_or_me}' matched {len(cands)} teams")
        return cands[0]

    def prep(t):
        ps = []
        for pk in t["picks"]:
            pts, opp = week_points(pk, proj, cfg["scoring"], srates, lgr, p6r, fr)
            ps.append(dict(pk, wk_pts=pts, wk_opp=opp))
        return ps

    me, opp = team("me"), team(args.opponent)
    mine, theirs = prep(me), prep(opp)
    my_slots, my_bench = optimal_lineup(mine, cfg["roster_slots"])
    op_slots, _ = optimal_lineup(theirs, cfg["roster_slots"])
    my_total = sum(s["pick"]["wk_pts"] for s in my_slots if s["pick"])
    op_total = sum(s["pick"]["wk_pts"] for s in op_slots if s["pick"])
    diff = my_total - op_total
    winp = 0.5 * (1 + math.erf(diff / (WEEKLY_SIGMA * math.sqrt(2) * math.sqrt(2)) / 1.0))

    print(f"=== {cfg['name']} · week {args.week} · {me['name']} vs {opp['name']} (rosters: {src}) ===")
    print(f"\nMY OPTIMAL LINEUP  ({my_total:.1f} pts)")
    for s in my_slots:
        p = s["pick"]
        print(f"  {s['slot']:5s} {p['name'] if p else '--':24s} "
              f"{(str(p['wk_pts']) if p else ''):>6s}  {('vs ' + (p['wk_opp'] or 'BYE?')) if p else ''}")
    print("  bench:", ", ".join(f"{p['name']} {p['wk_pts'] if p['wk_pts'] is not None else 'n/a'}" for p in my_bench) or "-")
    # start/sit close calls: bench player within 2 pts of a starter he could replace
    for p in my_bench:
        if p.get("wk_pts") is None:
            continue
        for s in my_slots:
            if s["pick"] and p["position"] in s["elig"] and 0 < s["pick"]["wk_pts"] - p["wk_pts"] < 2:
                print(f"  ~ CLOSE CALL: {p['name']} ({p['wk_pts']}) within 2 of {s['pick']['name']} ({s['pick']['wk_pts']}) at {s['slot']} — judgment, not math")
    missing = [p["name"] for p in mine if p.get("wk_pts") is None]
    if missing:
        print("  ! no projection (bye/inactive/empty slot):", ", ".join(missing))

    print(f"\nOPPONENT OPTIMAL   ({op_total:.1f} pts)")
    for s in op_slots:
        p = s["pick"]
        print(f"  {s['slot']:5s} {p['name'] if p else '--':24s} {(str(p['wk_pts']) if p else ''):>6s}")

    fav = "FAVORITE" if diff > 0 else "underdog"
    print(f"\nMATCHUP: {my_total:.1f} vs {op_total:.1f}  ->  {winp*100:.0f}% {fav}")
    print("STRATEGY:", "protect the lead — start floors, avoid volatile boom/bust plays"
          if diff > 6 else "coin flip — start your best players, ignore variance games"
          if diff > -6 else "underdog — start ceilings, stack correlated players, embrace variance")


if __name__ == "__main__":
    main()
