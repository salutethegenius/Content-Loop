import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from core.brand_loader import get_active_brands
from core.generation_flow import format_brand_picker_blocks
from core.slack_client import post_message
from routes.deps import require_cron_secret

router = APIRouter()


class GenerateStartRequest(BaseModel):
    channel: str | None = None


@router.post("/generate/start")
def start_generation(
    body: GenerateStartRequest | None = None,
    x_cron_secret: str | None = Header(default=None),
):
    """Open an interactive Nova generation flow in Slack.

    Posts a brand picker. Button clicks continue in /slack/interactions.
    """
    require_cron_secret(x_cron_secret)

    channel = (body.channel if body and body.channel else None) or os.environ.get(
        "SLACK_CONTENT_CHANNEL", ""
    )
    if not channel:
        raise HTTPException(status_code=400, detail="channel required")

    brands = get_active_brands()
    if not brands:
        raise HTTPException(status_code=400, detail="No active brands")

    blocks = format_brand_picker_blocks(brands)
    ts = post_message(
        channel,
        blocks=blocks,
        text="Who should I generate posts for?",
    )
    return {
        "ok": True,
        "channel": channel,
        "message_ts": ts,
        "brands": [b["brand_id"] for b in brands],
    }
