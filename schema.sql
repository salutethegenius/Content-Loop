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

-- V2: Meta Graph API publishing
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS meta_post_id TEXT;
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS published_via TEXT DEFAULT 'meta_graph';

-- V1.6: Image generation (Gemini Nano Banana 2 Lite). image_url already exists
-- from the original V1 schema; these track provenance for debugging/regen.
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS image_prompt TEXT;
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS image_model TEXT;
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS image_generated_at TIMESTAMPTZ;

-- V2.1: Stuck-claim recovery. claim_item_status stamps claimed_at when an
-- item enters publishing/scheduling; the cron sweep parks stale claims as
-- needs_review (no auto-retry — the Meta call may have succeeded).
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP;
