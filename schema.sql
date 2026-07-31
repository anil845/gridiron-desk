-- Gridiron Desk — Postgres schema
--
-- Three tables:
--   players  — the ID crosswalk, keyed on Sleeper's player_id. Sleeper carries
--              the mapping to other platforms' IDs (gsis, espn, yahoo, ...), so
--              it is the natural hub for joining data from different sources.
--   stats    — weekly player box-score stats from nflverse. Keyed on nflverse's
--              player_id, which equals Sleeper's gsis_id, so:
--                  players.gsis_id = stats.player_id
--              is the join between roster/crosswalk data and performance data.
--   signals  — normalized expert opinions for later ingestion (FantasyPros et al).
--
-- Run once against an empty database:
--   psql "$DATABASE_URL" -f schema.sql

CREATE TABLE IF NOT EXISTS players (
    player_id       text PRIMARY KEY,          -- Sleeper player_id (canonical)
    full_name       text,
    first_name      text,
    last_name       text,
    position        text,
    team            text,
    status          text,
    age             integer,
    years_exp       integer,
    search_rank     integer,                    -- Sleeper's own popularity rank
    -- crosswalk to other platforms' identifiers
    gsis_id         text,                        -- nflverse / NFL GSIS id
    espn_id         text,
    yahoo_id        text,
    sportradar_id   text,
    rotowire_id     text,
    fantasy_data_id text,
    source          text NOT NULL DEFAULT 'sleeper',
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS players_gsis_id_idx    ON players (gsis_id);
CREATE INDEX IF NOT EXISTS players_yahoo_id_idx   ON players (yahoo_id);
CREATE INDEX IF NOT EXISTS players_position_idx   ON players (position);
CREATE INDEX IF NOT EXISTS players_team_idx       ON players (team);


CREATE TABLE IF NOT EXISTS stats (
    player_id              text NOT NULL,        -- nflverse id (= players.gsis_id)
    season                 integer NOT NULL,
    week                   integer NOT NULL,
    season_type            text NOT NULL DEFAULT 'REG',
    player_name            text,
    position               text,
    team                   text,
    opponent               text,
    -- passing
    completions            integer,
    attempts               integer,
    passing_yards          numeric,
    passing_tds            integer,
    passing_interceptions  integer,
    passing_2pt            integer,
    -- rushing
    carries                integer,
    rushing_yards          numeric,
    rushing_tds            integer,
    rushing_2pt            integer,
    -- receiving
    receptions             integer,
    targets                integer,
    receiving_yards        numeric,
    receiving_tds          integer,
    receiving_2pt          integer,
    -- ball security (components kept separate so custom scoring can weight them)
    sack_fumbles_lost      integer,
    rushing_fumbles_lost   integer,
    receiving_fumbles_lost integer,
    -- nflverse's own reference calculations (not this project's league scoring)
    fantasy_points         numeric,
    fantasy_points_ppr     numeric,
    source                 text NOT NULL DEFAULT 'nflverse',
    captured_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, season, week, season_type)
);

CREATE INDEX IF NOT EXISTS stats_season_week_idx ON stats (season, week);
CREATE INDEX IF NOT EXISTS stats_player_idx      ON stats (player_id);


-- Expert-opinion signals. Nothing writes here yet; the shape is fixed now so
-- FantasyPros (and, if approved, Yahoo) ingestion can normalize into it later.
-- One row = one directional claim about one player from one source at one time.
CREATE TABLE IF NOT EXISTS signals (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id   text NOT NULL REFERENCES players (player_id),
    source      text NOT NULL,                   -- e.g. 'fantasypros'
    captured_at timestamptz NOT NULL,            -- when the claim was observed
    claim_type  text NOT NULL,                   -- e.g. 'ros_rank', 'buy_low', 'start_sit'
    direction   text NOT NULL,                   -- 'up' | 'down' | 'neutral'
    confidence  numeric,                          -- 0..1, source-normalized
    horizon     text,                             -- e.g. 'week', 'ros', 'dynasty'
    evidence    text,                             -- free-text note / link / rationale
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT signals_direction_chk  CHECK (direction IN ('up', 'down', 'neutral')),
    CONSTRAINT signals_confidence_chk CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX IF NOT EXISTS signals_player_idx  ON signals (player_id);
CREATE INDEX IF NOT EXISTS signals_capture_idx ON signals (captured_at);
