import os
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core import onboarding

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

    thread_ts = onboarding.start_session(brand_id, body.display_name, body.channel)
    if not thread_ts:
        raise HTTPException(status_code=502, detail="Could not post onboarding welcome to Slack")

    return {"ok": True, "brand_id": brand_id, "thread_ts": thread_ts, "phase": "identity"}
