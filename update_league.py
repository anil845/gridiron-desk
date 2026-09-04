"""Tier-B bridge: turn a pasted Yahoo transaction log into league-activity data,
so opponent moves / FAB spending are tracked even before the Yahoo API is
approved. Tolerant parser (same spirit as the draft board's bulk paste).

    python update_league.py --league papi-chulo --paste txn.txt
    (or pipe: Get-Clipboard | python update_league.py --league papi-chulo)

Yahoo's transaction log copies as blocks like:
    Wed Sep 10  Added  Player A (Buf - RB)  $12
                Dropped  Player B (Was - WR)
    Cerveza Loco
Appends to seasons/<slug>_transactions.json (deduped). Once Yahoo access is
approved, yahoo_sync.py writes the same file automatically and this is retired.
"""

import argparse
import json
import os
import re
import sys

import build_board as bb

ADD = re.compile(r"\bAdded\b\s+(.+?)\s*\(([A-Za-z]{2,3})\s*-\s*([A-Za-z/]+)\)", re.I)
DROP = re.compile(r"\bDropped\b\s+(.+?)\s*\(([A-Za-z]{2,3})\s*-\s*([A-Za-z/]+)\)", re.I)
FAB = re.compile(r"\$(\d+)")
DATE = re.compile(r"\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*[A-Z][a-z]{2}\s+\d{1,2}(?:,\s*\d{4})?)")


def parse(text, teams):
    """Split into per-transaction blocks (a team name ends each block) and pull
    adds/drops/FAB/date. Returns [{date, team, adds, drops, fab, targeted}]."""
    tnames = {bb.norm_name(t): t for t in teams}
    rows = []
    cur = {"adds": [], "drops": [], "fab": None, "date": None, "team": None}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        d = DATE.search(line)
        if d and not cur["adds"] and not cur["drops"]:
            cur["date"] = d.group(1)
        for m in ADD.finditer(line):
            cur["adds"].append({"name": m.group(1).strip(), "team": m.group(2).upper(), "pos": m.group(3).upper()})
        for m in DROP.finditer(line):
            cur["drops"].append({"name": m.group(1).strip(), "team": m.group(2).upper(), "pos": m.group(3).upper()})
        f = FAB.search(line)
        if f:
            cur["fab"] = int(f.group(1))
        tn = tnames.get(bb.norm_name(line))
        if tn and (cur["adds"] or cur["drops"]):
            cur["team"] = tn
            cur["targeted"] = [a["name"] for a in cur["adds"]]
            cur["type"] = "trade" if len(cur["adds"]) > 1 and not cur["fab"] else ("waiver" if cur["fab"] else "add/drop")
            rows.append(cur)
            cur = {"adds": [], "drops": [], "fab": None, "date": None, "team": None}
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--paste", help="file with the copied transaction log (else stdin)")
    args = ap.parse_args()
    cfg = bb.load_league(args.league)
    teams = cfg.get("team_names") or []
    text = open(args.paste, encoding="utf-8").read() if args.paste else sys.stdin.read()
    rows = parse(text, teams)

    path = os.path.join("seasons", f"{args.league}_transactions.json")
    existing = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"txns": []}
    seen = {json.dumps([r.get("date"), r.get("team"), sorted(a["name"] for a in r["adds"])], sort_keys=True)
            for r in existing["txns"]}
    added = 0
    for r in rows:
        key = json.dumps([r.get("date"), r.get("team"), sorted(a["name"] for a in r["adds"])], sort_keys=True)
        if key not in seen:
            existing["txns"].append(r)
            seen.add(key)
            added += 1
    json.dump(existing, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"parsed {len(rows)} transactions from paste, {added} new -> {path}")
    for r in rows[:10]:
        adds = ", ".join(a["name"] for a in r["adds"]) or "-"
        drops = ", ".join(d["name"] for d in r["drops"]) or "-"
        print(f"  {r.get('team') or '?':22s} +{adds}  -{drops}  {('$'+str(r['fab'])) if r['fab'] else ''}")


if __name__ == "__main__":
    main()
