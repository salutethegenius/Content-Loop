import os
import sys

# Allow `from core...` and `from routes...` style imports regardless of
# whether uvicorn is launched from the repo root or from inside app/.
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI

from routes.cron import router as cron_router
from routes.slack_interactions import router as slack_router

app = FastAPI(title="Content Loop Agent", version="1.0.0")


@app.get("/")
def health():
    return {"status": "ok", "service": "content-loop"}


app.include_router(cron_router)
app.include_router(slack_router)
