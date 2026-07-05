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
