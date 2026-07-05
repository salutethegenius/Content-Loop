"""Meta Graph API client for publishing posts to a Facebook Page.

V2 of the content loop. Direct Graph API calls (no MCP plugin). Kenneth
provides a long-lived Page Access Token with `pages_manage_posts` scope via
the META_PAGE_ACCESS_TOKEN env var; META_PAGE_ID is the BICCU page
(120368170965 = "Bahama Islands Co-operative Credit Union Limited").

Scheduled posts are handled natively by Meta: pass `published=false` plus a
`scheduled_publish_time` (unix seconds, 10 min - 6 months out) and Meta
publishes the post automatically at the requested time. No cron needed.
"""

import os
from datetime import datetime, timezone

import requests

GRAPH_BASE = "https://graph.facebook.com"
API_VERSION = os.environ.get("META_API_VERSION", "v23.0")


def _page_endpoint(page_id, edge):
    return f"{GRAPH_BASE}/{API_VERSION}/{page_id}/{edge}"


def _resolve(page_id, token):
    return page_id or os.environ.get("META_PAGE_ID"), token or os.environ.get(
        "META_PAGE_ACCESS_TOKEN"
    )


def publish_page_post(message, page_id=None, token=None):
    """Publish a text post to a Facebook Page immediately. Returns the Meta post id."""
    page_id, token = _resolve(page_id, token)
    if not page_id or not token:
        raise RuntimeError("META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set")

    resp = requests.post(
        _page_endpoint(page_id, "feed"),
        data={"message": message, "access_token": token},
        timeout=20,
    )
    data = resp.json()
    if "id" not in data:
        raise RuntimeError(f"Meta publish failed: {data}")
    return data["id"]


def schedule_page_post(message, scheduled_for_iso, page_id=None, token=None):
    """Schedule a text post on a Facebook Page. Returns the Meta post id.

    `scheduled_for_iso` is an ISO 8601 string (timezone-aware preferred).
    Meta requires the time to be 10 min - 6 months in the future.
    """
    page_id, token = _resolve(page_id, token)
    if not page_id or not token:
        raise RuntimeError("META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set")

    dt = datetime.fromisoformat(scheduled_for_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix = int(dt.astimezone(timezone.utc).timestamp())

    resp = requests.post(
        _page_endpoint(page_id, "feed"),
        data={
            "message": message,
            "published": "false",
            "scheduled_publish_time": str(unix),
            "access_token": token,
        },
        timeout=20,
    )
    data = resp.json()
    if "id" not in data:
        raise RuntimeError(f"Meta schedule failed: {data}")
    return data["id"]


def verify_token(page_id=None, token=None):
    """Sanity check that the page token works. Returns the page name on success."""
    page_id, token = _resolve(page_id, token)
    if not page_id or not token:
        raise RuntimeError("META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set")
    resp = requests.get(
        _page_endpoint(page_id, ""),
        params={"fields": "name", "access_token": token},
        timeout=15,
    )
    data = resp.json()
    if "name" not in data:
        raise RuntimeError(f"Meta token verify failed: {data}")
    return data["name"]
