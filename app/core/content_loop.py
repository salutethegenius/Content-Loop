"""Shared content generation loop used by cron and interactive Slack flows."""

import sys
import traceback

from core.brand_loader import get_active_brands, is_due_for_post
from core.db import get_last_posted, save_draft, update_status
from core.generator import generate_draft
from core.slack_client import post_for_approval


def generate_for_platforms(brand, platforms):
    """Generate drafts for one brand on the given platforms.

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
        try:
            draft_text = generate_draft(brand, platform)
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
    """
    results = []
    for brand in get_active_brands():
        try:
            last_posted = get_last_posted(brand["brand_id"])
            if not is_due_for_post(brand, last_posted):
                continue
            results.extend(
                generate_for_platforms(brand, brand.get("platforms") or [])
            )
        except Exception as exc:
            print(
                f"[content_loop] run failed for brand={brand.get('brand_id')}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            results.append({"brand": brand.get("brand_id"), "error": str(exc)})
    return results
