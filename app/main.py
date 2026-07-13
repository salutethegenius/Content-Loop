import os
import sys

# Allow `from core...` and `from routes...` style imports regardless of
# whether uvicorn is launched from the repo root or from inside app/.
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI

from routes.cron import router as cron_router
from routes.generate import router as generate_router
from routes.onboarding import router as onboarding_router
from routes.publish import router as publish_router
from routes.slack_commands import router as slack_commands_router
from routes.slack_events import router as slack_events_router
from routes.slack_interactions import router as slack_router
from routes.static import router as static_router

app = FastAPI(title="Content Loop Agent", version="1.7.1")

# Fail loudly at boot instead of KeyError deep inside a Slack background task
# (which acks 200 and then dies invisibly). Startup still proceeds so the
# health endpoint can report the problem.
REQUIRED_ENV = ("DATABASE_URL", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET",
                "ANTHROPIC_API_KEY")
_MISSING_ENV = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _MISSING_ENV:
    print(
        f"[main] FATAL CONFIG: missing required env vars: {', '.join(_MISSING_ENV)} "
        "— Slack buttons and generation WILL fail until these are set.",
        file=sys.stderr,
    )


@app.get("/")
def health():
    if _MISSING_ENV:
        return {
            "status": "degraded",
            "service": "content-loop",
            "missing_env": _MISSING_ENV,
        }
    return {"status": "ok", "service": "content-loop"}


app.include_router(cron_router)
app.include_router(generate_router)
app.include_router(onboarding_router)
app.include_router(publish_router)
app.include_router(slack_commands_router)
app.include_router(slack_events_router)
app.include_router(slack_router)
app.include_router(static_router)
