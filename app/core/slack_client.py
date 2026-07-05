import os

import requests

SLACK_POST_URL = "https://slack.com/api/chat.postMessage"


def post_for_approval(item_id, brand_name, platform, draft_text):
    """Post a draft to Slack with Approve/Reject buttons. Returns the message ts."""
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
            ],
        },
    ]

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
