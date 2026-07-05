import os
import secrets
from fastapi import APIRouter, Header, HTTPException

from core.brand_loader import get_active_brands, is_due_for_post
from core.db import get_last_posted, save_draft, update_status
from core.generator import generate_draft
from core.slack_client import post_for_approval

router = APIRouter()

CRON_SECRET = os.environ.get("CRON_SECRET", "")


@router.post("/cron/generate")
def run_content_loop(x_cron_secret: str | None = Header(default=None)):
    """Generate one draft per due brand/platform and post it to Slack for approval."""
    if not CRON_SECRET or not x_cron_secret or not secrets.compare_digest(
        x_cron_secret, CRON_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    results = []
    for brand in get_active_brands():
        last_posted = get_last_posted(brand["brand_id"])
        if not is_due_for_post(brand, last_posted):
            continue
        for platform in brand["platforms"]:
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
    return {"generated": results}
