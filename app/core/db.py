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


# --- DB-backed brand configs (onboarding output) ---


def upsert_brand(brand_id, config, voice_md):
    """Persist a brand config + voice so the loop can use it without file IO."""
    import json

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brands (brand_id, config, voice_md, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (brand_id) DO UPDATE
                   SET config = EXCLUDED.config,
                       voice_md = EXCLUDED.voice_md,
                       updated_at = now()
                """,
                (brand_id, json.dumps(config), voice_md),
            )
            conn.commit()
    finally:
        conn.close()


def get_brand_from_db(brand_id):
    """Return (config_dict, voice_md) for a DB-backed brand, or None if absent."""
    import json

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config, voice_md FROM brands WHERE brand_id = %s",
                (brand_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return json.loads(row[0]), row[1]
    finally:
        conn.close()


def list_db_brands():
    """Return all DB-backed brand configs (active ones filtered by caller)."""
    import json

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT config FROM brands")
            return [json.loads(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()


# --- Onboarding sessions ---


def create_onboarding_session(brand_id, display_name, channel, thread_ts):
    """Insert a new onboarding session. Fails softly if one already exists."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO onboarding_sessions
                    (brand_id, display_name, channel, thread_ts, phase, status)
                VALUES (%s, %s, %s, %s, 'identity', 'in_progress')
                ON CONFLICT (brand_id) DO UPDATE
                   SET display_name = EXCLUDED.display_name,
                       channel = EXCLUDED.channel,
                       thread_ts = EXCLUDED.thread_ts,
                       phase = 'identity',
                       answers = '{}'::jsonb,
                       draft_voice_md = NULL,
                       draft_config = NULL,
                       status = 'in_progress',
                       updated_at = now()
                RETURNING id
                """,
                (brand_id, display_name, channel, thread_ts),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
    finally:
        conn.close()


def get_onboarding_session_by_thread(thread_ts):
    """Return the onboarding session for a Slack thread, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, brand_id, display_name, channel, thread_ts,
                       phase, answers, draft_voice_md, draft_config, status
                  FROM onboarding_sessions
                 WHERE thread_ts = %s
                """,
                (thread_ts,),
            )
            row = cur.fetchone()
            if not row:
                return None
            import json

            return {
                "id": row[0],
                "brand_id": row[1],
                "display_name": row[2],
                "channel": row[3],
                "thread_ts": row[4],
                "phase": row[5],
                "answers": row[6] if isinstance(row[6], dict) else json.loads(row[6] or "{}"),
                "draft_voice_md": row[7],
                "draft_config": row[8] if isinstance(row[8], dict) else (json.loads(row[8]) if row[8] else None),
                "status": row[9],
            }
    finally:
        conn.close()


def get_onboarding_session(brand_id):
    """Return the onboarding session for a brand, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, brand_id, display_name, channel, thread_ts,
                       phase, answers, draft_voice_md, draft_config, status
                  FROM onboarding_sessions
                 WHERE brand_id = %s
                """,
                (brand_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            import json

            return {
                "id": row[0],
                "brand_id": row[1],
                "display_name": row[2],
                "channel": row[3],
                "thread_ts": row[4],
                "phase": row[5],
                "answers": row[6] if isinstance(row[6], dict) else json.loads(row[6] or "{}"),
                "draft_voice_md": row[7],
                "draft_config": row[8] if isinstance(row[8], dict) else (json.loads(row[8]) if row[8] else None),
                "status": row[9],
            }
    finally:
        conn.close()


def append_onboarding_answer(brand_id, phase, message_text):
    """Append a human reply to the session's answers under the current phase."""
    import json

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE onboarding_sessions
                   SET answers = answers || jsonb_build_object(
                        %s,
                        COALESCE(answers->%s, '[]'::jsonb) || to_jsonb(%s::text)
                   ),
                       updated_at = now()
                 WHERE brand_id = %s
                """,
                (phase, phase, message_text, brand_id),
            )
            conn.commit()
    finally:
        conn.close()


def set_onboarding_phase(brand_id, phase):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE onboarding_sessions
                   SET phase = %s, updated_at = now()
                 WHERE brand_id = %s
                """,
                (phase, brand_id),
            )
            conn.commit()
    finally:
        conn.close()


def save_onboarding_draft(brand_id, voice_md, config):
    import json

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE onboarding_sessions
                   SET draft_voice_md = %s,
                       draft_config = %s,
                       phase = 'awaiting_approval',
                       updated_at = now()
                 WHERE brand_id = %s
                """,
                (voice_md, json.dumps(config), brand_id),
            )
            conn.commit()
    finally:
        conn.close()


def set_onboarding_status(brand_id, status):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE onboarding_sessions
                   SET status = %s, updated_at = now()
                 WHERE brand_id = %s
                """,
                (status, brand_id),
            )
            conn.commit()
    finally:
        conn.close()
