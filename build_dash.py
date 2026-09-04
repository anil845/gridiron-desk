"""Render the in-season daily dashboard: one elegant strip page, both leagues
with a client-side toggle, data embedded (static-bake, like the draft board).

    python build_dash.py --week 1

Computes matchup + waiver intelligence + stream + context per league via
build_week / intel_week, assembles DASH_DATA, and writes dash.html (also
deployed to the war room at /dash). Opponent per league comes from
seasons/matchups.json (or Yahoo once its API is live); leagues without a
known opponent render a calm 'awaiting schedule' state.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re

import build_board as bb
import build_week as bw
import intel_week as iw

LEAGUES = ["papi-chulo", "the-league"]
MATCHUPS = os.path.join("seasons", "matchups.json")
SNAPDIR = os.path.join("seasons", "snapshots")
MOVE_THRESHOLD = 2.0   # weekly-projection points to count as a mover
INJ_WORDS = re.compile(r"\b(out|doubtful|questionable|limited|dnp|ir|injured reserve|"
                       r"suspend|placed on|ruled out|won'?t play|inactive|carted)\b", re.I)


def _next_lock(week):
    """Earliest kickoff of the week from the schedule -> (date, days_away)."""
    path = os.path.join(bb.DATA_DIR, "nflverse_games.csv")
    if not os.path.exists(path):
        return None, None
    days = []
    for r in csv.DictReader(io.StringIO(open(path, encoding="utf-8").read())):
        if r.get("season") == str(bb.SEASON) and r.get("game_type") == "REG" and r.get("week") == str(week) and r.get("gameday"):
            try:
                days.append(datetime.date.fromisoformat(r["gameday"]))
            except ValueError:
                pass
    if not days:
        return None, None
    first = min(days)
    return first.isoformat(), (first - datetime.date.today()).days


def _snapshot_diff(slug, board, proj, week, srates, lgr, p6r, fr, scoring):
    """Write today's snapshot (proj pts + injury per player); diff vs the most
    recent OLDER snapshot to surface movers. First run has no prior -> []."""
    os.makedirs(SNAPDIR, exist_ok=True)
    today = {}
    for pid, e in proj.items():
        pl = e.get("player", {})
        pos = pl.get("position")
        if pos not in bb.SKILL + ("K", "DEF"):
            continue
        name = (f"{pl.get('first_name', '')} {pl.get('last_name', '')}".strip() if pos != "DEF" else pl.get("last_name", ""))
        pts, _o = bw.week_points({"pid": pid, "name": name, "position": pos}, proj, scoring, srates, lgr, p6r, fr)
        today[pid] = {"name": name, "pos": pos, "pts": round(pts, 1) if pts is not None else None,
                      "inj": board.get(pid, {}).get("injury_status")}
    stamp = datetime.date.today().isoformat()
    cur_path = os.path.join(SNAPDIR, f"{slug}_w{week}_{stamp}.json")
    priors = sorted(p for p in os.listdir(SNAPDIR) if p.startswith(f"{slug}_w{week}_") and p != os.path.basename(cur_path))
    movers = []
    if priors:
        prev = json.load(open(os.path.join(SNAPDIR, priors[-1]), encoding="utf-8"))
        for pid, now in today.items():
            was = prev.get(pid)
            if not was:
                continue
            if now["pts"] is not None and was.get("pts") is not None and abs(now["pts"] - was["pts"]) >= MOVE_THRESHOLD:
                movers.append({"pid": pid, "name": now["name"], "pos": now["pos"],
                               "delta": round(now["pts"] - was["pts"], 1), "pts": now["pts"], "kind": "proj"})
            elif now["inj"] != was.get("inj") and (now["inj"] or was.get("inj")):
                movers.append({"pid": pid, "name": now["name"], "pos": now["pos"],
                               "from": was.get("inj"), "to": now["inj"], "kind": "status"})
    json.dump(today, open(cur_path, "w", encoding="utf-8"))
    return movers


def _today(out, movers, board, me, week):
    """The punch-list: only things that need a decision, ranked, capped at 6.
    Empty list = the calm all-clear."""
    items = []
    lock_date, days = _next_lock(week)
    # 1) roster holes (open starting slots)
    open_start = [s for s in (out.get("me", {}).get("open") or []) if s not in ("BN",)]
    if open_start:
        items.append({"u": "act", "text": f"Fill your open {', '.join(open_start)} — lineup incomplete"})
    # 2) injured starters (my optimal lineup)
    for p in (out.get("me", {}).get("lineup") or []):
        if p.get("inj"):
            sev = "act" if p["inj"] in ("Out", "IR", "Doubtful") else "soon"
            items.append({"u": sev, "text": f"{p['name']} is {p['inj']} — {'replace' if sev=='act' else 'monitor'} at {p.get('slot','')}"})
    # 3) projection/status movers on MY players
    my_pids = {pk["pid"] for pk in me["picks"]} if me else set()
    for m in movers:
        if m["pid"] in my_pids:
            if m["kind"] == "proj":
                items.append({"u": "fyi", "text": f"{m['name']} projection {'up' if m['delta']>0 else 'down'} {abs(m['delta'])} to {m['pts']}"})
            else:
                items.append({"u": "soon", "text": f"{m['name']} status: {m.get('from') or 'active'} -> {m.get('to') or 'active'}"})
    # 4) waiver claim window (surface Sun-Tue, both leagues process Tuesday)
    dow = datetime.date.today().weekday()  # Mon=0
    if dow in (6, 0, 1):  # Sun/Mon/Tue
        actionable = [w for w in (out.get("waivers") or []) if w["verdict"] not in ("WATCH", "FORMAT MIRAGE")]
        if actionable:
            items.append({"u": "soon", "text": f"Waivers process Tuesday — {len(actionable)} live target"
                          + ("s" if len(actionable) != 1 else "") + f", top: {actionable[0]['name']}"})
    # 5) news touching my roster with injury/status keywords
    for n in (out.get("intel") or []):
        if INJ_WORDS.search(n["headline"]):
            items.append({"u": "soon", "text": f"{n['player']}: {n['headline'].split(': ', 1)[-1][:80]}"})
    rank = {"act": 0, "soon": 1, "fyi": 2}
    items.sort(key=lambda x: rank.get(x["u"], 3))
    return {"lock_date": lock_date, "lock_days": days, "items": items[:6]}


def compute_league(slug, week, force, ctx):
    cfg = bb.load_league(slug)
    out = {"name": cfg["name"], "slug": slug, "week": week, "teams": cfg["teams"]}
    try:
        rosters, src = bw.load_rosters(slug)
    except Exception:
        out["state"] = "no rosters yet — drafts soon"
        return out
    board = {p["player_id"]: p for p in json.load(open(f"board_data_{slug}.json", encoding="utf-8"))["players"]}
    proj = bw.fetch_week_proj(week, force)
    srates, lgr, p6r, fr = ctx

    me = next((t for t in rosters["teams"] if t.get("is_me")), None)
    opp_name = (ctx_matchups().get(slug) or {}).get(str(week))
    opp = None
    if opp_name:
        opp = next((t for t in rosters["teams"] if t["name"].lower().startswith(opp_name.lower())), None)

    def lineup(team):
        ps = []
        for pk in team["picks"]:
            pts, o = bw.week_points(pk, proj, cfg["scoring"], srates, lgr, p6r, fr)
            bd = board.get(pk["pid"], {})
            ps.append({"name": pk["name"], "position": bd.get("position") or pk.get("position"),
                       "wk_pts": pts, "opp": o, "inj": bd.get("injury_status")})
        slots, bench = bw.optimal_lineup(ps, cfg["roster_slots"])
        starters = [{"slot": s["slot"], "name": s["pick"]["name"], "pos": s["pick"]["position"],
                     "pts": s["pick"]["wk_pts"], "opp": s["pick"].get("opp"), "inj": s["pick"].get("inj")}
                    for s in slots if s["pick"]]
        open_slots = [s["slot"] for s in slots if not s["pick"]]
        total = round(sum(s["pick"]["wk_pts"] for s in slots if s["pick"]), 1)
        b = [{"name": x["name"], "pos": x["position"], "pts": x["wk_pts"]} for x in bench if x.get("wk_pts")]
        return starters, total, b, open_slots

    if me:
        my_line, my_tot, my_bench, my_open = lineup(me)
        out["me"] = {"name": me["name"], "lineup": my_line, "total": my_tot,
                     "bench": my_bench[:6], "open": my_open}
        if opp:
            op_line, op_tot, _b, _o = lineup(opp)
            out["opp"] = {"name": opp["name"], "lineup": op_line, "total": op_tot,
                          "roster_as_of": rosters.get("exported_at", src)}
            import math
            diff = my_tot - op_tot
            out["winp"] = round(50 * (1 + math.erf(diff / (25 * 2))))
            out["directive"] = ("Protect the lead — start floors, skip boom/bust." if diff > 6
                                else "Coin flip — start your best, ignore variance." if diff > -6
                                else "Underdog — start ceilings, stack correlated players.")
        else:
            out["opp"] = {"name": opp_name or "awaiting schedule", "lineup": [], "total": None}

    # standings + season tracking
    out["standings"] = _standings(slug, rosters, me, opp_name)
    out["activity"] = _activity(slug, board)
    if out.get("opp") and out["standings"]:
        rec = next((t for t in out["standings"] if t["name"].lower().startswith((opp_name or "").lower())), None)
        if rec:
            out["opp"]["record"] = f"{rec['wins']}-{rec['losses']}" + (f"-{rec['ties']}" if rec['ties'] else "")
            out["opp"]["pf"] = rec["pf"]
    if me and out["standings"]:
        mine = next((t for t in out["standings"] if t.get("is_me")), None)
        if mine:
            out["my_record"] = {"rec": f"{mine['wins']}-{mine['losses']}" + (f"-{mine['ties']}" if mine['ties'] else ""),
                                "rank": mine["rank"], "pf": mine["pf"], "pa": mine["pa"], "of": len(out["standings"])}

    # waiver intelligence (reuse intel_week internals lightly)
    out["waivers"], out["stream"] = _waivers(cfg, slug, week, board, proj, me, ctx, force)
    out["intel"], out["podcast"] = _context(slug, board, me, opp)
    movers = _snapshot_diff(slug, board, proj, week, srates, lgr, p6r, fr, cfg["scoring"])
    out["movers"] = [m for m in movers if m["pid"] in ({pk["pid"] for pk in me["picks"]} if me else set())
                     or board.get(m["pid"], {}).get("value", 0) >= 12][:10]
    out["today"] = _today(out, movers, board, me, week)
    return out


def _activity(slug, board):
    """League transaction feed + per-team FAB spent, from seasons/<slug>_transactions.json
    (Yahoo sync or manual paste). 'notable' flags moves for players our board
    values highly — i.e. a rival grabbing a real asset, not a dart."""
    path = os.path.join("seasons", f"{slug}_transactions.json")
    if not os.path.exists(path):
        return None
    txns = json.load(open(path, encoding="utf-8")).get("txns", [])
    valmap = {bb.norm_name(p["name"]): p.get("value", 0) for p in board.values()}
    fab_spent = {}
    recent = []
    for t in txns:
        if t.get("fab"):
            fab_spent[t.get("team")] = fab_spent.get(t.get("team"), 0) + t["fab"]
        val = max((valmap.get(bb.norm_name(a["name"]), 0) for a in t.get("adds", [])), default=0)
        recent.append({"team": t.get("team"), "fab": t.get("fab"),
                       "adds": [a["name"] for a in t.get("adds", [])],
                       "drops": [d["name"] for d in t.get("drops", [])],
                       "date": t.get("date"), "type": t.get("type"), "notable": val >= 15})
    recent.reverse()   # newest first
    top_fab = sorted(fab_spent.items(), key=lambda kv: -kv[1])[:5]
    return {"recent": recent[:12], "fab_spent": [{"team": k, "spent": v} for k, v in top_fab], "n": len(txns)}


def _standings(slug, rosters, me, opp_name):
    """Live standings from seasons/<slug>_standings.json (Yahoo sync or manual);
    preseason fallback synthesizes 0-0-0 from the roster list so the strip
    renders from week 0."""
    path = os.path.join("seasons", f"{slug}_standings.json")
    my_name = me["name"] if me else None
    if os.path.exists(path):
        teams = json.load(open(path, encoding="utf-8")).get("teams", [])
    else:
        teams = [{"name": t["name"], "rank": i + 1, "wins": 0, "losses": 0, "ties": 0,
                  "pf": 0.0, "pa": 0.0, "streak": ""} for i, t in enumerate(rosters["teams"])]
    for t in teams:
        t["is_me"] = (t["name"] == my_name)
    return teams


def _context(slug, board, me, opp):
    """News + podcast stances for players on my roster, my opponent's, or hot
    waiver names. Filtered so the strip is signal, not a firehose."""
    relevant = set()
    for t in (me, opp):
        if t:
            for pk in t["picks"]:
                relevant.add(bb.norm_name(pk["name"]))
    news = []
    for p in board.values():
        if p.get("news") and bb.norm_name(p["name"]) in relevant:
            news.append({"player": p["name"], "headline": p["news"]})
    pods = []
    cpath = os.path.join("seasons", "podcast", "consensus.json")
    if os.path.exists(cpath):
        pc = json.load(open(cpath, encoding="utf-8"))
        for pl in pc.get("players", []):
            if pl["norm"] in relevant and abs(pl["score"]) >= 2:
                top = pl["stances"][0] if pl["stances"] else {}
                pods.append({"player": pl["player"], "lean": pl["lean"], "score": pl["score"],
                             "n": pl["n_shows"], "quote": top.get("quote", "")})
    return news[:8], sorted(pods, key=lambda p: -abs(p["score"]))[:8]


_MATCHUP_CACHE = None


def ctx_matchups():
    global _MATCHUP_CACHE
    if _MATCHUP_CACHE is None:
        _MATCHUP_CACHE = json.load(open(MATCHUPS, encoding="utf-8")) if os.path.exists(MATCHUPS) else {}
    return _MATCHUP_CACHE


def _waivers(cfg, slug, week, board, proj, me, ctx, force):
    srates, lgr, p6r, fr = ctx
    fp, _ = iw.fetch_fp_weekly(force)
    market, _ = iw.fetch_espn_market(force)
    usage, _ = iw.fetch_usage(week, force)
    rosters, _ = bw.load_rosters(slug)
    rostered = {pk["pid"] for t in rosters["teams"] for pk in t["picks"]}
    my_studs = {board.get(pk["pid"], {}).get("name") for pk in me["picks"]} if me else set()
    mine = []
    if me:
        for pk in me["picks"]:
            pts, _o = bw.week_points(pk, proj, cfg["scoring"], srates, lgr, p6r, fr)
            mine.append({"name": pk["name"], "position": board.get(pk["pid"], {}).get("position") or pk.get("position"), "wk_pts": pts})
        slots, _b = bw.optimal_lineup(mine, cfg["roster_slots"])
        weakest = {}
        for s in slots:
            if s["pick"]:
                for pos in s["elig"]:
                    weakest[pos] = min(weakest.get(pos, 1e9), s["pick"]["wk_pts"])
    else:
        weakest = {}
    skill, stream = [], []
    for pid, e in proj.items():
        if pid in rostered:
            continue
        pl = e.get("player", {})
        pos = pl.get("position")
        if pos not in bb.SKILL + ("K", "DEF"):
            continue
        name = (f"{pl.get('first_name', '')} {pl.get('last_name', '')}".strip() if pos != "DEF" else pl.get("last_name", ""))
        pts, opp = bw.week_points({"pid": pid, "name": name, "position": pos}, proj, cfg["scoring"], srates, lgr, p6r, fr)
        if pts is None or pts < iw.QUALITY:
            continue
        nn = bb.norm_name(name)
        bd = board.get(pid, {})
        c = {"name": name, "pos": pos, "opp": opp, "our_pts": round(pts, 1),
             "improve": round(pts - weakest.get(pos, pts), 1) if pos in weakest else 0.0,
             "ecr": fp.get(nn, {}).get("ecr"), "owned": market.get(nn, {}).get("owned"),
             "vel": market.get(nn, {}).get("vel"), "usage": usage.get(nn),
             "trending": bd.get("trending"), "injury": bd.get("injury_status"),
             "handcuff_of_mine": bd.get("handcuff_of") if bd.get("handcuff_of") in my_studs else None}
        c["verdict"], c["why"] = iw.verdict(c, slug)
        c["bid"], c["walk"] = iw.fab_bid(c, 100, slug)
        (stream if pos in ("K", "DEF") else skill).append(c)
    order = {"INSURANCE": 0, "EARLY": 1, "CONTESTED": 2, "SOLID": 3, "WATCH": 4, "FRENZY": 5, "FORMAT MIRAGE": 6}
    skill.sort(key=lambda c: (order.get(c["verdict"], 9), -c["improve"], -(c["our_pts"] or 0)))
    top_stream = {}
    for pos in ("K", "DEF"):
        top_stream[pos] = sorted([c for c in stream if c["pos"] == pos], key=lambda c: -c["our_pts"])[:3]
    return skill[:10], top_stream


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-embed", action="store_true")
    args = ap.parse_args()
    notes = []
    p6d, p6r = bb.pick_six_data(False, notes)
    _, srates, lgr, fr = bb.load_history(False, p6d)
    ctx = (srates, lgr, p6r, fr)

    data = {"built": datetime.datetime.now().isoformat(timespec="seconds"), "week": args.week, "leagues": {}}
    for slug in LEAGUES:
        try:
            data["leagues"][slug] = compute_league(slug, args.week, args.force, ctx)
            print("built", slug)
        except Exception as exc:  # noqa: BLE001
            print("skip", slug, exc)
            data["leagues"][slug] = {"name": slug, "slug": slug, "state": f"error: {exc}"}

    with open("dash_data.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    print("wrote dash_data.json")
    if not args.no_embed:
        html = open("dash.html", encoding="utf-8").read()
        blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</script", "<\\/script")
        html, n = re.subn(r"(/\*DASH_DATA\*/)(.*?)(/\*END_DASH_DATA\*/)", lambda m: m.group(1) + blob + m.group(3), html, flags=re.S)
        if n != 1:
            raise SystemExit("dash.html: need exactly one DASH_DATA marker")
        with open("dash.html", "w", encoding="utf-8") as fh:
            fh.write(html)
        print("embedded into dash.html")


if __name__ == "__main__":
    main()
