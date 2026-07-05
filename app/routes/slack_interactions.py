import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request

from core.db import update_status

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
async def handle_interaction(request: Request):
    """Handle Approve/Reject button clicks from Slack."""
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
    try:
        item_id = int(value.split("_", 1)[1])
    except (IndexError, ValueError):
        raise HTTPException(status_code=400, detail="Bad action value")

    if action_id == "approve":
        update_status(item_id, "approved")
    elif action_id == "reject":
        update_status(item_id, "rejected")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action_id}")

    return {"ok": True}
