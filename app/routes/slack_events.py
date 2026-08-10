"""Slack Events API endpoint.

Handles url_verification (Slack's ownership check) and message events.
Message events drive the onboarding conversation: when a human replies in
an onboarding thread, we accumulate the reply and advance the phase when
they type `next`. After the final phase, we synthesize the brand's
voice.md + config.json via Claude and post it back for approval.

Slack requires a 200 response within 3 seconds, so synthesis (slow Claude
call) happens in a FastAPI BackgroundTask after we acknowledge.
"""

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core import db, onboarding
from core.slack_client import post_message
from core.slack_verify import verify_slack_signature

router = APIRouter()


@router.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(timestamp, signature, raw_body):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Slack retries events it thinks timed out (3s window). We ack fast and
    # process in the background, so a retry means the original delivery is
    # already being handled — processing it again would double-append answers
    # or double-advance the onboarding phase.
    if request.headers.get("X-Slack-Retry-Num"):
        return {"ok": True}

    try:
        payload = json.loads(raw_body.decode())
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Bad JSON")

    # Slack ownership handshake.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    if payload.get("type") != "event_callback":
        return {"ok": True}

    event = payload.get("event") or {}
    if event.get("type") != "message":
        return {"ok": True}

    # Ignore messages from bots (including our own) to avoid loops.
    if event.get("bot_id") or event.get("subtype"):
        return {"ok": True}

    thread_ts = event.get("thread_ts")
    text = event.get("text", "")
    channel = event.get("channel")

    # Only thread replies in onboarding threads matter.
    if not thread_ts:
        return {"ok": True}

    # `rejected` is accepted too: after a draft is rejected the operator
    # replies with corrections in the same thread, then clicks Regenerate.
    session = db.get_onboarding_session_by_thread(thread_ts)
    if not session or session.get("status") not in ("in_progress", "rejected"):
        return {"ok": True}

    # Acknowledge now, process in the background to stay under Slack's 3s limit.
    background_tasks.add_task(
        _process_onboarding_reply, session, text, channel, thread_ts
    )
    return {"ok": True}


def _process_onboarding_reply(session, text, channel, thread_ts):
    """Handle one human reply in an onboarding thread."""
    brand_id = session["brand_id"]
    phase = session["phase"]

    # After the draft is on the table, replies only matter as revision
    # feedback on a rejected draft; store them where Regenerate will find
    # them and confirm receipt so the operator knows it registered.
    if phase in ("awaiting_approval", "done"):
        if session.get("status") == "rejected":
            db.append_onboarding_answer(brand_id, "revision_feedback", text)
            post_message(
                channel,
                text="Noted. Add more if you like, then click *Regenerate* on the draft.",
                thread_ts=thread_ts,
            )
        return

    if onboarding.is_next_command(text):
        nxt = onboarding.next_phase(phase)
        if nxt is None:
            # Last phase finished -> synthesize the brand artifacts.
            try:
                answers = db.get_onboarding_session(brand_id)["answers"]
                voice_md, config = onboarding.synthesize_brand(
                    brand_id, session["display_name"], answers
                )
                db.save_onboarding_draft(brand_id, voice_md, config)
                blocks = onboarding.format_draft_message(
                    brand_id, session["display_name"], voice_md, config
                )
                post_message(channel, blocks=blocks, thread_ts=thread_ts,
                             text=f"Draft brand voice and config for {session['display_name']}")
            except Exception as exc:
                post_message(
                    channel,
                    text=f"Nova hit a problem while drafting: {exc}. Type `next` to retry.",
                    thread_ts=thread_ts,
                )
            return

        # Advance to the next phase.
        db.set_onboarding_phase(brand_id, nxt)
        phase_msg = onboarding.format_phase_message(session["display_name"], nxt)
        if phase_msg:
            post_message(channel, text=phase_msg, thread_ts=thread_ts)
        return

    # Otherwise, accumulate the reply under the current phase.
    db.append_onboarding_answer(brand_id, phase, text)
