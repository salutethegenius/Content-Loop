import os
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core import db, onboarding
from core.slack_client import post_message

router = APIRouter()

CRON_SECRET = os.environ.get("CRON_SECRET", "")


class OnboardStartRequest(BaseModel):
    brand_id: str
    display_name: str
    channel: str  # Slack channel ID where the onboarding thread should be opened


@router.post("/onboard/start")
def start_onboarding(
    body: OnboardStartRequest,
    x_cron_secret: str | None = Header(default=None),
):
    """Open a Nova onboarding thread in Slack for a new brand.

    Admin-triggered. Requires the X-Cron-Secret header (same shared secret as
    the cron endpoint) so the public URL cannot be abused.
    """
    if not CRON_SECRET or not x_cron_secret or not secrets.compare_digest(
        x_cron_secret, CRON_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    brand_id = body.brand_id.strip().lower()
    if not brand_id or not body.display_name or not body.channel:
        raise HTTPException(status_code=400, detail="brand_id, display_name, channel all required")

    # Post the welcome + first phase as a top-level message in the channel.
    welcome = (
        f"*Nova onboarding for {body.display_name}*\n"
        f"Hi! I'm Nova. I'll ask a few batches of questions to learn your brand's "
        f"voice and content territory. Reply in this thread, and type `next` when "
        f"you are ready to move on. At the end I'll draft your voice.md and "
        f"config.json for approval."
    )
    thread_ts = post_message(body.channel, text=welcome)

    # Post phase 1 as the first threaded reply.
    phase_msg = onboarding.format_phase_message(body.display_name, "identity")
    post_message(body.channel, text=phase_msg, thread_ts=thread_ts)

    # Persist the session.
    db.create_onboarding_session(brand_id, body.display_name, body.channel, thread_ts)

    return {"ok": True, "brand_id": brand_id, "thread_ts": thread_ts, "phase": "identity"}
