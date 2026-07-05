import os
import secrets
from fastapi import APIRouter, Header, HTTPException

from core.content_loop import run_content_loop

router = APIRouter()

CRON_SECRET = os.environ.get("CRON_SECRET", "")


@router.post("/cron/generate")
def run_content_loop_endpoint(x_cron_secret: str | None = Header(default=None)):
    """Generate one draft per due brand/platform and post it to Slack for approval."""
    if not CRON_SECRET or not x_cron_secret or not secrets.compare_digest(
        x_cron_secret, CRON_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    results = run_content_loop()
    return {"generated": results}
