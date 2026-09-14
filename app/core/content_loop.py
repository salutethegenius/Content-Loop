"""Shared content generation loop used by cron and interactive Slack flows."""

import sys
import traceback

from core.brand_loader import get_active_brands, is_due_for_post
from core.db import (
    get_conn,
    get_last_activity,
    has_pending_backlog,
    save_draft,
    update_status,
)
from core.generator import generate_draft
from core.slack_client import post_for_approval

# Arbitrary constant identifying the content-loop advisory lock in Postgres.
LOOP_LOCK_KEY = 913522041


def try_with_loop_lock(fn):
    """Run fn() while holding the content-loop advisory lock.

    Returns (True, fn()) if the lock was acquired, or (False, None) if
    another generation run currently holds it. Both the cron loop and the
    interactive Slack flow acquire this lock, so overlapping runs can't
    generate duplicate drafts for the same brand/platform.
    """
    lock_conn = get_conn()
    try:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOOP_LOCK_KEY,))
            acquired = cur.fetchone()[0]
        if not acquired:
            return False, None
        try:
            return True, fn()
        finally:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOOP_LOCK_KEY,))
    finally:
        lock_conn.close()


def generate_for_platforms(brand, platforms, topic=None):
    """Generate drafts for one brand on the given platforms.

    `topic` is an optional operator-supplied brief. When omitted, the
    generator picks a pillar from memory and avoids recent wording.

    Skips platforms not in the brand config. Returns a list of result dicts.
    A failure on one platform (Claude error, Slack error, DB error, etc.) is
    caught and recorded as an error entry so it cannot abort generation for
    the remaining platforms/brands in the same run (see run_content_loop).
    """
    brand_platforms = set(brand.get("platforms") or [])
    results = []
    for platform in platforms:
        if platform not in brand_platforms:
            continue
        item_id = None
        try:
            draft_text = generate_draft(brand, platform, topic=topic)
            item_id = save_draft(brand["brand_id"], platform, draft_text)
            ts = post_for_approval(
                item_id, brand["display_name"], platform, draft_text
            )
            update_status(item_id, "pending_approval", slack_message_ts=ts)
            results.append(
                {
                    "brand": brand["brand_id"],
                    "platform": platform,
                    "item_id": item_id,
                }
            )
        except Exception as exc:
            print(
                f"[content_loop] generation failed for brand={brand.get('brand_id')} "
                f"platform={platform}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            if item_id is not None:
                # The row was inserted as pending_approval but never reached
                # Slack. Left alone it would block the brand's cadence forever
                # (has_pending_backlog) with no visible message to act on, so
                # park it as a terminal error instead.
                try:
                    update_status(item_id, "error")
                except Exception:
                    print(
                        f"[content_loop] could not mark item {item_id} as error",
                        file=sys.stderr,
                    )
            results.append(
                {
                    "brand": brand["brand_id"],
                    "platform": platform,
                    "error": str(exc),
                }
            )
    return results


def run_content_loop():
    """Generate one draft per due brand/platform (blanket cron path).

    Each brand is isolated: a failure loading/checking one brand (bad config,
    DB hiccup) is recorded as an error entry and does not prevent the
    remaining brands from being processed in the same run.

    Guards:
    - Postgres advisory lock so overlapping cron fires (double trigger,
      manual + scheduled) can't generate duplicates concurrently.
    - Cadence is based on the newest non-rejected draft's created_at
      (get_last_activity), so schedule-only brands aren't "always due".
    - Brands with a pending_approval backlog are skipped until the human
      clears the queue.
    """
    acquired, results = try_with_loop_lock(_run_content_loop_locked)
    if not acquired:
        return [{"skipped": "content loop already running"}]
    return results


def _run_content_loop_locked():
    results = []
    for brand in get_active_brands():
        brand_id = brand.get("brand_id")
        try:
            if has_pending_backlog(brand_id):
                results.append(
                    {"brand": brand_id, "skipped": "pending_approval backlog"}
                )
                continue
            if not is_due_for_post(brand, get_last_activity(brand_id)):
                continue
            results.extend(
                generate_for_platforms(brand, brand.get("platforms") or [])
            )
        except Exception as exc:
            print(
                f"[content_loop] run failed for brand={brand_id}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            results.append({"brand": brand_id, "error": str(exc)})
    return results
