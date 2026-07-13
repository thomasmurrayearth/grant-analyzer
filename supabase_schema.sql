-- Grant Analyser — Supabase schema.
--
-- Safe to run more than once: every statement is IF NOT EXISTS / ADD COLUMN
-- IF NOT EXISTS, so re-running it never drops data.
--
-- HOW TO RUN: Supabase dashboard → SQL Editor → New query → paste → Run.
--
-- The app degrades gracefully if this has not been run: writes that use the
-- new columns are retried without them (see db.py), and the new tables simply
-- log a warning. But the funnel metrics and the pricing gates in the launch
-- plan need it, so run it before the launch push.

-- ── Analyses: one row per run (the original table) ────────────────────────
CREATE TABLE IF NOT EXISTS analyses (
  id               BIGSERIAL PRIMARY KEY,
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  completed_at     TIMESTAMPTZ,
  job_id           TEXT NOT NULL,
  company_url      TEXT,
  input_snippet    TEXT,
  company_name     TEXT,
  geographies      TEXT,
  consortium       BOOLEAN,
  accelerators     BOOLEAN,
  prizes           BOOLEAN,
  status           TEXT DEFAULT 'started',
  grants_found     INTEGER,
  profile_json     JSONB,
  results_json     JSONB,
  error_message    TEXT,
  ip_address       TEXT,
  user_email       TEXT
);

-- July 2026: newsletter consent + per-run API cost (needed for the pricing gates).
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS newsletter_opt_in  BOOLEAN DEFAULT FALSE;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS cost_usd           NUMERIC(10, 4);
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS api_calls          INTEGER;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS input_tokens       INTEGER;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS output_tokens      INTEGER;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS cache_read_tokens  INTEGER;
ALTER TABLE analyses ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER;

CREATE INDEX IF NOT EXISTS analyses_job_id_idx     ON analyses (job_id);
CREATE INDEX IF NOT EXISTS analyses_created_at_idx ON analyses (created_at DESC);
CREATE INDEX IF NOT EXISTS analyses_ip_created_idx ON analyses (ip_address, created_at DESC);

-- ── Events: the funnel (landing views, CTA clicks, downloads, shares) ─────
-- `event` is free text so a new funnel step can be measured without a migration.
CREATE TABLE IF NOT EXISTS events (
  id         BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  event      TEXT NOT NULL,
  job_id     TEXT,
  detail     TEXT,
  ip_address TEXT
);

CREATE INDEX IF NOT EXISTS events_created_at_idx ON events (created_at DESC);
CREATE INDEX IF NOT EXISTS events_event_idx      ON events (event);

-- ── Feedback: "how useful was this shortlist?" after results render ───────
CREATE TABLE IF NOT EXISTS feedback (
  id          BIGSERIAL PRIMARY KEY,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  job_id      TEXT,
  rating      INTEGER,
  comment     TEXT,
  may_contact BOOLEAN DEFAULT FALSE,
  user_email  TEXT
);

CREATE INDEX IF NOT EXISTS feedback_created_at_idx ON feedback (created_at DESC);

-- ── Waitlist: emails captured when a run was turned away at capacity ──────
CREATE TABLE IF NOT EXISTS waitlist (
  id         BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  user_email TEXT NOT NULL,
  reason     TEXT,
  ip_address TEXT
);
