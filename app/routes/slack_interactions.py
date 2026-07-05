import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core import db, generation_flow, onboarding
from core.db import update_status
from core.slack_client import post_message

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
    - Interactive generation: `gen_pick_brand`, `gen_confirm`.
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
        update_status(item_id, "approved" if action_id == "approve" else "rejected")
        return {"ok": True}

    # --- Onboarding actions (defer slow work to background) ---
    if action_id in ("onboard_approve", "onboard_reject", "onboard_regenerate"):
        brand_id = value
        background_tasks.add_task(
            _handle_onboarding_action, action_id, brand_id, channel, thread_ts
        )
        return {"ok": True}

    # --- Interactive generation actions ---
    if action_id == "gen_pick_brand":
        brand_id = value.strip().lower()
        background_tasks.add_task(
            generation_flow.handle_pick_brand, brand_id, channel, thread_ts
        )
        return {"ok": True}

    if action_id == "gen_confirm":
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
