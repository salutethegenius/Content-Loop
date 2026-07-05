import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from core import db, generation_flow, onboarding
from core.db import update_status
from core.slack_client import (
    format_publish_result_blocks,
    format_resolved_approval_blocks,
    format_schedule_picker_blocks,
    post_message,
    update_message,
)

router = APIRouter()

REPLAY_TOLERANCE_SECONDS = 60 * 5


def _verify_slack_signature(timestamp: str, signature: str, body: bytes) -> bool:
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > REPLAY_TOLERANCE_SECONDS:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(
        signing_secret.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/slack/interactions")
async def handle_interaction(request: Request, background_tasks: BackgroundTasks):
    """Handle Approve/Reject button clicks from Slack.

    Action families:
    - Content approval: action_id `approve`/`reject`, value `{action}_{item_id}`.
    - Onboarding approval: action_id `onboard_approve`/`onboard_reject`/
      `onboard_regenerate`, value `brand_id`.
    - Interactive generation: `gen_pick_brand_*`, `gen_confirm_*`.
    """
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(timestamp, signature, raw_body):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    parsed = parse_qs(raw_body.decode())
    payload_str = parsed.get("payload", [""])[0]
    if not payload_str:
        raise HTTPException(status_code=400, detail="Missing payload")

    payload = json.loads(payload_str)
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": True}

    action = actions[0]
    action_id = action.get("action_id")
    value = action.get("value", "")
    channel = payload.get("channel", {}).get("id")
    message = payload.get("message", {}) or {}
    thread_ts = message.get("thread_ts") or message.get("ts")

    # --- Content approval actions ---
    if action_id in ("approve", "reject"):
        try:
            item_id = int(value.split("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        status = "approved" if action_id == "approve" else "rejected"
        user_id = (payload.get("user") or {}).get("id")
        # Ack immediately (Slack needs a response within 3s). The DB update +
        # in-place message update happen in a background task so a slow DB
        # connection can't cause Slack to silently drop the replace_original.
        background_tasks.add_task(
            _handle_approval, item_id, status, user_id,
            channel, message.get("ts"), message.get("blocks"),
        )
        return {"ok": True}

    # --- Publish actions (V2) ---
    if action_id == "publish_now":
        try:
            item_id = int(value.split("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        # Ack immediately with a "Publishing..." state; background task does
        # the Graph API call and flips the message to the final status.
        intermediate = format_publish_result_blocks(
            message.get("blocks"), ":clock1: Publishing to Facebook..."
        )
        background_tasks.add_task(
            _handle_publish_now, item_id, channel, message.get("ts"),
            message.get("blocks"),
        )
        return JSONResponse(
            content={"replace_original": True, "blocks": intermediate}
        )

    if action_id == "publish_schedule":
        try:
            item_id = int(value.split("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        picker_blocks = format_schedule_picker_blocks(item_id, message.get("blocks"))
        return JSONResponse(
            content={"replace_original": True, "blocks": picker_blocks}
        )

    if action_id == "publish_confirm":
        try:
            item_id = int(value.split("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        # Pull the datetimepicker value out of state.values
        selected_ts = _extract_datetimepicker_value(payload, item_id)
        if not selected_ts:
            return JSONResponse(
                content={
                    "replace_original": True,
                    "blocks": format_publish_result_blocks(
                        message.get("blocks"),
                        ":x: No time selected. Click Schedule again.",
                    ),
                }
            )
        intermediate = format_publish_result_blocks(
            message.get("blocks"), ":clock1: Scheduling on Facebook..."
        )
        background_tasks.add_task(
            _handle_publish_schedule, item_id, selected_ts,
            channel, message.get("ts"), message.get("blocks"),
        )
        return JSONResponse(
            content={"replace_original": True, "blocks": intermediate}
        )

    # datetimepicker change events — Slack still requires a 200 ack
    if action_id and action_id.startswith("publish_dt_"):
        return {"ok": True}

    # --- Onboarding actions (defer slow work to background) ---
    if action_id in ("onboard_approve", "onboard_reject", "onboard_regenerate"):
        brand_id = value
        background_tasks.add_task(
            _handle_onboarding_action, action_id, brand_id, channel, thread_ts
        )
        return {"ok": True}

    # --- Interactive generation actions ---
    if action_id and action_id.startswith("gen_pick_brand_"):
        brand_id = value.strip().lower()
        background_tasks.add_task(
            generation_flow.handle_pick_brand, brand_id, channel, thread_ts
        )
        return {"ok": True}

    if action_id and action_id.startswith("gen_confirm_"):
        brand_id, platforms = generation_flow.parse_confirm_value(value)
        background_tasks.add_task(
            generation_flow.handle_confirm, brand_id, platforms, channel, thread_ts
        )
        return {"ok": True}

    raise HTTPException(status_code=400, detail=f"Unknown action: {action_id}")


def _handle_onboarding_action(action_id, brand_id, channel, thread_ts):
    session = db.get_onboarding_session(brand_id)
    if not session:
        return

    if action_id == "onboard_approve":
        if not session.get("draft_voice_md") or not session.get("draft_config"):
            post_message(
                channel,
                text="No draft on file. Click Regenerate first.",
                thread_ts=thread_ts,
            )
            return
        db.upsert_brand(brand_id, session["draft_config"], session["draft_voice_md"])
        db.set_onboarding_status(brand_id, "approved")
        db.set_onboarding_phase(brand_id, "done")
        post_message(
            channel,
            text=(
                f"Approved. {session['display_name']} is now live in the content loop. "
                f"It will be picked up on the next cron run."
            ),
            thread_ts=thread_ts,
        )
        return

    if action_id == "onboard_reject":
        db.set_onboarding_status(brand_id, "rejected")
        post_message(
            channel,
            text="Rejected. Reply in this thread with what to change, then click Regenerate.",
            thread_ts=thread_ts,
        )
        return

    if action_id == "onboard_regenerate":
        try:
            # Pull any replies posted since the last draft (status was
            # awaiting_approval) into the answers before re-synthesizing.
            answers = session["answers"]
            voice_md, config = onboarding.synthesize_brand(
                brand_id, session["display_name"], answers
            )
            db.save_onboarding_draft(brand_id, voice_md, config)
            db.set_onboarding_status(brand_id, "in_progress")
            blocks = onboarding.format_draft_message(
                brand_id, session["display_name"], voice_md, config
            )
            post_message(
                channel,
                blocks=blocks,
                thread_ts=thread_ts,
                text=f"Regenerated draft for {session['display_name']}",
            )
        except Exception as exc:
            post_message(
                channel,
                text=f"Regeneration failed: {exc}",
                thread_ts=thread_ts,
            )


def _handle_approval(item_id, status, user_id, channel, message_ts,
                     original_blocks):
    """Background task: update content_items status and replace the Slack
    draft message in place with the resolved status line (+ Publish/Schedule
    buttons for approved facebook drafts)."""
    update_status(item_id, status)
    item = db.get_content_item(item_id)
    platform = item.get("platform") if item else None
    updated_blocks = format_resolved_approval_blocks(
        original_blocks, status, user_id,
        item_id=item_id, platform=platform,
    )
    try:
        update_message(channel, message_ts, blocks=updated_blocks)
    except Exception as exc:
        # Don't surface Slack failures to the user — the DB status is already
        # correct, which is the source of truth. Log via a thread reply.
        post_message(
            channel,
            text=f"(approval recorded in DB, but Slack message update failed: {exc})",
            thread_ts=message_ts,
        )


def _extract_datetimepicker_value(payload, item_id):
    """Read the selected unix timestamp from a datetimepicker in the
    interaction's state.values, keyed by the publish_dt_{item_id} action id."""
    state = payload.get("state") or {}
    values = state.get("values") or {}
    for block_values in values.values():
        for action_id, action_state in block_values.items():
            if action_id == f"publish_dt_{item_id}":
                selected = (action_state or {}).get("selected_date_time")
                return selected
    return None


def _handle_publish_now(item_id, channel, message_ts, original_blocks):
    """Background task: publish an approved facebook draft via the Meta Graph
    API, then flip the Slack message to a final status line."""
    from datetime import datetime, timezone

    from core import meta_publisher

    item = db.get_content_item(item_id)
    if not item:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, ":x: Item not found."
            ),
        )
        return
    if item["status"] != "approved":
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                f":x: Cannot publish — item status is '{item['status']}'.",
            ),
        )
        return
    if item["platform"] != "facebook":
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                ":x: V2 supports Facebook publishing only.",
            ),
        )
        return

    try:
        meta_post_id = meta_publisher.publish_page_post(item["draft_text"])
    except Exception as exc:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, f":x: Meta publish failed: {exc}"
            ),
        )
        return

    db.update_status(
        item_id, "posted",
        meta_post_id=meta_post_id,
        posted_at=datetime.now(timezone.utc),
    )
    update_message(
        channel, message_ts,
        blocks=format_publish_result_blocks(
            original_blocks,
            f":rocket: Published to Facebook. Meta post id: `{meta_post_id}`",
        ),
    )


def _handle_publish_schedule(item_id, selected_ts, channel, message_ts,
                             original_blocks):
    """Background task: schedule an approved facebook draft via the Meta Graph
    API, then flip the Slack message to a final status line."""
    from datetime import datetime, timezone

    from core import meta_publisher

    item = db.get_content_item(item_id)
    if not item:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, ":x: Item not found."
            ),
        )
        return
    if item["status"] != "approved":
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                f":x: Cannot schedule — item status is '{item['status']}'.",
            ),
        )
        return
    if item["platform"] != "facebook":
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                ":x: V2 supports Facebook scheduling only.",
            ),
        )
        return

    # Slack datetimepicker returns unix seconds (int). Meta wants unix seconds
    # for scheduled_publish_time, but our meta_publisher takes an ISO string.
    scheduled_dt = datetime.fromtimestamp(selected_ts, tz=timezone.utc)
    iso = scheduled_dt.isoformat()

    try:
        meta_post_id = meta_publisher.schedule_page_post(item["draft_text"], iso)
    except Exception as exc:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, f":x: Meta schedule failed: {exc}"
            ),
        )
        return

    db.update_status(
        item_id, "scheduled",
        meta_post_id=meta_post_id,
        scheduled_for=iso,
    )
    update_message(
        channel, message_ts,
        blocks=format_publish_result_blocks(
            original_blocks,
            f":calendar: Scheduled for {scheduled_dt.strftime('%Y-%m-%d %H:%M UTC')}. "
            f"Meta post id: `{meta_post_id}`",
        ),
    )
