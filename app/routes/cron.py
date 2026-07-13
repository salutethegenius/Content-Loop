import os
import secrets
from fastapi import APIRouter, Header, HTTPException

from core import db
from core.content_loop import run_content_loop

router = APIRouter()

CRON_SECRET = os.environ.get("CRON_SECRET", "")


def _sweep_stuck_claims():
    """Recover items orphaned in publishing/scheduling by a mid-publish crash.

    Reverts them to approved and alerts the content channel: the operator
    must check the Facebook page before re-publishing, because the Meta call
    may have succeeded right before the crash.
    """
    try:
        recovered = db.recover_stuck_claims(max_age_minutes=15)
    except Exception:
        return []
    if recovered:
        try:
            from core.slack_client import post_message

            ids = ", ".join(f"#{r['id']} ({r['brand']})" for r in recovered)
            post_message(
                os.environ.get("SLACK_CONTENT_CHANNEL", ""),
                text=(
                    f":warning: Recovered {len(recovered)} post(s) stuck "
                    f"mid-publish: {ids}. They are back to *approved* — check "
                    "the Facebook page before re-publishing, the original "
                    "publish may have gone through."
                ),
            )
        except Exception:
            pass
    return recovered


@router.post("/cron/generate")
def run_content_loop_endpoint(x_cron_secret: str | None = Header(default=None)):
    """Generate one draft per due brand/platform and post it to Slack for approval."""
    if not CRON_SECRET or not x_cron_secret or not secrets.compare_digest(
        x_cron_secret, CRON_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    recovered = _sweep_stuck_claims()
    results = run_content_loop()
    return {"generated": results, "recovered_stuck": [r["id"] for r in recovered]}
