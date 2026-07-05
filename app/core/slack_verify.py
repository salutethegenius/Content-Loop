"""Shared Slack request-signature verification (HMAC-SHA256).

Slash commands, interactive payloads, and Events API requests are all signed
the same way. Kept here so route modules don't reimplement the same check.
"""

import hashlib
import hmac
import os
import time

REPLAY_TOLERANCE_SECONDS = 60 * 5


def verify_slack_signature(timestamp: str, signature: str, body: bytes) -> bool:
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > REPLAY_TOLERANCE_SECONDS:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(
        signing_secret.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
