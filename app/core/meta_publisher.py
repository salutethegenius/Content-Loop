"""Meta Graph API client for publishing posts to a Facebook Page.

Direct Graph API calls (no MCP plugin). Each brand stores `meta_page_id` and
`meta_token_env` in its config; resolve_for_brand() loads the matching Page
Access Token from the environment. Legacy callers may still pass page_id /
token explicitly, falling back to META_PAGE_ID / META_PAGE_ACCESS_TOKEN.

Scheduled posts are handled natively by Meta: pass `published=false` plus a
`scheduled_publish_time` (unix seconds, 10 min - 30 days out) and Meta
publishes the post automatically at the requested time. No cron needed.

Image schedules use Meta's two-step flow (required for Planner visibility):
  1. POST /{page_id}/photos with published=false + temporary=true (no schedule)
  2. POST /{page_id}/feed with attached_media + published=false +
     scheduled_publish_time + unpublished_content_type=SCHEDULED
A single /photos call with scheduled_publish_time can return a photo id
without creating a scheduled Page post — that was the Slack "success but
missing from Planner" bug.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

class ScheduleVerificationFailed(RuntimeError):
    """Meta accepted the schedule call and returned a post id, but the
    follow-up verification could not confirm it as a visible scheduled post
    (e.g. Graph API read-after-write lag). The post likely WAS created on
    Meta's side, so callers must persist `post_id` against the item (instead
    of leaving it retriable) to avoid a duplicate post on retry."""

    def __init__(self, post_id, message):
        super().__init__(message)
        self.post_id = post_id


GRAPH_BASE = "https://graph.facebook.com"
API_VERSION = os.environ.get("META_API_VERSION", "v23.0")
DEFAULT_TOKEN_ENV = "META_PAGE_ACCESS_TOKEN"

# Meta Pages API: scheduled_publish_time must be 10 minutes – 30 days out.
MIN_SCHEDULE_OFFSET_SEC = 10 * 60
MAX_SCHEDULE_OFFSET_SEC = 30 * 24 * 60 * 60


def _page_endpoint(page_id, edge=""):
    edge = edge.lstrip("/")
    if edge:
        return f"{GRAPH_BASE}/{API_VERSION}/{page_id}/{edge}"
    return f"{GRAPH_BASE}/{API_VERSION}/{page_id}"


def resolve_for_brand(brand_config):
    """Return (page_id, token) for a brand config.

    Requires brand_config['meta_page_id']. Token is loaded from the env var
    named by brand_config['meta_token_env'] (default META_PAGE_ACCESS_TOKEN).

    Raises RuntimeError if the brand has no page mapping or the token env is
    unset — never silently falls back to another brand's page.
    """
    if not brand_config:
        raise RuntimeError("No brand config provided for Meta publish")

    brand_id = brand_config.get("brand_id") or "?"
    page_id = (brand_config.get("meta_page_id") or "").strip()
    if not page_id:
        raise RuntimeError(
            f"Brand '{brand_id}' has no meta_page_id — refusing to publish "
            "to a default page. Set meta_page_id on the brand config."
        )

    token_env = (
        (brand_config.get("meta_token_env") or "").strip() or DEFAULT_TOKEN_ENV
    )
    token = os.environ.get(token_env) or ""
    if not token:
        raise RuntimeError(
            f"Brand '{brand_id}' token env '{token_env}' is not set"
        )
    return page_id, token


def _resolve(page_id, token):
    """Legacy resolve for explicit page_id/token args (env fallback)."""
    return page_id or os.environ.get("META_PAGE_ID"), token or os.environ.get(
        DEFAULT_TOKEN_ENV
    )


def _raise_if_error(data, label):
    """Raise if Graph returned an error object or no usable id."""
    if not isinstance(data, dict):
        raise RuntimeError(f"{label}: unexpected response {data!r}")
    if data.get("error"):
        raise RuntimeError(f"{label}: {data}")


def _extract_post_id(data, label):
    """Prefer page post_id over photo id when both are present."""
    _raise_if_error(data, label)
    post_id = data.get("post_id") or data.get("id")
    if not post_id:
        raise RuntimeError(f"{label}: {data}")
    return post_id


def _parse_schedule_unix(scheduled_for_iso):
    """Convert ISO datetime to unix seconds and validate Meta's window."""
    dt = datetime.fromisoformat(scheduled_for_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix = int(dt.astimezone(timezone.utc).timestamp())
    now = int(time.time())
    if unix < now + MIN_SCHEDULE_OFFSET_SEC:
        raise RuntimeError(
            "Schedule time must be at least 10 minutes in the future "
            f"(got {scheduled_for_iso})."
        )
    if unix > now + MAX_SCHEDULE_OFFSET_SEC:
        raise RuntimeError(
            "Schedule time must be within 30 days "
            f"(got {scheduled_for_iso}). Meta Planner rejects farther dates."
        )
    return unix


def publish_page_post(message, page_id=None, token=None, image_url=None):
    """Publish a post to a Facebook Page immediately. Returns the Meta post id.

    If `image_url` is provided, publishes a native photo post via the
    `/photos` edge with `caption=message` and `url=image_url`. Otherwise
    publishes a text-only post via the `/feed` edge.
    """
    page_id, token = _resolve(page_id, token)
    if not page_id or not token:
        raise RuntimeError("META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set")

    if image_url:
        resp = requests.post(
            _page_endpoint(page_id, "photos"),
            data={
                "url": image_url,
                "caption": message,
                "access_token": token,
            },
            timeout=20,
        )
    else:
        resp = requests.post(
            _page_endpoint(page_id, "feed"),
            data={"message": message, "access_token": token},
            timeout=20,
        )
    data = resp.json()
    return _extract_post_id(data, "Meta publish failed")


def _upload_temporary_photo(page_id, token, image_url):
    """Stage an unpublished photo for use in a scheduled feed post.

    Meta requires temporary=true for photos that will be attached to a
    scheduled post. Do NOT pass scheduled_publish_time here.
    """
    resp = requests.post(
        _page_endpoint(page_id, "photos"),
        data={
            "url": image_url,
            "published": "false",
            "temporary": "true",
            "access_token": token,
        },
        timeout=30,
    )
    data = resp.json()
    _raise_if_error(data, "Meta photo upload failed")
    photo_id = data.get("id")
    if not photo_id:
        raise RuntimeError(f"Meta photo upload failed: {data}")
    return photo_id


def schedule_page_post(message, scheduled_for_iso, page_id=None, token=None,
                       image_url=None):
    """Schedule a post on a Facebook Page. Returns the Meta page post id.

    `scheduled_for_iso` is an ISO 8601 string (timezone-aware preferred).
    Meta requires the time to be 10 min - 30 days in the future.

    Text-only: POST /feed with published=false + scheduled_publish_time.
    With image: two-step upload (temporary photo) then scheduled /feed post
    with attached_media — required for the post to appear in Meta Planner.
    """
    page_id, token = _resolve(page_id, token)
    if not page_id or not token:
        raise RuntimeError("META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set")

    unix = _parse_schedule_unix(scheduled_for_iso)

    if image_url:
        photo_id = _upload_temporary_photo(page_id, token, image_url)
        resp = requests.post(
            _page_endpoint(page_id, "feed"),
            data={
                "message": message or "",
                "attached_media[0]": json.dumps({"media_fbid": str(photo_id)}),
                "published": "false",
                "scheduled_publish_time": str(unix),
                "unpublished_content_type": "SCHEDULED",
                "access_token": token,
            },
            timeout=30,
        )
    else:
        resp = requests.post(
            _page_endpoint(page_id, "feed"),
            data={
                "message": message,
                "published": "false",
                "scheduled_publish_time": str(unix),
                "unpublished_content_type": "SCHEDULED",
                "access_token": token,
            },
            timeout=20,
        )
    data = resp.json()
    post_id = _extract_post_id(data, "Meta schedule failed")

    # Soft verify: confirm the post is queryable as scheduled. Raise a
    # distinguishable ScheduleVerificationFailed (carrying post_id) rather
    # than a bare RuntimeError, so callers can still persist the post id
    # against the item instead of leaving it retriable — Meta most likely
    # DID create the post (this can be read-after-write lag on Meta's side),
    # so blindly retrying on a bare failure would create a duplicate.
    try:
        verify_scheduled_post(post_id, page_id=page_id, token=token)
    except Exception as exc:
        raise ScheduleVerificationFailed(
            post_id,
            f"Meta returned id `{post_id}` but it is not visible as a "
            f"scheduled post yet (Planner may not show it): {exc}",
        ) from exc
    return post_id


def verify_scheduled_post(post_id, page_id=None, token=None):
    """Confirm a post id is a scheduled (unpublished) Page post.

    Checks the post node for scheduled_publish_time, then falls back to the
    page's scheduled_posts edge. Raises RuntimeError if not found/scheduled.
    """
    page_id, token = _resolve(page_id, token)
    if not page_id or not token:
        raise RuntimeError("META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set")

    resp = requests.get(
        f"{GRAPH_BASE}/{API_VERSION}/{post_id}",
        params={
            "fields": "id,is_published,scheduled_publish_time,message",
            "access_token": token,
        },
        timeout=15,
    )
    data = resp.json()
    _raise_if_error(data, "Meta schedule verify failed")
    if data.get("scheduled_publish_time"):
        return data

    # Fallback: scan scheduled_posts edge for this id
    edge = requests.get(
        _page_endpoint(page_id, "scheduled_posts"),
        params={"fields": "id,scheduled_publish_time", "access_token": token},
        timeout=15,
    )
    edge_data = edge.json()
    _raise_if_error(edge_data, "Meta scheduled_posts list failed")
    ids = {row.get("id") for row in (edge_data.get("data") or [])}
    # Meta sometimes returns bare post id vs pageId_postId
    bare = str(post_id).split("_")[-1]
    if post_id in ids or bare in ids or any(
        str(i).endswith(bare) for i in ids if i
    ):
        return {"id": post_id, "via": "scheduled_posts"}
    raise RuntimeError(
        f"post `{post_id}` not found on scheduled_posts "
        f"(edge returned {len(ids)} items)"
    )


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
