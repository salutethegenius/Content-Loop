import os

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


def update_status(
    item_id,
    status,
    slack_message_ts=None,
    posted_at=None,
    meta_post_id=None,
    scheduled_for=None,
):
    """Update an item's status and optionally its Slack ts / posted timestamp /
    Meta post id / scheduled_for time. Uses COALESCE so any kwarg left as None
    preserves the existing value."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE content_items
                   SET status = %s,
                       slack_message_ts = COALESCE(%s, slack_message_ts),
                       approved_at = CASE WHEN %s = 'approved' THEN now() ELSE approved_at END,
                       posted_at = COALESCE(%s, posted_at),
                       meta_post_id = COALESCE(%s, meta_post_id),
                       scheduled_for = COALESCE(%s, scheduled_for)
                 WHERE id = %s
                """,
                (
                    status,
                    slack_message_ts,
                    status,
                    posted_at,
                    meta_post_id,
                    scheduled_for,
                    item_id,
                ),
            )
            conn.commit()
    finally:
        conn.close()


def claim_item_status(item_id, new_status, expected_status="approved"):
    """Atomically transition item_id from expected_status to new_status.

    `expected_status` may be a single status string or a tuple/list of
    acceptable current statuses (e.g. ('approved', 'needs_review')).

    Uses a single conditional UPDATE so two concurrent requests (e.g. a
    double-click on "Publish now") cannot both proceed to call the Meta
    Graph API for the same item. Returns True if this caller won the race
    (the row existed with expected_status and was flipped), False if the
    item was already in a different status (already claimed by another
    request, already published/scheduled, or not approved).

    Also stamps claimed_at for transient claims (publishing/scheduling) so
    the cron sweep can recover items orphaned by a mid-publish crash, and
    approved_at when the transition is an approval.
    """
    if isinstance(expected_status, str):
        expected = [expected_status]
    else:
        expected = list(expected_status)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE content_items
                   SET status = %s,
                       claimed_at = CASE WHEN %s IN ('publishing', 'scheduling')
                                         THEN now() ELSE claimed_at END,
                       approved_at = CASE WHEN %s = 'approved'
                                          THEN now() ELSE approved_at END
                 WHERE id = %s AND status = ANY(%s)
                RETURNING id
                """,
                (new_status, new_status, new_status, item_id, expected),
            )
            row = cur.fetchone()
            conn.commit()
            return row is not None
    finally:
        conn.close()


def recover_stuck_claims(max_age_minutes=15):
    """Park items stuck in publishing/scheduling as needs_review.

    A crash between claim_item_status and the final update_status leaves an
    item invisible in the queue with no retry path. The cron sweep calls this.
    Recovered items are NOT auto-retried: the original Meta call may have
    succeeded right before the crash, so a blind retry could create a
    duplicate Facebook post. A human must check the Facebook page, then
    either publish the item again (the queue allows publishing from
    needs_review) or delete it.

    Returns the recovered rows as [{id, brand}].
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE content_items
                   SET status = 'needs_review'
                 WHERE status IN ('publishing', 'scheduling')
                   AND claimed_at IS NOT NULL
                   AND claimed_at < now() - make_interval(mins => %s)
                RETURNING id, brand
                """,
                (max_age_minutes,),
            )
            rows = cur.fetchall()
            conn.commit()
            return [{"id": r[0], "brand": r[1]} for r in rows]
    finally:
        conn.close()


def update_content_image(item_id, image_url, image_prompt=None, image_model=None):
    """Record a generated image against a content item.

    Called after the on-demand "Generate image" Slack button runs the
    Claude slot-fill + cairosvg pipeline. All of image_url / image_prompt /
    image_model are persisted so we can debug prompts and re-run with a
    different model without losing history.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE content_items
                   SET image_url = %s,
                       image_prompt = COALESCE(%s, image_prompt),
                       image_model = COALESCE(%s, image_model),
                       image_generated_at = now()
                 WHERE id = %s
                """,
                (image_url, image_prompt, image_model, item_id),
            )
            conn.commit()
    finally:
        conn.close()


def get_content_item(item_id):
    """Return a content_items row by id, or None. Includes meta_post_id."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, brand, platform, draft_text, image_url, status,
                       slack_message_ts, scheduled_for, created_at,
                       approved_at, posted_at, meta_post_id, published_via,
                       image_prompt, image_model, image_generated_at
                  FROM content_items
                 WHERE id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "brand": row[1],
                "platform": row[2],
                "draft_text": row[3],
                "image_url": row[4],
                "status": row[5],
                "slack_message_ts": row[6],
                "scheduled_for": row[7],
                "created_at": row[8],
                "approved_at": row[9],
                "posted_at": row[10],
                "meta_post_id": row[11],
                "published_via": row[12],
                "image_prompt": row[13],
                "image_model": row[14],
                "image_generated_at": row[15],
            }
    finally:
        conn.close()


DELETABLE_STATUSES = (
    "pending_approval", "approved", "rejected", "scheduled", "needs_review",
    "error",
)


def delete_content_item(item_id):
    """Hard-delete a content item, atomically gated on status.

    Only rows in DELETABLE_STATUSES are removed — an item mid-publish
    (`publishing`/`scheduling`) or already `posted` is left alone so a
    concurrent publish task can't complete against a vanished row and
    posted history (used for cadence) is preserved.

    Returns True if a row was removed.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM content_items WHERE id = %s AND status = ANY(%s)",
                (item_id, list(DELETABLE_STATUSES)),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    finally:
        conn.close()


def get_last_activity(brand_id):
    """Return the created_at of the brand's newest non-rejected item, or None.

    Cadence baseline for cron generation. Using content creation (not
    posted_at) means schedule-only brands — which never accumulate `posted`
    rows because Meta auto-publishes — are not treated as "always due".
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max(created_at)
                  FROM content_items
                 WHERE brand = %s
                   AND status != 'rejected'
                """,
                (brand_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def has_pending_backlog(brand_id):
    """True if the brand has drafts still waiting for approval."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM content_items
                 WHERE brand = %s AND status = 'pending_approval'
                 LIMIT 1
                """,
                (brand_id,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def list_actionable_items(limit=20):
    """Return approved + scheduled + needs_review items, newest approved_at
    first.

    needs_review items were stuck mid-publish and recovered by the cron
    sweep; they surface in the queue so the operator can check the Facebook
    page, then publish again or delete.

    Joins brands for display_name when available; falls back to brand_id.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.brand, c.platform, c.draft_text, c.image_url,
                       c.status, c.scheduled_for, c.approved_at, c.created_at,
                       COALESCE(b.config->>'display_name', c.brand) AS display_name
                  FROM content_items c
                  LEFT JOIN brands b ON b.brand_id = c.brand
                 WHERE c.status IN ('approved', 'scheduled', 'needs_review')
                 ORDER BY c.approved_at DESC NULLS LAST, c.id DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "brand": r[1],
                    "platform": r[2],
                    "draft_text": r[3],
                    "image_url": r[4],
                    "status": r[5],
                    "scheduled_for": r[6],
                    "approved_at": r[7],
                    "created_at": r[8],
                    "display_name": r[9],
                }
                for r in rows
            ]
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


def _config_dict(value):
    """psycopg2 auto-decodes JSONB columns to dicts; older code paths and
    text-typed columns hand back strings. Accept both."""
    import json

    if isinstance(value, dict):
        return value
    return json.loads(value)


def get_brand_from_db(brand_id):
    """Return (config_dict, voice_md) for a DB-backed brand, or None if absent."""
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
            return _config_dict(row[0]), row[1]
    finally:
        conn.close()


def list_db_brands():
    """Return all DB-backed brand configs (active ones filtered by caller)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT config FROM brands")
            return [_config_dict(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()


def is_onboarded(brand_id):
    """True if a brand has a row in the `brands` table.

    Only brands that have been through Nova onboarding and approved land in
    `brands`. Filesystem-only seed brands (e.g. drewber, kgc before onboarding)
    return False, which lets the interactive generation flow prompt the user
    to onboard instead of silently generating with the fallback prompt.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM brands WHERE brand_id = %s", (brand_id,))
            return cur.fetchone() is not None
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


_ONBOARDING_SESSION_COLUMNS = """
    SELECT id, brand_id, display_name, channel, thread_ts,
           phase, answers, draft_voice_md, draft_config, status
      FROM onboarding_sessions
"""


def _onboarding_row_to_dict(row):
    """Hydrate an onboarding_sessions row (selected with
    _ONBOARDING_SESSION_COLUMNS) into a dict, coercing JSONB values that
    psycopg2 may hand back as strings."""
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


def get_onboarding_session_by_thread(thread_ts):
    """Return the onboarding session for a Slack thread, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                _ONBOARDING_SESSION_COLUMNS + "WHERE thread_ts = %s",
                (thread_ts,),
            )
            row = cur.fetchone()
            return _onboarding_row_to_dict(row) if row else None
    finally:
        conn.close()


def get_onboarding_session(brand_id):
    """Return the onboarding session for a brand, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                _ONBOARDING_SESSION_COLUMNS + "WHERE brand_id = %s",
                (brand_id,),
            )
            row = cur.fetchone()
            return _onboarding_row_to_dict(row) if row else None
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


def claim_onboarding_status(brand_id, new_status, expected_status):
    """Atomically transition an onboarding session's status.

    Same claim pattern as claim_item_status: a single conditional UPDATE so
    a double-clicked Approve/Reject/Regenerate button cannot run its slow
    side effects (brand upsert, Claude synthesis) twice. `expected_status`
    may be a string or a tuple/list. Returns True if this caller won.
    """
    if isinstance(expected_status, str):
        expected = [expected_status]
    else:
        expected = list(expected_status)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE onboarding_sessions
                   SET status = %s, updated_at = now()
                 WHERE brand_id = %s AND status = ANY(%s)
                RETURNING id
                """,
                (new_status, brand_id, expected),
            )
            row = cur.fetchone()
            conn.commit()
            return row is not None
    finally:
        conn.close()
