"""Shared route helpers.

All admin/automation endpoints (cron, generate, onboard, publish) are gated
by the same X-Cron-Secret header so the public Railway URL cannot be abused.
"""

import os
import secrets

from fastapi import HTTPException

CRON_SECRET = os.environ.get("CRON_SECRET", "")


def require_cron_secret(x_cron_secret):
    """Raise 401 unless the X-Cron-Secret header matches CRON_SECRET."""
    if not CRON_SECRET or not x_cron_secret or not secrets.compare_digest(
        x_cron_secret, CRON_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
