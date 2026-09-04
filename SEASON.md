# In-Season System — Design Map

Goal: waivers, add/drop, start/sit, matchup-tailored advice, K/DEF streaming,
podcast-derived signal — with **minimal noise and high automation**. Same
architecture that won the draft: build script → static HTML per league, all
signals joined onto players, judgment stays visible and separate from data.

Two leagues, opposite modes (encoded in config, never in code):
- **papi-chulo** (3 bench): waivers are triage — immediate starters only, churn
  hard, no stashing. Trades matter because the wire is thin.
- **the-league** (5 bench): waivers are opportunity — stashes, handcuffs,
  speculative adds are allowed plays.

## The weekly artifact

```
python build_week.py --league the-league --week 3
  → week_the-league_w3.html        (also deployed to the war room: /week/<slug>)
```

One glanceable page, board-style:
1. **My lineup vs opponent** — both rosters, weekly points through league
   scoring, start/sit calls with close-call flags (<2pt = judgment, not math),
   win probability, and the variance directive: favorite → floors,
   underdog → ceilings + correlated stacks.
2. **Waiver board** — FA pool (relevant players − all rostered), ranked by
   improvement over MY weakest starter (papi) or roster value added (league),
   with a suggested FAB bid derived from the gap + my remaining budget +
   competition (other teams' needs from their rosters).
3. **K/DEF stream card** — this week + next week: opponent offense strength,
   opposing QB sack/INT proneness, dome, miss-penalty scoring. Auto-flags
   "your DEF has a bottom-5 matchup → stream X".
4. **Usage trends** — snap/target/carry share deltas from nflverse actuals;
   usage spikes surface waiver targets a week before points do.
5. **🎙 Podcast consensus** — see below. Badges on player rows, like the
   draft board's source model: agreement is boring, divergence is the signal.

## Data layer (automated, no LLM)

| What | Source | Cadence | Where it runs |
|---|---|---|---|
| Weekly + ROS projections | Sleeper `/projections/nfl/2026/<wk>` (verified: per-week, opponent included) | nightly | GH Actions → commit to repo |
| Injuries, trending | Sleeper | nightly + Sunday sweep | GH Actions |
| News | ESPN public API | nightly | GH Actions |
| Actuals / usage | nflverse weekly | Tue night | GH Actions |
| League rosters/standings | **manual paste** (transaction log Tue, standings Mon, roster check Sun — ~5 min/league/wk) | weekly | `update_league.py --paste` (tolerant parser, parseBulk-style) |

League state lives in `seasons/` as dated JSON committed to git — diffable,
backfill-proof, no database. Yahoo OAuth replaces the manual paste if/when
API access is approved; the paste is the load-bearing fallback either way.

GH Actions cron lesson (learned previously): schedule on odd minutes with a
frequent single cadence and gate on time *inside* the script — clean :00/:15
ticks get silently skipped.

## Podcast synthesizer

No listening, no audio transcription. Big fantasy shows post full episodes on
YouTube → `yt-dlp --write-auto-sub` pulls the captions as text for free.
(whisper.cpp + ffmpeg is the fallback for audio-only feeds; ffmpeg verified
installed.)

Pipeline (nightly, automated):
1. Fetch new episodes from the chosen shows' channels → captions → clean text.
2. `claude -p` extraction against a strict schema:
   `{player, stance: buy/sell/start/sit/add/drop/hold, strength, quote, show, episode_date}`
3. Aggregate stances per player across shows → **podcast consensus** stored in
   `seasons/podcast/*.json`, joined to players by name like every other source.

Surfaced as: 🎙 badge + stance on week.html rows (hover = actual quotes), and a
"pods vs model" disagreement list — same P/M philosophy as the draft board:
when the pods and the projections disagree, that's exactly where to look.

## Alerts (phone via ntfy, minimal-noise rules)

1. **Sunday inactive sweep** (the one that justifies everything): every 15 min
   in the 11:00am–1:05pm window (in-script time gate), reads MY rosters, pulls
   inactives; if a starter is OUT → push "Papi: X is OUT — best bench: Y".
2. **Tuesday waiver nudge**: claim-deadline reminder + top 3 targets + FAB
   bids. Per-league claim windows encoded (League: 1 day; **Papi: verify —
   never sourced from settings**).
3. **Mid-week news** ONLY if it touches my roster or a top-5 waiver target.
   Nothing else pushes. Silence is the default.

## What runs where

- **No-LLM automation** (GH Actions): all data sync, caption fetch, alert sweeps.
- **LLM on subscription** (`claude -p`, local or /schedule): nightly podcast
  extraction; Tuesday-morning digest (build week.html for both leagues + one
  summary push).
- **Human, ~10 min/week total**: Tuesday transaction-log paste ×2, Sunday
  lineup taps in the Yahoo app.

## Build order

1. **Now → Wk1 (by Sep 10)**: `update_league.py` state capture + snapshots;
   K/DEF streamer (the manual Steelers/Bates analysis, automated);
   `build_week.py` core — lineup, start/sit, matchup, win prob.
2. **Wk 2**: waiver ranker + FAB bids; war-room `/week/` hosting; Tuesday digest.
3. **Wk 3**: Sunday inactive alert (ntfy) + usage-trend detection.
4. **Wk 4**: podcast pipeline end to end.
5. **Wk 5+**: opponent tendencies, trade finder — only once snapshots have
   accumulated enough to mean something.

Expectation check (kept from the original plan, still true): the pieces that
survive a real season are the Sunday alert, waiver-vs-my-weakest-starter, and
seeing the opponent's roster next to mine. Everything else earns its place or
gets cut.
