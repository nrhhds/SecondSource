-- Second Source - Postgres schema for Supabase.
--
-- Ported from the SQLite DDL in ingest.py (articles, fetch_log) and render.py
-- (scores, receipts, withdrawals, clusters, cluster_members).
--
-- Run once against the project, e.g. in the Supabase SQL editor, or:
--   psql "$DATABASE_URL" -f schema.sql
-- Idempotent: safe to re-run.
--
-- ---------------------------------------------------------------------------
-- Why a dedicated schema instead of public
-- ---------------------------------------------------------------------------
-- Supabase auto-exposes the tables in its configured "Exposed schemas" over
-- PostgREST. This table set holds scraped article body text, which design rule
-- 4 says is stored for analysis and never served. A local SQLite file was
-- unreachable by construction; a hosted database is unreachable only by
-- correct configuration, so this file sets up two independent layers:
--
--   1. This schema is NOT in Supabase's Exposed schemas list, so PostgREST has
--      no route to it at all. Do not add 'secondsource' to that setting.
--   2. RLS is enabled on every table with no policies defined, which denies
--      anon and authenticated even if layer 1 is ever misconfigured.
--
-- Layer 2 alone is not enough: the service_role key bypasses RLS entirely, so
-- schema isolation is what survives a leaked service key. Layer 1 alone is not
-- enough either, because it is one dropdown away from being undone. Both.
--
-- Neither layer affects the ingest writer, which connects over the Postgres
-- wire protocol as the project's database role, not through PostgREST.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS secondsource;

-- ---------------------------------------------------------------------------
-- Articles
-- ---------------------------------------------------------------------------
-- Timestamps are TIMESTAMPTZ, not the TEXT they were in SQLite. SQLite had no
-- date type so ISO strings were the only option; here they let health.py do
-- interval arithmetic in the database instead of comparing strings.
--
-- fulltext_ok and scored stay INTEGER rather than becoming BOOLEAN: the Python
-- writes and compares 0/1 in several places, and a type change would be a
-- behaviour change smuggled into a storage migration. Worth revisiting later,
-- deliberately.
CREATE TABLE IF NOT EXISTS secondsource.articles (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    published_at    TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ NOT NULL,
    raw_text        TEXT,
    word_count      INTEGER,
    fulltext_ok     INTEGER NOT NULL DEFAULT 0,
    -- Fingerprint of raw_text. Two articles from one outlet never share a body
    -- verbatim; when they do, the extractor has locked onto page furniture such
    -- as a paywall or consent wall. ingest.accept_body() rejects the repeat and
    -- demotes the first copy, which drops the source's extraction rate into the
    -- alarm health.py already raises. A demoted row KEEPS its body_sha - the
    -- hash is the only record that the fingerprint is known bad.
    body_sha        TEXT,
    scored          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_source  ON secondsource.articles(source_id);
CREATE INDEX IF NOT EXISTS idx_pubdate ON secondsource.articles(published_at);
CREATE INDEX IF NOT EXISTS idx_scored  ON secondsource.articles(scored);
CREATE INDEX IF NOT EXISTS idx_body_sha ON secondsource.articles(source_id, body_sha);

CREATE TABLE IF NOT EXISTS secondsource.fetch_log (
    run_at      TIMESTAMPTZ NOT NULL,
    source_id   TEXT NOT NULL,
    entries     INTEGER,
    new_rows    INTEGER,
    error       TEXT
);

-- ---------------------------------------------------------------------------
-- Scores and receipts
-- ---------------------------------------------------------------------------
-- DOUBLE PRECISION, not REAL. SQLite's REAL is an 8-byte IEEE float; Postgres
-- REAL is 4-byte. Rule 7 requires scores be regenerable from a rubric version
-- and a URL, so silently narrowing float width would make old scores fail to
-- reproduce.
CREATE TABLE IF NOT EXISTS secondsource.scores (
    article_id      TEXT NOT NULL,
    rubric_version  TEXT NOT NULL,
    signal          TEXT NOT NULL,
    value           DOUBLE PRECISION,
    value_text      TEXT,
    judged          INTEGER NOT NULL DEFAULT 0,
    confidence      DOUBLE PRECISION,
    spread          DOUBLE PRECISION,
    scored_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (article_id, rubric_version, signal)
);
CREATE INDEX IF NOT EXISTS idx_scores_article ON secondsource.scores(article_id);

CREATE TABLE IF NOT EXISTS secondsource.receipts (
    article_id      TEXT NOT NULL,
    rubric_version  TEXT NOT NULL,
    signal          TEXT NOT NULL,
    paragraph       INTEGER,
    quote           TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts ON secondsource.receipts(article_id, signal);

CREATE TABLE IF NOT EXISTS secondsource.withdrawals (
    article_id      TEXT NOT NULL,
    code            TEXT NOT NULL
        CHECK (code IN ('EXTRACTION_ERROR','WRONG_CLUSTER','DUPLICATE','LEGAL')),
    withdrawn_at    TIMESTAMPTZ NOT NULL,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS secondsource.clusters (
    id          TEXT PRIMARY KEY,
    label       TEXT,
    bill_id     TEXT,
    created_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS secondsource.cluster_members (
    cluster_id  TEXT NOT NULL,
    article_id  TEXT NOT NULL,
    PRIMARY KEY (cluster_id, article_id)
);

-- No foreign keys, matching the SQLite original. Adding them here would be a
-- behaviour change during a storage migration: cluster_members and receipts
-- could start rejecting rows the old store accepted.

-- ---------------------------------------------------------------------------
-- Layer 2: RLS on, no policies -> deny by default for anon and authenticated
-- ---------------------------------------------------------------------------
ALTER TABLE secondsource.articles        ENABLE ROW LEVEL SECURITY;
ALTER TABLE secondsource.fetch_log       ENABLE ROW LEVEL SECURITY;
ALTER TABLE secondsource.scores          ENABLE ROW LEVEL SECURITY;
ALTER TABLE secondsource.receipts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE secondsource.withdrawals     ENABLE ROW LEVEL SECURITY;
ALTER TABLE secondsource.clusters        ENABLE ROW LEVEL SECURITY;
ALTER TABLE secondsource.cluster_members ENABLE ROW LEVEL SECURITY;

-- Belt and braces: strip the API roles' grants outright, including on tables
-- created later, so a future CREATE TABLE in this schema does not quietly
-- arrive reachable.
REVOKE ALL ON SCHEMA secondsource FROM anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA secondsource FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA secondsource REVOKE ALL ON TABLES FROM anon, authenticated;
