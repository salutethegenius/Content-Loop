-- Content Loop Agent V1 schema
-- Run on Railway Postgres:
--   psql "$DATABASE_URL" -f schema.sql

CREATE TABLE IF NOT EXISTS content_items (
    id SERIAL PRIMARY KEY,
    brand TEXT NOT NULL,
    platform TEXT NOT NULL,
    draft_text TEXT,
    image_url TEXT,
    status TEXT DEFAULT 'pending_approval',
    slack_message_ts TEXT,
    scheduled_for TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    approved_at TIMESTAMP,
    posted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS content_items_brand_status_idx
    ON content_items (brand, status);

-- DB-backed brand configs and voice files.
-- brand_loader reads from here first, falls back to app/brands/{brand}/ on disk.
-- Onboarding-approved brands land here so they are immediately live
-- without a filesystem write or a git commit.
CREATE TABLE IF NOT EXISTS brands (
    brand_id TEXT PRIMARY KEY,
    config JSONB NOT NULL,
    voice_md TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

-- Ongoing Slack onboarding conversations.
-- One row per brand being onboarded. State survives restarts.
CREATE TABLE IF NOT EXISTS onboarding_sessions (
    id SERIAL PRIMARY KEY,
    brand_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'identity',
    answers JSONB DEFAULT '{}'::jsonb,
    draft_voice_md TEXT,
    draft_config JSONB,
    status TEXT DEFAULT 'in_progress',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS onboarding_sessions_thread_ts_idx
    ON onboarding_sessions (thread_ts);
