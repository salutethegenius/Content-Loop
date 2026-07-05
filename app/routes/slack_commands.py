"""Slack slash command handler.

Configure in the Slack app (api.slack.com/apps → your app → Slash Commands):
  Command:    /nova
  Request URL: https://nova-production-14f6.up.railway.app/slack/commands
  Short desc:  Start a Nova content flow

Subcommands:
  /nova              -> post a brand picker to #nova-agent (same as /generate/start)
  /nova generate     -> same as above
  /nova help         -> ephemeral help text

The endpoint verifies the Slack HMAC signature (no X-Cron-Secret needed —
slash commands are Slack-signed). It responds within 3s with an ephemeral
confirmation, and posts the actual brand picker to SLACK_CONTENT_CHANNEL so
the rest of the flow (platform picker, drafts, approvals, publish buttons)
stays in the approval channel.
"""

import os
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.brand_loader import get_active_brands
from core.generation_flow import format_brand_picker_blocks
from core.slack_client import post_message
from core.slack_verify import verify_slack_signature

router = APIRouter()


@router.post("/slack/commands")
async def handle_slash_command(request: Request):
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(timestamp, signature, raw_body):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    parsed = parse_qs(raw_body.decode())
    command = (parsed.get("command", [""])[0] or "").strip()
    text = (parsed.get("text", [""])[0] or "").strip().lower()
    user_id = parsed.get("user_id", [""])[0] or "someone"
    user_name = parsed.get("user_name", [""])[0] or user_id

    if command and command != "/nova":
        return JSONResponse(
            content={
                "response_type": "ephemeral",
                "text": f"Unknown command `{command}`. Try `/nova` or `/nova help`.",
            }
        )

    # /nova help
    if text in ("help", "?", "-h"):
        return JSONResponse(
            content={
                "response_type": "ephemeral",
                "text": (
                    "*Nova commands*\n"
                    "• `/nova` — start a content generation flow (pick a brand, "
                    "pick platforms, Nova drafts posts for approval)\n"
                    "• `/nova help` — this message\n\n"
                    "Drafts appear in this channel with Approve / Reject buttons. "
                    "Approved Facebook drafts also get Publish now / Schedule buttons."
                ),
            }
        )

    # /nova or /nova generate -> post the brand picker to #nova-agent
    if text in ("", "generate", "gen", "start"):
        channel = os.environ.get("SLACK_CONTENT_CHANNEL", "")
        if not channel:
            return JSONResponse(
                content={
                    "response_type": "ephemeral",
                    "text": "SLACK_CONTENT_CHANNEL is not configured on the server.",
                }
            )
        brands = get_active_brands()
        if not brands:
            return JSONResponse(
                content={
                    "response_type": "ephemeral",
                    "text": "No active brands configured. Onboard one first via /onboard/start.",
                }
            )
        blocks = format_brand_picker_blocks(brands)
        try:
            post_message(
                channel,
                blocks=blocks,
                text=f"Who should I generate posts for? (started by <@{user_id}>)",
            )
        except Exception as exc:
            return JSONResponse(
                content={
                    "response_type": "ephemeral",
                    "text": f"Could not post the brand picker: {exc}",
                }
            )
        return JSONResponse(
            content={
                "response_type": "ephemeral",
                "text": (
                    f"Posted a brand picker to <#{channel}>. "
                    "Click a brand there to continue."
                ),
            }
        )

    # Unknown subcommand
    return JSONResponse(
        content={
            "response_type": "ephemeral",
            "text": (
                f"Did not understand `/nova {text}`. Try `/nova` to start a "
                "generation flow, or `/nova help`."
            ),
        }
    )
