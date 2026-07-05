"""Interactive Slack flow: pick brand, pick platforms, generate targeted drafts."""

from core import db
from core.brand_loader import get_brand_by_id
from core.content_loop import generate_for_platforms
from core.slack_client import post_message

PLATFORM_ORDER = ["facebook", "instagram", "linkedin"]
PRIMARY_PLATFORMS = ["facebook", "instagram"]


def ordered_platforms(platforms):
    """Return platforms in canonical order, filtered to those configured."""
    configured = set(platforms or [])
    return [p for p in PLATFORM_ORDER if p in configured]


def format_brand_picker_blocks(brands):
    """Slack blocks asking which brand to generate for."""
    elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": brand["display_name"]},
            "value": brand["brand_id"],
            "action_id": f"gen_pick_brand_{brand['brand_id']}",
        }
        for brand in brands
    ]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Nova content generation*\n"
                    "Who should I generate posts for? Pick a brand below."
                ),
            },
        },
        {"type": "actions", "elements": elements[:5]},
    ]
    if len(elements) > 5:
        blocks.append({"type": "actions", "elements": elements[5:10]})
    return blocks


def format_platform_picker_blocks(brand):
    """Slack blocks asking which platforms to generate for one brand."""
    brand_id = brand["brand_id"]
    display_name = brand["display_name"]
    configured = ordered_platforms(brand.get("platforms"))
    if not configured:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{display_name}* has no platforms configured. "
                        "Update the brand config or re-onboard."
                    ),
                },
            }
        ]

    elements = []
    primary = [p for p in PRIMARY_PLATFORMS if p in configured]
    if len(primary) == 2:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Facebook + Instagram"},
                "style": "primary",
                "value": f"{brand_id}:facebook,instagram",
                "action_id": f"gen_confirm_{brand_id}_fb_ig",
            }
        )

    for platform in configured:
        label = platform.replace("_", " ").title()
        if platform in PRIMARY_PLATFORMS and len(primary) == 2:
            label = f"{label} only"
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label},
                "value": f"{brand_id}:{platform}",
                "action_id": f"gen_confirm_{brand_id}_{platform}",
            }
        )

    if len(configured) > 1:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "All configured platforms"},
                "value": f"{brand_id}:{','.join(configured)}",
                "action_id": f"gen_confirm_{brand_id}_all",
            }
        )

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Generating for {display_name}*\n"
                    "Which platforms? Facebook and Instagram are primary; "
                    "LinkedIn is optional when configured."
                ),
            },
        },
    ]
    for i in range(0, len(elements), 5):
        blocks.append({"type": "actions", "elements": elements[i : i + 5]})
    return blocks


def format_onboard_prompt_blocks(brand):
    """Slack blocks prompting the user to onboard a not-yet-onboarded brand.

    Shown in place of the platform picker when a brand only exists as a
    filesystem seed (no row in the `brands` table). The user can either
    start onboarding right here, or force a one-off generation with the
    fallback prompt.
    """
    brand_id = brand["brand_id"]
    display_name = brand["display_name"]
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{display_name}* hasn't been onboarded yet.\n"
                    "Without onboarding, Nova has no voice.md or content "
                    "pillars for this brand, so drafts will be generic and "
                    "off-voice. Onboard now to set the voice, pillars, "
                    "compliance rules, and cadence."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Onboard now"},
                    "value": brand_id,
                    "action_id": f"gen_onboard_{brand_id}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Generate anyway"},
                    "value": brand_id,
                    "action_id": f"gen_force_pick_{brand_id}",
                },
            ],
        },
    ]


def parse_confirm_value(value):
    """Parse gen_confirm button value: brand_id:facebook,instagram."""
    brand_id, _, platforms_str = value.partition(":")
    platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
    return brand_id.strip(), platforms


def handle_pick_brand(brand_id, channel, thread_ts):
    """Post platform picker after a brand is selected, gated on onboarding.

    If the brand has not been onboarded (no row in `brands`), post an
    Onboard-now prompt instead of the platform picker. The user can still
    bypass via the "Generate anyway" button, which calls
    `handle_force_pick_brand`.
    """
    brand = get_brand_by_id(brand_id)
    if not brand:
        post_message(
            channel,
            text=f"Brand `{brand_id}` not found or inactive.",
            thread_ts=thread_ts,
        )
        return

    try:
        onboarded = db.is_onboarded(brand_id)
    except Exception:
        # DB unreachable: fall through to the platform picker so a DB
        # outage doesn't block generation entirely.
        onboarded = True

    if not onboarded:
        blocks = format_onboard_prompt_blocks(brand)
        post_message(
            channel,
            blocks=blocks,
            thread_ts=thread_ts,
            text=f"{brand['display_name']} needs onboarding first.",
        )
        return

    _post_platform_picker(brand, channel, thread_ts)


def handle_force_pick_brand(brand_id, channel, thread_ts):
    """Bypass the onboarding gate and post the platform picker directly.

    Triggered by the 'Generate anyway' button on the onboarding prompt.
    Keeps the previous capability (fallback-prompt drafts for seed brands)
    available behind an explicit confirmation.
    """
    brand = get_brand_by_id(brand_id)
    if not brand:
        post_message(
            channel,
            text=f"Brand `{brand_id}` not found or inactive.",
            thread_ts=thread_ts,
        )
        return
    _post_platform_picker(brand, channel, thread_ts)


def _post_platform_picker(brand, channel, thread_ts):
    blocks = format_platform_picker_blocks(brand)
    post_message(
        channel,
        blocks=blocks,
        thread_ts=thread_ts,
        text=f"Pick platforms for {brand['display_name']}",
    )


def handle_confirm(brand_id, platforms, channel, thread_ts):
    """Run targeted generation and reply in the Slack thread."""
    brand = get_brand_by_id(brand_id)
    if not brand:
        post_message(
            channel,
            text=f"Brand `{brand_id}` not found or inactive.",
            thread_ts=thread_ts,
        )
        return

    configured = set(brand.get("platforms") or [])
    valid = [p for p in platforms if p in configured]
    skipped = [p for p in platforms if p not in configured]

    if not valid:
        post_message(
            channel,
            text=(
                f"No valid platforms selected for {brand['display_name']}. "
                f"Configured: {', '.join(ordered_platforms(brand.get('platforms'))) or 'none'}."
            ),
            thread_ts=thread_ts,
        )
        return

    post_message(
        channel,
        text=f"Generating {', '.join(valid)} for {brand['display_name']}...",
        thread_ts=thread_ts,
    )

    results = generate_for_platforms(brand, valid)
    if not results:
        post_message(
            channel,
            text="Nothing was generated. Check logs for errors.",
            thread_ts=thread_ts,
        )
        return

    summary = ", ".join(f"{r['platform']} (item #{r['item_id']})" for r in results)
    extra = ""
    if skipped:
        extra = f" Skipped (not configured): {', '.join(skipped)}."
    post_message(
        channel,
        text=(
            f"Done. Drafts for {brand['display_name']}: {summary}. "
            f"They are in the approval channel for review.{extra}"
        ),
        thread_ts=thread_ts,
    )
