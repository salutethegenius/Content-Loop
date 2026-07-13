import os

import requests

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
SLACK_UPDATE_URL = "https://slack.com/api/chat.update"


def _extract_item_id_from_blocks(blocks):
    """Scan a draft message's action blocks for the approve button value to
    recover the content_item id. Returns int or None."""
    for b in blocks or []:
        if b.get("type") != "actions":
            continue
        for el in b.get("elements", []):
            if el.get("action_id") == "approve":
                val = el.get("value", "")
                try:
                    return int(val.split("_", 1)[1])
                except (IndexError, ValueError):
                    return None
    return None


def _delete_button(item_id):
    """Danger-styled Delete button with a Slack confirm dialog."""
    return {
        "type": "button",
        "style": "danger",
        "text": {"type": "plain_text", "text": "Delete"},
        "value": f"delete_item_{item_id}",
        "action_id": "delete_item",
        "confirm": {
            "title": {"type": "plain_text", "text": "Delete this post?"},
            "text": {
                "type": "mrkdwn",
                "text": (
                    "This removes the post from the queue. If it is already "
                    "scheduled on Facebook, the scheduled post is cancelled "
                    "too. This cannot be undone."
                ),
            },
            "confirm": {"type": "plain_text", "text": "Delete"},
            "deny": {"type": "plain_text", "text": "Keep"},
        },
    }


def format_resolved_approval_blocks(original_blocks, status, user_id=None,
                                    item_id=None, platform=None):
    """Replace Approve/Reject buttons with a status line on the draft message.

    For approved facebook items, also append Publish now / Schedule buttons so
    the human can push the post to Meta from Slack without leaving the channel.
    """
    blocks = [b for b in (original_blocks or []) if b.get("type") != "actions"]
    label = "Approved" if status == "approved" else "Rejected"
    emoji = ":white_check_mark:" if status == "approved" else ":x:"
    if user_id:
        status_text = f"{emoji} *{label}* by <@{user_id}>"
    else:
        status_text = f"{emoji} *{label}*"

    if item_id is None:
        item_id = _extract_item_id_from_blocks(original_blocks)

    if status == "approved" and platform == "facebook" and item_id is not None:
        status_text += " — ready to publish."
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": status_text}],
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Publish now"},
                        "style": "primary",
                        "value": f"publish_now_{item_id}",
                        "action_id": "publish_now",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Schedule"},
                        "value": f"publish_schedule_{item_id}",
                        "action_id": "publish_schedule",
                    },
                    _delete_button(item_id),
                ],
            }
        )
    else:
        if status == "approved":
            status_text += " — ready for manual posting."
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": status_text}],
            }
        )
    return blocks


def format_schedule_picker_blocks(item_id, original_blocks):
    """Replace the publish actions block with a datetimepicker + confirm button
    so the user can pick when Meta should publish the post."""
    blocks = [b for b in (original_blocks or []) if b.get("type") != "actions"]
    blocks.append(
        {
            "type": "section",
            "block_id": "schedule_picker_hint",
            "text": {
                "type": "mrkdwn",
                "text": "Pick a time to schedule this post (10 min – 30 days out).",
            },
        }
    )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "datetimepicker",
                    "action_id": f"publish_dt_{item_id}",
                    "initial_date_time": _default_schedule_ts(),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Confirm schedule"},
                    "style": "primary",
                    "value": f"publish_confirm_{item_id}",
                    "action_id": "publish_confirm",
                },
            ],
        }
    )
    return blocks


def _default_schedule_ts():
    """Default the datetimepicker to ~1 hour from now (unix seconds, int)."""
    import time as _time

    return int(_time.time()) + 3600


def format_publish_result_blocks(original_blocks, status_text):
    """Replace the publish actions block with a final status line. Also strips
    the schedule-picker hint section if present (left over from the picker)."""
    blocks = [
        b
        for b in (original_blocks or [])
        if b.get("type") != "actions" and b.get("block_id") != "schedule_picker_hint"
    ]
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": status_text}],
        }
    )
    return blocks



def format_queue_blocks(items):
    """Summary table of approved/scheduled posts with Open + Refresh buttons."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Approved posts queue ({len(items)})",
            },
        }
    ]
    if not items:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No actionable posts. Approve a draft first._",
                },
            }
        )
        return blocks

    for it in items:
        first_line = ((it.get("draft_text") or "").splitlines() or [""])[0]
        preview = first_line[:80] + ("…" if len(first_line) > 80 else "")
        name = it.get("display_name") or it.get("brand") or "?"
        img = (
            ":white_check_mark: image"
            if it.get("image_url")
            else ":camera: no image"
        )
        sched = ""
        if it.get("scheduled_for"):
            sf = it["scheduled_for"]
            try:
                sched = f":calendar: {sf:%Y-%m-%d %H:%M UTC}"
            except Exception:
                sched = f":calendar: {sf}"
        status_line = f"{it.get('status', '?')} {sched}".strip()
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*#{it['id']}* — {name} • {it.get('platform', '?')}\n"
                        f"{preview or '_empty draft_'}"
                    ),
                },
                "fields": [
                    {"type": "mrkdwn", "text": f"*Status:*\n{status_line}"},
                    {"type": "mrkdwn", "text": f"*Image:*\n{img}"},
                ],
                "accessory": {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Open"},
                    "value": f"queue_open_{it['id']}",
                    "action_id": "queue_open",
                },
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Refresh queue"},
                    "value": "queue_refresh",
                    "action_id": "queue_refresh",
                }
            ],
        }
    )
    return blocks


def format_approved_action_blocks(item):
    """Re-render a single approved/scheduled post with action buttons.

    Reuses existing action_ids (gen_image, publish_now, publish_schedule) so
    the interaction handlers work unchanged.
    """
    brand_name = item.get("display_name") or item.get("brand") or "?"
    platform = item.get("platform") or "?"
    draft = item.get("draft_text") or ""
    # Slack rejects section text over 3,000 chars; leave headroom for the
    # brand/platform header line.
    if len(draft) > 2900:
        draft = draft[:2900].rstrip() + "…"
    item_id = item["id"]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{brand_name} - {platform}*\n{draft}",
            },
        }
    ]
    if item.get("image_url"):
        blocks.append(
            {
                "type": "image",
                "image_url": item["image_url"],
                "alt_text": f"Generated image for {brand_name} {platform} post",
            }
        )
    actions = []
    if item.get("image_url"):
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Regenerate image"},
                "value": f"gen_image_{item_id}",
                "action_id": "gen_image",
            }
        )
    else:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Generate image"},
                "value": f"gen_image_{item_id}",
                "action_id": "gen_image",
            }
        )
    if platform == "facebook" and item.get("status") == "approved":
        actions.append(
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "Publish now"},
                "value": f"publish_now_{item_id}",
                "action_id": "publish_now",
            }
        )
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Schedule"},
                "value": f"publish_schedule_{item_id}",
                "action_id": "publish_schedule",
            }
        )
    elif item.get("status") == "scheduled" and item.get("scheduled_for"):
        sf = item["scheduled_for"]
        try:
            when = f"{sf:%Y-%m-%d %H:%M UTC}"
        except Exception:
            when = str(sf)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":calendar: Already scheduled for {when}",
                    }
                ],
            }
        )
    actions.append(_delete_button(item_id))
    blocks.append({"type": "actions", "elements": actions})
    return blocks



def post_for_approval(item_id, brand_name, platform, draft_text, image_url=None):
    """Post a draft to Slack with Approve/Reject buttons. Returns the message ts.

    If `image_url` is set (V1.6 image generation), an image block is appended
    above the action buttons. Otherwise the draft posts as text-only with a
    "Generate image" button alongside Approve/Reject so the human can trigger
    one-off Claude design-system image generation on demand.
    """
    bot_token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CONTENT_CHANNEL"]

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{brand_name} - {platform}*\n{draft_text}",
            },
        },
    ]

    if image_url:
        blocks.append(
            {
                "type": "image",
                "image_url": image_url,
                "alt_text": f"Generated image for {brand_name} {platform} post",
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "value": f"approve_{item_id}",
                        "action_id": "approve",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "value": f"reject_{item_id}",
                        "action_id": "reject",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Regenerate image"},
                        "value": f"gen_image_{item_id}",
                        "action_id": "gen_image",
                    },
                ],
            }
        )
    else:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "value": f"approve_{item_id}",
                        "action_id": "approve",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "value": f"reject_{item_id}",
                        "action_id": "reject",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Generate image"},
                        "value": f"gen_image_{item_id}",
                        "action_id": "gen_image",
                    },
                ],
            }
        )

    resp = requests.post(
        SLACK_POST_URL,
        headers={"Authorization": f"Bearer {bot_token}"},
        json={
            "channel": channel,
            "blocks": blocks,
            "text": f"New draft for {brand_name} on {platform}",
        },
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack post_for_approval failed: {data}")
    return data["ts"]


def format_draft_with_image_blocks(original_blocks, draft_text, brand_name,
                                   platform, item_id, image_url,
                                   with_regenerate=True):
    """Rebuild a draft message to show the generated image inline.

    Used after the "Generate image" button runs the Claude slot-fill pipeline
    and saves the PNG.
    Replaces whatever was in `original_blocks` with: text section + image
    block + action row (Approve/Reject + Regenerate image if with_regenerate).
    """
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{brand_name} - {platform}*\n{draft_text}",
            },
        },
        {
            "type": "image",
            "image_url": image_url,
            "alt_text": f"Generated image for {brand_name} {platform} post",
        },
    ]
    action_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve"},
            "style": "primary",
            "value": f"approve_{item_id}",
            "action_id": "approve",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reject"},
            "style": "danger",
            "value": f"reject_{item_id}",
            "action_id": "reject",
        },
    ]
    if with_regenerate:
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Regenerate image"},
                "value": f"gen_image_{item_id}",
                "action_id": "gen_image",
            }
        )
    blocks.append({"type": "actions", "elements": action_elements})
    return blocks


def post_message(channel, text=None, blocks=None, thread_ts=None):
    """Post a generic message (optionally threaded) and return its ts."""
    bot_token = os.environ["SLACK_BOT_TOKEN"]
    payload = {"channel": channel}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts

    resp = requests.post(
        SLACK_POST_URL,
        headers={"Authorization": f"Bearer {bot_token}"},
        json=payload,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack post_message failed: {data}")
    return data["ts"]


def update_message(channel, ts, text=None, blocks=None):
    """Update an existing message in place. Used by background tasks that
    follow up an immediate `replace_original` ack with the final result."""
    bot_token = os.environ["SLACK_BOT_TOKEN"]
    payload = {"channel": channel, "ts": ts}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks

    resp = requests.post(
        SLACK_UPDATE_URL,
        headers={"Authorization": f"Bearer {bot_token}"},
        json=payload,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack update_message failed: {data}")
    return data.get("ts")
