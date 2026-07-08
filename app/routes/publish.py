import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from core import db, meta_publisher

router = APIRouter()

CRON_SECRET = os.environ.get("CRON_SECRET", "")


class PublishRequest(BaseModel):
    item_id: int
    scheduled_for: str | None = Field(
        default=None,
        description="Optional ISO 8601 datetime. If set, post is scheduled instead of published immediately.",
    )


def _check_cron_secret(x_cron_secret: str | None):
    if not CRON_SECRET or not x_cron_secret or not secrets.compare_digest(
        x_cron_secret, CRON_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/publish")
def publish_item(
    body: PublishRequest,
    x_cron_secret: str | None = Header(default=None),
):
    """Publish (or schedule) an approved content item to its Facebook Page via
    the Meta Graph API. V2 supports facebook only.

    Body:
      - item_id (int, required)
      - scheduled_for (ISO 8601 str, optional) -> schedules instead of publishes
    """
    _check_cron_secret(x_cron_secret)

    item = db.get_content_item(body.item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    if item["platform"] != "facebook":
        raise HTTPException(
            status_code=400,
            detail="V2 supports facebook publishing only (instagram coming in V2.1)",
        )
    if not item.get("draft_text"):
        raise HTTPException(status_code=400, detail="Item has no draft_text")

    # Atomically claim the item so a duplicate/retried request cannot call
    # the Meta Graph API twice for the same item.
    claim_status = "scheduling" if body.scheduled_for else "publishing"
    if not db.claim_item_status(body.item_id, claim_status, expected_status="approved"):
        raise HTTPException(
            status_code=400,
            detail=f"Item {body.item_id} status is '{item['status']}', must be 'approved'",
        )

    if body.scheduled_for:
        try:
            meta_post_id = meta_publisher.schedule_page_post(
                item["draft_text"], body.scheduled_for,
                image_url=item.get("image_url"),
            )
        except meta_publisher.ScheduleVerificationFailed as exc:
            # Meta most likely created the post; persist it as scheduled so a
            # retry cannot create a duplicate, but flag it for a manual check.
            db.update_status(
                body.item_id,
                "scheduled",
                meta_post_id=exc.post_id,
                scheduled_for=body.scheduled_for,
            )
            return {
                "ok": True,
                "status": "scheduled",
                "verified": False,
                "meta_post_id": exc.post_id,
                "scheduled_for": body.scheduled_for,
                "image_url": item.get("image_url"),
                "warning": str(exc),
            }
        except Exception as exc:
            db.update_status(body.item_id, "approved")
            raise HTTPException(status_code=502, detail=f"Meta schedule failed: {exc}")
        db.update_status(
            body.item_id,
            "scheduled",
            meta_post_id=meta_post_id,
            scheduled_for=body.scheduled_for,
        )
        return {
            "ok": True,
            "status": "scheduled",
            "meta_post_id": meta_post_id,
            "scheduled_for": body.scheduled_for,
            "image_url": item.get("image_url"),
        }

    try:
        meta_post_id = meta_publisher.publish_page_post(
            item["draft_text"], image_url=item.get("image_url"),
        )
    except Exception as exc:
        db.update_status(body.item_id, "approved")
        raise HTTPException(status_code=502, detail=f"Meta publish failed: {exc}")
    db.update_status(
        body.item_id,
        "posted",
        meta_post_id=meta_post_id,
        posted_at=datetime.now(timezone.utc),
    )
    return {
        "ok": True,
        "status": "posted",
        "meta_post_id": meta_post_id,
        "image_url": item.get("image_url"),
    }


@router.get("/meta/verify")
def verify_meta_token(x_cron_secret: str | None = Header(default=None)):
    """Sanity check the META_PAGE_ACCESS_TOKEN works against META_PAGE_ID.
    Returns the page name on success. Cron-secret gated so it's not a public
    token oracle."""
    _check_cron_secret(x_cron_secret)
    try:
        name = meta_publisher.verify_token()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Meta verify failed: {exc}")
    return {"ok": True, "page_name": name}
