"""Podcast synthesizer: YouTube captions -> LLM stance extraction -> per-player
consensus. No listening, no audio transcription.

    python podcast_ingest.py                # fetch recent episodes for all shows, extract, aggregate
    python podcast_ingest.py --extract FILE # extract stances from one .vtt/.txt (test)
    python podcast_ingest.py --shows N      # episodes per show (default 1)

Writes seasons/podcast/consensus.json (per-player aggregated stances) and
seasons/podcast/raw/<date>_<show>.json (per-episode extractions). Joined to
players by name on the dashboard, like every other signal.
"""

import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

FEEDS = os.path.join("seasons", "podcasts.json")
OUT = os.path.join("seasons", "podcast")
STANCES = ("buy", "sell", "start", "sit", "add", "drop", "hold")
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")

SCHEMA_PROMPT = """You are extracting fantasy-football player opinions from a podcast transcript.
Output ONLY a JSON array, no prose, no markdown fences. One object per DISTINCT
player-stance the hosts express. Skip chit-chat, ads, and generic talk.
Each object:
  {"player": "<full name>", "stance": "<buy|sell|start|sit|add|drop|hold>",
   "strength": <1-3 how emphatic>, "quote": "<<=15-word supporting phrase>"}
Only include players named specifically with an actionable opinion. If none, output [].

TRANSCRIPT:
"""


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[.'’\-]", "", s)
    s = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_vtt(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    body = re.sub(r"^WEBVTT.*?\n\n", "", s, flags=re.S)
    lines = [re.sub(r"<[^>]+>", "", ln).strip() for ln in body.splitlines()
             if ln.strip() and "-->" not in ln
             and not ln.startswith(("Kind:", "Language:", "align:", "NOTE"))]
    out = []
    for ln in lines:
        if not out or out[-1] != ln:
            out.append(ln)
    return " ".join(out)


def extract_stances(text, show, date):
    """claude -p with a strict schema. Truncate very long transcripts; the
    fantasy content is dense enough that 40k chars captures the opinions."""
    prompt = SCHEMA_PROMPT + text[:40000]
    try:
        r = subprocess.run(["claude", "-p"], input=prompt, capture_output=True,
                           text=True, timeout=300, encoding="utf-8")
        out = r.stdout.strip()
        m = re.search(r"\[.*\]", out, re.S)   # tolerate any stray wrapper text
        rows = json.loads(m.group(0)) if m else []
    except Exception as exc:  # noqa: BLE001
        print(f"  extraction failed ({exc})", file=sys.stderr)
        return []
    clean = []
    for x in rows:
        st = str(x.get("stance", "")).lower()
        if x.get("player") and st in STANCES:
            clean.append({"player": x["player"], "norm": norm(x["player"]), "stance": st,
                          "strength": int(x.get("strength", 1) or 1),
                          "quote": (x.get("quote") or "")[:120], "show": show, "date": date})
    return clean


def fetch_captions(query, n, force):
    """yt-dlp: N recent episodes matching a channel query -> list of
    (title, date, vid, vtt_path). Only fetches captions for episodes not
    already cached; --dateafter limits to the backfill window."""
    os.makedirs(os.path.join(OUT, "vtt"), exist_ok=True)
    tmpl = os.path.join(OUT, "vtt", "%(upload_date)s_%(id)s.%(ext)s")
    cutoff = (datetime.date.today() - datetime.timedelta(days=BACKFILL_DAYS)).strftime("%Y%m%d")
    cmd = ["python", "-m", "yt_dlp", f"ytsearch{n}:{query}",
           "--skip-download", "--write-auto-subs", "--sub-langs", "en-en,en",
           "--dateafter", cutoff, "--no-warnings", "--ignore-errors",
           "--match-filter", "duration > 900",   # real episodes, not shorts/clips
           "-o", tmpl, "--print", "%(upload_date)s|%(id)s|%(title)s"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"  yt-dlp failed ({exc})", file=sys.stderr)
        return []
    eps = []
    for line in (r.stdout or "").splitlines():
        if "|" in line:
            date, vid, title = (line.split("|", 2) + ["", "", ""])[:3]
            vtt = sorted(glob.glob(os.path.join(OUT, "vtt", f"{date}_{vid}.*.vtt")))
            if vtt:
                eps.append((title, date, vid, vtt[0]))
    return eps


def aggregate_from_disk():
    """Rebuild consensus.json from every raw/*.json on disk (accumulates
    across backfill runs)."""
    rows = []
    for f in glob.glob(os.path.join(OUT, "raw", "*.json")):
        try:
            rows.extend(json.load(open(f, encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    cons = aggregate(rows)
    with open(os.path.join(OUT, "consensus.json"), "w", encoding="utf-8") as fh:
        json.dump({"built": _now(), "episodes_files": len(glob.glob(os.path.join(OUT, "raw", "*.json"))),
                   "stances": len(rows), "players": cons}, fh, indent=1, ensure_ascii=False)
    return rows, cons


def aggregate(all_rows):
    by = {}
    for r in all_rows:
        p = by.setdefault(r["norm"], {"player": r["player"], "stances": [], "shows": set()})
        p["stances"].append({"stance": r["stance"], "strength": r["strength"],
                             "show": r["show"], "date": r["date"], "quote": r["quote"]})
        p["shows"].add(r["show"])
    out = []
    for norm_name, p in by.items():
        pos = sum(s["strength"] for s in p["stances"] if s["stance"] in ("buy", "start", "add", "hold"))
        neg = sum(s["strength"] for s in p["stances"] if s["stance"] in ("sell", "sit", "drop"))
        net = pos - neg
        out.append({"player": p["player"], "norm": norm_name,
                    "lean": "buy" if net > 0 else "sell" if net < 0 else "mixed",
                    "score": net, "n_shows": len(p["shows"]),
                    "stances": sorted(p["stances"], key=lambda s: -s["strength"])})
    out.sort(key=lambda p: -abs(p["score"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", help="test: extract stances from one vtt/txt file")
    ap.add_argument("--shows", type=int, default=1, help="episodes per show")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(os.path.join(OUT, "raw"), exist_ok=True)

    if args.extract:
        text = clean_vtt(args.extract) if args.extract.endswith(".vtt") else open(args.extract, encoding="utf-8").read()
        print(f"transcript: {len(text.split())} words -> extracting...", file=sys.stderr)
        rows = extract_stances(text, "test", datetime.date.today().isoformat())
        print(json.dumps(rows, indent=1))
        print(f"\n{len(rows)} stances extracted", file=sys.stderr)
        return

    feeds = json.load(open(FEEDS, encoding="utf-8"))
    new_eps = 0
    for show in feeds["shows"]:
        print(f"[{show['name']}] fetching up to {args.shows} from last {BACKFILL_DAYS}d...", file=sys.stderr, flush=True)
        for title, date, vid, vtt in fetch_captions(show["channel_query"], args.shows, args.force):
            rawf = os.path.join(OUT, "raw", f"{date}_{vid}.json")
            if os.path.exists(rawf) and not args.force:
                continue   # already extracted this episode
            text = clean_vtt(vtt)
            if len(text.split()) < 500:   # skip clips/empty caption tracks
                continue
            rows = extract_stances(text, show["name"], date or datetime.date.today().isoformat())
            with open(rawf, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, indent=1)
            new_eps += 1
            print(f"  [{date}] {title[:55]}: {len(rows)} stances", file=sys.stderr, flush=True)
    all_rows, consensus = aggregate_from_disk()
    print(f"\n+{new_eps} new episodes · {len(all_rows)} total stances -> {len(consensus)} players", file=sys.stderr)
    for p in consensus[:15]:
        print(f"  {p['lean']:5s} {p['score']:>+3d} ({p['n_shows']}sh) {p['player']}", file=sys.stderr)


if __name__ == "__main__":
    main()
