import os

from fastapi import APIRouter, Header

from core import db
from core.content_loop import run_content_loop
from routes.deps import require_cron_secret

router = APIRouter()


def _sweep_stuck_claims():
    """Recover items orphaned in publishing/scheduling by a mid-publish crash.

    Parks them as needs_review (no auto-retry) and alerts the content
    channel: the operator must check the Facebook page first, because the
    Meta call may have succeeded right before the crash and a blind retry
    would create a duplicate post.
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
                    f"mid-publish: {ids}. They are parked as *needs review* — "
                    "check the Facebook page first: if the post went live, "
                    "delete the item from the /nova queue; if not, open it "
                    "there and publish again."
                ),
            )
        except Exception:
            pass
    return recovered


@router.post("/cron/generate")
def run_content_loop_endpoint(x_cron_secret: str | None = Header(default=None)):
    """Generate one draft per due brand/platform and post it to Slack for approval."""
    require_cron_secret(x_cron_secret)

    recovered = _sweep_stuck_claims()
    results = run_content_loop()
    return {"generated": results, "recovered_stuck": [r["id"] for r in recovered]}
