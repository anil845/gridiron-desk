"""Edge finder: where the podcast crowd-of-experts and our algo disagree.

    python edge_finder.py --league the-league

Joins podcast consensus (seasons/podcast/consensus.json) to our board values
and surfaces the disagreements — the only place an edge can live:

  PODS LOVE, WE'RE COLD   experts buying hard, our value low  -> reach earlier
                          than our board says, OR crowd hype to fade
  WE LOVE, PODS COLD/SELL  our value high, experts selling     -> our contrarian
                          edge, OR a model blind spot to double-check
  ALIGNED                 both agree                           -> confidence, no edge

Weights podcast conviction by BOTH stance strength and number of independent
shows (3 shows saying buy >> 1 host). Prints draft-actionable edges for the
league whose board is loaded.
"""

import argparse
import json
import os

import build_board as bb

CONSENSUS = os.path.join("seasons", "podcast", "consensus.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="the-league")
    ap.add_argument("--min-shows", type=int, default=1, help="only trust pods with >= this many shows")
    args = ap.parse_args()

    if not os.path.exists(CONSENSUS):
        raise SystemExit("No podcast consensus yet — run podcast_ingest.py first.")
    pc = json.load(open(CONSENSUS, encoding="utf-8"))
    board = json.load(open(f"board_data_{args.league}.json", encoding="utf-8"))["players"]
    by_norm = {bb.norm_name(p["name"]): p for p in board}
    skill = [p for p in board if p["position"] in bb.SKILL]
    # our value percentile within the whole skill pool (0 = replacement, 1 = best)
    ranked = sorted(skill, key=lambda p: -p["value"])
    our_rank = {p["player_id"]: i + 1 for i, p in enumerate(ranked)}
    n = len(ranked)

    print(f"=== Edge finder · {args.league} · {pc.get('stances')} stances from "
          f"{pc.get('episodes_files')} episodes ===\n")

    rows = []
    for pl in pc.get("players", []):
        if pl["n_shows"] < args.min_shows:
            continue
        bp = by_norm.get(pl["norm"])
        if not bp or bp["position"] not in bb.SKILL:
            continue
        # podcast conviction: net score scaled by show-count (independent corroboration)
        conv = pl["score"] * (1 + 0.5 * (pl["n_shows"] - 1))
        orank = our_rank.get(bp["player_id"], n)
        our_pct = 1 - (orank - 1) / max(1, n - 1)          # 1=elite, 0=deep
        # pod enthusiasm as a percentile too (rough: map conviction)
        rows.append({"name": bp["name"], "pos": bp["position"], "value": bp["value"],
                     "orank": orank, "our_pct": our_pct, "lean": pl["lean"],
                     "conv": conv, "n": pl["n_shows"],
                     "wf": bp.get("wf_value"), "espn": bp.get("espn_aav"),
                     "quote": pl["stances"][0].get("quote", "") if pl["stances"] else ""})

    # PODS LOVE, WE'RE COLD: strong positive conviction but our value ranks them low
    love_cold = sorted([r for r in rows if r["conv"] >= 2 and r["orank"] > 40],
                       key=lambda r: (r["orank"], -r["conv"]))
    # WE LOVE, PODS COLD/SELL: we rank them high but pods lean sell/mixed or weak
    love_hot = sorted([r for r in rows if r["orank"] <= 60 and (r["lean"] == "sell" or r["conv"] <= -1)],
                      key=lambda r: (r["orank"], r["conv"]))
    # ALIGNED buys (confidence picks): pods buy + we rank high
    aligned = sorted([r for r in rows if r["conv"] >= 3 and r["orank"] <= 40],
                     key=lambda r: r["orank"])

    def show(title, rs, note):
        print(f"── {title}  ({note})")
        if not rs:
            print("   (none)")
        for r in rs[:10]:
            mk = f"wf${r['wf']}" if r["wf"] else ""
            mk += f" espn${r['espn']}" if r["espn"] else ""
            print(f"   {r['name']:22s} {r['pos']:3s}  ourRank #{r['orank']:<3d} ${r['value']:<3d}  "
                  f"pods {r['lean']}×{r['n']} (conv {r['conv']:+.0f})  {mk}")
            if r["quote"]:
                print(f"       “{r['quote'][:80]}”")
        print()

    show("PODS LOVE, WE'RE COLD", love_cold, "experts buy, our board ranks low — reach earlier, or crowd hype to fade")
    show("WE LOVE, PODS COLD/SELL", love_hot, "our value high, experts cold — our contrarian edge, or a blind spot")
    show("ALIGNED HIGH-CONVICTION BUYS", aligned, "both agree — draft with confidence")


if __name__ == "__main__":
    main()
