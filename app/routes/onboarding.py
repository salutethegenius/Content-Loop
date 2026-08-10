from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core import onboarding
from routes.deps import require_cron_secret

router = APIRouter()


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
    require_cron_secret(x_cron_secret)

    brand_id = body.brand_id.strip().lower()
    if not brand_id or not body.display_name or not body.channel:
        raise HTTPException(status_code=400, detail="brand_id, display_name, channel all required")

    try:
        thread_ts = onboarding.start_session(brand_id, body.display_name, body.channel)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not post onboarding welcome to Slack: {exc}",
        )

    return {"ok": True, "brand_id": brand_id, "thread_ts": thread_ts, "phase": "identity"}
