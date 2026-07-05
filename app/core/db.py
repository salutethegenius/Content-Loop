import os
from datetime import datetime, timezone

import psycopg2


def get_conn():
    """Open a new Postgres connection. Caller is responsible for closing it."""
    return psycopg2.connect(os.environ["DATABASE_URL"])


def save_draft(brand_id, platform, draft_text):
    """Insert a pending_approval draft and return its new row id."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_items (brand, platform, draft_text, status)
                VALUES (%s, %s, %s, 'pending_approval')
                RETURNING id
                """,
                (brand_id, platform, draft_text),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
    finally:
        conn.close()


def update_status(item_id, status, slack_message_ts=None, posted_at=None):
    """Update an item's status and optionally its Slack ts / posted timestamp."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE content_items
                   SET status = %s,
                       slack_message_ts = COALESCE(%s, slack_message_ts),
                       approved_at = CASE WHEN %s = 'approved' THEN now() ELSE approved_at END,
                       posted_at = COALESCE(%s, posted_at)
                 WHERE id = %s
                """,
                (status, slack_message_ts, status, posted_at, item_id),
            )
            conn.commit()
    finally:
        conn.close()


def get_last_posted(brand_id):
    """Return the most recent posted_at for a brand, or None if never posted."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max(posted_at)
                  FROM content_items
                 WHERE brand = %s
                   AND status = 'posted'
                """,
                (brand_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()
