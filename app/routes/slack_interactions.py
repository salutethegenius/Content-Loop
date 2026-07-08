import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from core import db, generation_flow, onboarding
from core.db import update_status
from core.slack_client import (
    format_approved_action_blocks,
    format_draft_with_image_blocks,
    format_publish_result_blocks,
    format_queue_blocks,
    format_resolved_approval_blocks,
    format_schedule_picker_blocks,
    post_message,
    update_message,
)

router = APIRouter()

REPLAY_TOLERANCE_SECONDS = 60 * 5


def _verify_slack_signature(timestamp: str, signature: str, body: bytes) -> bool:
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


@router.post("/slack/interactions")
async def handle_interaction(request: Request, background_tasks: BackgroundTasks):
    """Handle Approve/Reject button clicks from Slack.

    Action families:
    - Content approval: action_id `approve`/`reject`, value `{action}_{item_id}`.
    - Onboarding approval: action_id `onboard_approve`/`onboard_reject`/
      `onboard_regenerate`, value `brand_id`.
    - Interactive generation: `gen_pick_brand_*`, `gen_confirm_*`.
    """
    raw_body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(timestamp, signature, raw_body):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    parsed = parse_qs(raw_body.decode())
    payload_str = parsed.get("payload", [""])[0]
    if not payload_str:
        raise HTTPException(status_code=400, detail="Missing payload")

    payload = json.loads(payload_str)
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": True}

    action = actions[0]
    action_id = action.get("action_id")
    value = action.get("value", "")
    channel = payload.get("channel", {}).get("id")
    message = payload.get("message", {}) or {}
    thread_ts = message.get("thread_ts") or message.get("ts")

    # --- Content approval actions ---
    if action_id in ("approve", "reject"):
        try:
            item_id = int(value.split("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        status = "approved" if action_id == "approve" else "rejected"
        user_id = (payload.get("user") or {}).get("id")
        # Ack immediately (Slack needs a response within 3s). The DB update +
        # in-place message update happen in a background task so a slow DB
        # connection can't cause Slack to silently drop the replace_original.
        background_tasks.add_task(
            _handle_approval, item_id, status, user_id,
            channel, message.get("ts"), message.get("blocks"),
        )
        return {"ok": True}

    # --- Generate / regenerate image (V1.6) ---
    if action_id == "gen_image":
        try:
            item_id = int(value.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        # Ack immediately; the Claude slot-fill + rasterize call takes a few
        # seconds and Slack would otherwise drop the message update.
        # Background task shows "Generating..." then the final image-inlined message.
        background_tasks.add_task(
            _handle_generate_image, item_id, channel, message.get("ts"),
            message.get("blocks"),
        )
        return {"ok": True}

    # --- Publish actions (V2) ---
    if action_id == "publish_now":
        try:
            item_id = int(value.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        # Ack immediately; background task shows "Publishing..." then the
        # Graph API result. Pure background pattern avoids any race between
        # a replace_original response and the followup chat.update.
        background_tasks.add_task(
            _handle_publish_now, item_id, channel, message.get("ts"),
            message.get("blocks"),
        )
        return {"ok": True}

    if action_id == "publish_schedule":
        try:
            item_id = int(value.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        # Ack immediately; background task swaps in the datetimepicker via
        # chat.update so a slow response can't cause Slack to drop it.
        background_tasks.add_task(
            _handle_show_schedule_picker, item_id, channel, message.get("ts"),
            message.get("blocks"),
        )
        return {"ok": True}

    if action_id == "publish_confirm":
        try:
            item_id = int(value.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        # Prefer Slack state.values; if the picker was never touched, use a
        # fresh now+1h default (see _extract_datetimepicker_value docstring).
        selected_ts = _extract_datetimepicker_value(payload, item_id)
        if not selected_ts:
            background_tasks.add_task(
                _handle_schedule_error, channel, message.get("ts"),
                message.get("blocks"),
                ":x: No time selected. Pick a time below, then Confirm.",
                item_id,
            )
            return {"ok": True}
        background_tasks.add_task(
            _handle_publish_schedule, item_id, selected_ts,
            channel, message.get("ts"), message.get("blocks"),
        )
        return {"ok": True}

    # datetimepicker change events — Slack still requires a 200 ack
    if action_id and action_id.startswith("publish_dt_"):
        return {"ok": True}

    # --- Queue actions (approved-posts summary) ---
    if action_id == "queue_open":
        try:
            item_id = int(value.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            raise HTTPException(status_code=400, detail="Bad action value")
        background_tasks.add_task(
            _handle_queue_open, item_id, channel, message.get("ts"),
        )
        return {"ok": True}

    if action_id == "queue_refresh":
        background_tasks.add_task(
            _handle_queue_refresh, channel, message.get("ts"),
        )
        return {"ok": True}

    # --- Onboarding actions (defer slow work to background) ---
    if action_id in ("onboard_approve", "onboard_reject", "onboard_regenerate"):
        brand_id = value
        background_tasks.add_task(
            _handle_onboarding_action, action_id, brand_id, channel, thread_ts
        )
        return {"ok": True}

    # --- Interactive generation actions ---
    if action_id and action_id.startswith("gen_pick_brand_"):
        brand_id = value.strip().lower()
        background_tasks.add_task(
            generation_flow.handle_pick_brand, brand_id, channel, thread_ts
        )
        return {"ok": True}

    if action_id and action_id.startswith("gen_force_pick_"):
        brand_id = value.strip().lower()
        background_tasks.add_task(
            generation_flow.handle_force_pick_brand, brand_id, channel, thread_ts
        )
        return {"ok": True}

    if action_id and action_id.startswith("gen_onboard_"):
        brand_id = value.strip().lower()
        background_tasks.add_task(
            _handle_onboard_from_picker, brand_id, channel, thread_ts
        )
        return {"ok": True}

    if action_id and action_id.startswith("gen_confirm_"):
        brand_id, platforms = generation_flow.parse_confirm_value(value)
        background_tasks.add_task(
            generation_flow.handle_confirm, brand_id, platforms, channel, thread_ts
        )
        return {"ok": True}

    raise HTTPException(status_code=400, detail=f"Unknown action: {action_id}")


def _handle_onboarding_action(action_id, brand_id, channel, thread_ts):
    session = db.get_onboarding_session(brand_id)
    if not session:
        return

    if action_id == "onboard_approve":
        if not session.get("draft_voice_md") or not session.get("draft_config"):
            post_message(
                channel,
                text="No draft on file. Click Regenerate first.",
                thread_ts=thread_ts,
            )
            return
        db.upsert_brand(brand_id, session["draft_config"], session["draft_voice_md"])
        db.set_onboarding_status(brand_id, "approved")
        db.set_onboarding_phase(brand_id, "done")
        post_message(
            channel,
            text=(
                f"Approved. {session['display_name']} is now live in the content loop. "
                f"It will be picked up on the next cron run."
            ),
            thread_ts=thread_ts,
        )
        return

    if action_id == "onboard_reject":
        db.set_onboarding_status(brand_id, "rejected")
        post_message(
            channel,
            text="Rejected. Reply in this thread with what to change, then click Regenerate.",
            thread_ts=thread_ts,
        )
        return

    if action_id == "onboard_regenerate":
        try:
            # Pull any replies posted since the last draft (status was
            # awaiting_approval) into the answers before re-synthesizing.
            answers = session["answers"]
            voice_md, config = onboarding.synthesize_brand(
                brand_id, session["display_name"], answers
            )
            db.save_onboarding_draft(brand_id, voice_md, config)
            db.set_onboarding_status(brand_id, "in_progress")
            blocks = onboarding.format_draft_message(
                brand_id, session["display_name"], voice_md, config
            )
            post_message(
                channel,
                blocks=blocks,
                thread_ts=thread_ts,
                text=f"Regenerated draft for {session['display_name']}",
            )
        except Exception as exc:
            post_message(
                channel,
                text=f"Regeneration failed: {exc}",
                thread_ts=thread_ts,
            )


def _handle_generate_image(item_id, channel, message_ts, original_blocks):
    """Background task: generate an image for a draft via Claude slot-filling,
    persist it, and update the Slack message in place to show the image inline.

    Pulled by the 'Generate image' / 'Regenerate image' button on a draft
    message. Shows an intermediate 'Generating...' state, then the final
    image-inlined message with Approve/Reject + Regenerate image buttons.
    """
    from core import image_generator
    from core.brand_loader import get_brand_by_id

    item = db.get_content_item(item_id)
    if not item:
        try:
            update_message(
                channel, message_ts,
                blocks=format_publish_result_blocks(
                    original_blocks, ":x: Item not found."
                ),
            )
        except Exception:
            pass
        return

    # Show an intermediate state so the user sees feedback within a second.
    try:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, ":hourglass_flowing_sand: Generating image with Claude..."
            ),
        )
    except Exception:
        pass

    brand = get_brand_by_id(item["brand"])
    if not brand:
        try:
            update_message(
                channel, message_ts,
                blocks=format_publish_result_blocks(
                    original_blocks,
                    f":x: Brand `{item['brand']}` not found or inactive.",
                ),
            )
        except Exception:
            pass
        return

    try:
        image_url, prompt, model_used = image_generator.generate_and_save(
            brand, item["platform"], item["draft_text"], item_id,
        )
    except Exception as exc:
        try:
            update_message(
                channel, message_ts,
                blocks=format_publish_result_blocks(
                    original_blocks, f":x: Image generation failed: {exc}"
                ),
            )
        except Exception:
            pass
        return

    if not image_url:
        try:
            update_message(
                channel, message_ts,
                blocks=format_publish_result_blocks(
                    original_blocks,
                    ":x: Image generated but no public URL configured "
                    "(IMAGE_BASE_URL not set on the server).",
                ),
            )
        except Exception:
            pass
        return

    db.update_content_image(item_id, image_url, prompt, model_used)

    try:
        new_blocks = format_draft_with_image_blocks(
            original_blocks,
            item["draft_text"],
            brand.get("display_name") or item["brand"],
            item["platform"],
            item_id,
            image_url,
        )
        update_message(channel, message_ts, blocks=new_blocks)
    except Exception as exc:
        post_message(
            channel,
            text=f"(image generated and saved at {image_url}, but Slack message update failed: {exc})",
            thread_ts=message_ts,
        )


def _handle_onboard_from_picker(brand_id, channel, thread_ts):
    """Background task: start a Nova onboarding flow for a brand the user
    picked from the `/nova` brand picker but hasn't onboarded yet.

    Opens a fresh top-level welcome message in the same channel and runs
    the onboarding there (re-using the same code path as `POST /onboard/start`).
    Replies in the picker thread with a pointer so the user knows where to go.
    """
    from core import onboarding as _onboarding
    from core.brand_loader import get_brand_by_id

    brand = get_brand_by_id(brand_id)
    if not brand:
        post_message(
            channel,
            text=f"Brand `{brand_id}` not found or inactive.",
            thread_ts=thread_ts,
        )
        return

    display_name = brand.get("display_name") or brand_id
    try:
        new_thread_ts = _onboarding.start_session(brand_id, display_name, channel)
    except Exception as exc:
        post_message(
            channel,
            text=f"Could not start onboarding for {display_name}: {exc}",
            thread_ts=thread_ts,
        )
        return

    if not new_thread_ts:
        post_message(
            channel,
            text=f"Could not start onboarding for {display_name} (Slack post failed).",
            thread_ts=thread_ts,
        )
        return

    post_message(
        channel,
        text=(
            f"Started onboarding for {display_name} in a new thread above. "
            f"Reply there to answer Nova's questions."
        ),
        thread_ts=thread_ts,
    )


def _handle_approval(item_id, status, user_id, channel, message_ts,
                     original_blocks):
    """Background task: update content_items status and replace the Slack
    draft message in place with the resolved status line (+ Publish/Schedule
    buttons for approved facebook drafts)."""
    update_status(item_id, status)
    item = db.get_content_item(item_id)
    platform = item.get("platform") if item else None
    updated_blocks = format_resolved_approval_blocks(
        original_blocks, status, user_id,
        item_id=item_id, platform=platform,
    )
    try:
        update_message(channel, message_ts, blocks=updated_blocks)
    except Exception as exc:
        # Don't surface Slack failures to the user — the DB status is already
        # correct, which is the source of truth. Log via a thread reply.
        post_message(
            channel,
            text=f"(approval recorded in DB, but Slack message update failed: {exc})",
            thread_ts=message_ts,
        )


def _extract_datetimepicker_value(payload, item_id):
    """Read the selected unix timestamp from a datetimepicker.

    Prefer Slack state.values (set when the user interacts with the picker).
    If untouched, return a fresh now+1h default — do NOT reuse the stale
    initial_date_time baked into the message at picker-open time (that can
    fall inside Meta's 10-minute minimum after the message sits open).
    """
    import time as _time

    state = payload.get("state") or {}
    values = state.get("values") or {}
    target = f"publish_dt_{item_id}"
    for block_values in values.values():
        for action_id, action_state in block_values.items():
            if action_id == target:
                selected = (action_state or {}).get("selected_date_time")
                if selected:
                    return selected

    # Fresh default if the picker was never touched.
    return int(_time.time()) + 3600


def _handle_show_schedule_picker(item_id, channel, message_ts, original_blocks):
    """Background task: swap the draft message in place to show the
    datetimepicker + Confirm schedule button."""
    picker_blocks = format_schedule_picker_blocks(item_id, original_blocks)
    try:
        update_message(channel, message_ts, blocks=picker_blocks)
    except Exception as exc:
        post_message(
            channel,
            text=f"(could not show schedule picker: {exc})",
            thread_ts=message_ts,
        )


def _handle_schedule_error(channel, message_ts, original_blocks, status_text,
                            item_id=None):
    """Background task: show a schedule error, keeping Schedule recoverable.

    Prefer re-showing the picker (with a context error line) so the user can
    retry without hunting for the original Approve message.
    """
    if item_id is not None:
        blocks = format_schedule_picker_blocks(item_id, original_blocks)
        # Insert error context above the picker actions
        blocks.insert(
            -1,
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": status_text}],
            },
        )
    else:
        blocks = format_publish_result_blocks(original_blocks, status_text)
    try:
        update_message(channel, message_ts, blocks=blocks)
    except Exception:
        pass


def _handle_queue_open(item_id, channel, thread_ts):
    """Background task: open a queue row as a threaded full-draft reply."""
    item = db.get_content_item(item_id)
    if not item:
        post_message(
            channel,
            text=f"(item #{item_id} not found)",
            thread_ts=thread_ts,
        )
        return
    # Prefer friendly display_name from brands when available.
    try:
        from core.brand_loader import get_brand_by_id
        brand = get_brand_by_id(item.get("brand"))
        if brand:
            item = dict(item)
            item["display_name"] = brand.get("display_name") or item["brand"]
    except Exception:
        pass
    blocks = format_approved_action_blocks(item)
    try:
        post_message(
            channel,
            blocks=blocks,
            text=f"Post #{item_id} — {item.get('platform', '?')}",
            thread_ts=thread_ts,
        )
    except Exception as exc:
        post_message(
            channel,
            text=f"(could not open item #{item_id}: {exc})",
            thread_ts=thread_ts,
        )


def _handle_queue_refresh(channel, message_ts):
    """Background task: re-render the queue message in place."""
    try:
        items = db.list_actionable_items(limit=20)
        blocks = format_queue_blocks(items)
        update_message(channel, message_ts, blocks=blocks)
    except Exception as exc:
        post_message(
            channel,
            text=f"(could not refresh queue: {exc})",
            thread_ts=message_ts,
        )


def _handle_publish_now(item_id, channel, message_ts, original_blocks):
    """Background task: publish an approved facebook draft via the Meta Graph
    API, then flip the Slack message to a final status line."""
    from datetime import datetime, timezone

    from core import meta_publisher

    item = db.get_content_item(item_id)
    if not item:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, ":x: Item not found."
            ),
        )
        return
    if item["platform"] != "facebook":
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                ":x: V2 supports Facebook publishing only.",
            ),
        )
        return

    # Atomically claim the item (approved -> publishing) so a double-click or
    # duplicate Slack retry cannot both proceed to call the Meta Graph API
    # for the same item and create two live posts.
    if not db.claim_item_status(item_id, "publishing", expected_status="approved"):
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                f":x: Cannot publish — item status is '{item['status']}' "
                "(already published, scheduled, or being published).",
            ),
        )
        return

    # Show an intermediate "Publishing..." state so the user sees feedback.
    try:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, ":clock1: Publishing to Facebook..."
            ),
        )
    except Exception:
        pass

    try:
        meta_post_id = meta_publisher.publish_page_post(
            item["draft_text"], image_url=item.get("image_url"),
        )
    except Exception as exc:
        # Release the claim so the user can retry.
        db.update_status(item_id, "approved")
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, f":x: Meta publish failed: {exc}"
            ),
        )
        return

    db.update_status(
        item_id, "posted",
        meta_post_id=meta_post_id,
        posted_at=datetime.now(timezone.utc),
    )
    update_message(
        channel, message_ts,
        blocks=format_publish_result_blocks(
            original_blocks,
            f":rocket: Published to Facebook. Meta post id: `{meta_post_id}`",
        ),
    )


def _handle_publish_schedule(item_id, selected_ts, channel, message_ts,
                             original_blocks):
    """Background task: schedule an approved facebook draft via the Meta Graph
    API, then flip the Slack message to a final status line."""
    from datetime import datetime, timezone

    from core import meta_publisher

    item = db.get_content_item(item_id)
    if not item:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, ":x: Item not found."
            ),
        )
        return
    if item["platform"] != "facebook":
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                ":x: V2 supports Facebook scheduling only.",
            ),
        )
        return

    # Slack datetimepicker returns unix seconds (int). Meta wants unix seconds
    # for scheduled_publish_time, but our meta_publisher takes an ISO string.
    scheduled_dt = datetime.fromtimestamp(selected_ts, tz=timezone.utc)
    iso = scheduled_dt.isoformat()

    # Pre-flight Meta window (10 min – 30 days) so Slack can restore the picker
    # instead of a hard Meta error after a long-open picker. Pure validation,
    # no external calls yet, so no claim needed if this fails.
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    min_ts = now_ts + meta_publisher.MIN_SCHEDULE_OFFSET_SEC
    max_ts = now_ts + meta_publisher.MAX_SCHEDULE_OFFSET_SEC
    if selected_ts < min_ts or selected_ts > max_ts:
        _handle_schedule_error(
            channel, message_ts, original_blocks,
            ":warning: Pick a time between 10 minutes and 30 days from now, "
            "then Confirm again.",
            item_id=item_id,
        )
        return

    # Atomically claim the item (approved -> scheduling) so a double-click or
    # duplicate Slack retry cannot both proceed to call the Meta Graph API
    # for the same item and create two scheduled posts.
    if not db.claim_item_status(item_id, "scheduling", expected_status="approved"):
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                f":x: Cannot schedule — item status is '{item['status']}' "
                "(already published, scheduled, or being scheduled).",
            ),
        )
        return

    # Show an intermediate "Scheduling..." state so the user sees feedback.
    try:
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, ":clock1: Scheduling on Facebook..."
            ),
        )
    except Exception:
        pass

    try:
        meta_post_id = meta_publisher.schedule_page_post(
            item["draft_text"], iso, image_url=item.get("image_url"),
        )
    except meta_publisher.ScheduleVerificationFailed as exc:
        # Meta most likely DID create the scheduled post (this is a
        # read-after-write verification failure, not necessarily a real
        # failure). Persist the post id and mark it scheduled anyway so a
        # retry cannot create a duplicate; surface the caveat in Slack.
        db.update_status(
            item_id, "scheduled",
            meta_post_id=exc.post_id,
            scheduled_for=iso,
        )
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks,
                f":warning: Scheduled for {scheduled_dt.strftime('%Y-%m-%d %H:%M UTC')}, "
                f"but could not confirm it's visible in Meta Planner yet. "
                f"Meta post id: `{exc.post_id}`. Check the Planner manually; "
                "do not schedule this item again.",
            ),
        )
        return
    except Exception as exc:
        # Release the claim so the user can retry.
        db.update_status(item_id, "approved")
        update_message(
            channel, message_ts,
            blocks=format_publish_result_blocks(
                original_blocks, f":x: Meta schedule failed: {exc}"
            ),
        )
        return

    db.update_status(
        item_id, "scheduled",
        meta_post_id=meta_post_id,
        scheduled_for=iso,
    )
    update_message(
        channel, message_ts,
        blocks=format_publish_result_blocks(
            original_blocks,
            f":calendar: Scheduled for {scheduled_dt.strftime('%Y-%m-%d %H:%M UTC')}. "
            f"Meta post id: `{meta_post_id}`",
        ),
    )
