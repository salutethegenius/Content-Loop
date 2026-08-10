from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core import db, meta_publisher
from core.brand_loader import get_brand_by_id
from routes.deps import require_cron_secret

router = APIRouter()


class PublishRequest(BaseModel):
    item_id: int
    scheduled_for: str | None = Field(
        default=None,
        description="Optional ISO 8601 datetime. If set, post is scheduled instead of published immediately.",
    )


def _credentials_for_item(item):
    """Resolve Meta page_id + token from the item's brand. Raises HTTPException."""
    try:
        return meta_publisher.resolve_for_item(item)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    require_cron_secret(x_cron_secret)

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

    page_id, token = _credentials_for_item(item)

    # Atomically claim the item so a duplicate/retried request cannot call
    # the Meta Graph API twice for the same item. needs_review items (stuck
    # publishes recovered by the cron sweep) may be re-published after the
    # operator has checked the Facebook page.
    claim_status = "scheduling" if body.scheduled_for else "publishing"
    if not db.claim_item_status(
        body.item_id, claim_status,
        expected_status=("approved", "needs_review"),
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Item {body.item_id} status is '{item['status']}', "
                "must be 'approved' or 'needs_review'"
            ),
        )

    if body.scheduled_for:
        try:
            meta_post_id = meta_publisher.schedule_page_post(
                item["draft_text"], body.scheduled_for,
                page_id=page_id, token=token,
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
                "page_id": page_id,
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
            "page_id": page_id,
        }

    try:
        meta_post_id = meta_publisher.publish_page_post(
            item["draft_text"],
            page_id=page_id, token=token,
            image_url=item.get("image_url"),
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
        "page_id": page_id,
    }


@router.get("/meta/verify")
def verify_meta_token(
    x_cron_secret: str | None = Header(default=None),
    brand_id: str = Query(default="biccu"),
):
    """Sanity check the brand's Page Access Token against its meta_page_id.
    Returns the page name on success. Cron-secret gated so it's not a public
    token oracle. Pass ?brand_id=kgc to verify a non-default brand."""
    require_cron_secret(x_cron_secret)
    brand = get_brand_by_id(brand_id)
    if not brand:
        raise HTTPException(
            status_code=404, detail=f"Unknown or inactive brand '{brand_id}'"
        )
    try:
        page_id, token = meta_publisher.resolve_for_brand(brand)
        name = meta_publisher.verify_token(page_id=page_id, token=token)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Meta verify failed: {exc}")
    return {
        "ok": True,
        "brand_id": brand_id,
        "page_id": page_id,
        "page_name": name,
    }
