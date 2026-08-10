"""Nova onboarding engine.

Walks a business through a Slack thread interview in seven phases, then
synthesizes a voice.md + config.json via Claude and posts it back with
Approve/Reject/Regenerate buttons. On Approve, the brand is persisted to
the `brands` table so the content loop can use it immediately.
"""

import os

import anthropic

from core import db
from core.slack_client import post_message

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Ordered list of (phase_key, phase_title, questions_list).
# Each phase is one Slack message from Nova. The human replies in the
# thread (one or several messages), then types `next` to advance.
PHASES = [
    (
        "identity",
        "Phase 1 - Identity (who you are)",
        [
            "What is the full legal name of the business, and what do you want to be called in posts?",
            "In one or two sentences, what does the business do? No marketing language, just the facts.",
            "Where are you based, and who is your primary customer? (City/country, demographics, life stage.)",
            "What is the one thing you want people to feel when they interact with your brand? One word or one sentence.",
            "What makes you different from the next business doing something similar? Be specific, not 'we care more'.",
            "Are there any sister brands, parent companies, or sub-brands we should reference or avoid referencing?",
        ],
    ),
    (
        "voice",
        "Phase 2 - Voice and tone (how you sound)",
        [
            "If your brand were a person at a dinner table, how would they speak? Calm and measured? Warm and chatty? Direct and dry?",
            "On a scale of 1 to 5, how formal are you? (1 = casual, 5 = institutional.)",
            "Name three brands (any industry) whose tone you respect. What do you like about each?",
            "Are there words or phrases you never want to see in a post? Hype words, jargon, anything off-brand.",
            "Do you use emojis? If yes, how many and where? (Never, only at the end of Instagram, sparingly, freely.)",
            "Do you use exclamation marks? Hashtags? If hashtags, how many and on which platforms?",
            "Do you use em-dashes? Default for this system is no, confirm or override.",
            "Are rhetorical questions allowed, or do you find them manipulative?",
            "Is humor allowed? If yes, what kind (dry, warm, observational) and what is off-limits?",
            "How do you refer to your customer in posts? 'You', 'members', 'riders', 'clients', 'our community'?",
        ],
    ),
    (
        "content",
        "Phase 3 - Content territory (what you post about)",
        [
            "What are the 4 to 8 recurring themes or content pillars you want to post about? (e.g. stewardship, member stories, behind-the-scenes, financial literacy, driver spotlights, community moments, product announcements, industry POV.)",
            "For each pillar, give one example post idea you would be proud to publish.",
            "Are there topics you never want to touch? Politics, competitors, specific events, certain words.",
            "What is the most valuable thing a follower could take away from your posts? A feeling, a fact, a habit, an action.",
            "Do you have any recurring series or campaigns we should plan around? (e.g. 'Member Monday', 'Stewardship Series'.)",
            "How much should Nova invent versus how much will you feed her? Fully autonomous within pillars, or you supply a brief each cycle?",
        ],
    ),
    (
        "compliance",
        "Phase 4 - Constraints and compliance (what you cannot say)",
        [
            "Is there a compliance review step before a post goes live, or is Slack approval the only gate?",
            "Are there legal disclaimers that must appear on certain post types? (e.g. 'Insured by...', 'Not financial advice', 'Terms apply'.)",
            "Are there products, services, or promotions you cannot mention on social at all?",
            "Can you name partners, sponsors, or third parties? What permission is required?",
            "Are there dates or windows where you go dark? Earnings periods, regulatory reviews, sensitivities.",
            "What should Nova do if she is unsure whether something is allowed? Draft it anyway and flag, or skip?",
        ],
    ),
    (
        "platform",
        "Phase 5 - Platform behavior (Facebook, Instagram, optional LinkedIn)",
        [
            "Facebook and Instagram are our primary platforms. How should your voice differ between them, if at all?",
            "Facebook: preferred post length, hashtag count (typically 2 to 4), emoji policy.",
            "Instagram: preferred post length, hashtag count, emoji policy, any visual conventions.",
            "Do you want LinkedIn as an optional third platform? If yes, how should LinkedIn differ from Facebook and Instagram?",
            "Are there other platforms you want to add later? Threads, X, TikTok. Flag for later.",
        ],
    ),
    (
        "cadence",
        "Phase 6 - Cadence and logistics",
        [
            "How often do you want to post, per platform? (e.g. Instagram every 3 days, LinkedIn weekly.)",
            "Is there a day of week or time of day you prefer, or is cadence enough?",
            "Who approves drafts in Slack? Just you, or multiple people?",
            "What channel should drafts land in? Channel name or ID. Nova will use the ID.",
            "Image style in one sentence, used to steer generated images. (e.g. 'sovereign navy and gold, institutional, clean'.)",
            "Anything else Nova should know that has not come up yet?",
        ],
    ),
    (
        "visual",
        "Phase 7 - Visual identity (for image generation)",
        [
            "What are your brand colors? Give 2 to 5 hex codes (e.g. #003366, #FFFFFF, #F58220), in priority order.",
            "What typography style fits the brand? (e.g. 'geometric sans-serif, bold weight, all-caps headlines' or 'serif, editorial, mixed case'.)",
            "How should the wordmark or logo appear on generated images? (e.g. 'BICCU' bottom-right, or 'no wordmark, just typography'.)",
            "What is the layout/feel for image posts? (e.g. 'centered bold headline on solid brand-color background' or 'split layout, text left, accent color right'.)",
            "Anything Nova should avoid in generated images? (e.g. 'no stock photos, no clipart, no gradients, no people'.)",
            "Footer data for generated images: website URL, phone number, tagline (one short line), and a primary hashtag. Also list social handles for facebook, instagram, linkedin if you have them. Format: website=..., phone=..., tagline=..., hashtag=..., facebook=..., instagram=..., linkedin=...",
        ],
    ),
]

PHASE_KEYS = [p[0] for p in PHASES]
NEXT_KEYWORDS = {"next", "/next", "next.", "next!"}


def get_phase(phase_key):
    for key, title, questions in PHASES:
        if key == phase_key:
            return title, questions
    return None, None


def next_phase(current_key):
    try:
        idx = PHASE_KEYS.index(current_key)
    except ValueError:
        return None
    if idx + 1 >= len(PHASES):
        return None
    return PHASE_KEYS[idx + 1]


def is_next_command(text):
    return text.strip().lower() in NEXT_KEYWORDS


def format_phase_message(display_name, phase_key):
    title, questions = get_phase(phase_key)
    if not questions:
        return None
    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    return (
        f"*Nova onboarding for {display_name}*\n"
        f"*{title}*\n\n"
        f"{numbered}\n\n"
        f"_Reply with your answers in this thread. When you are done, type `next` to move on._"
    )


def start_session(brand_id, display_name, channel):
    """Open a Nova onboarding thread in `channel` for a new brand.

    Posts the welcome message as a top-level message in the channel, posts
    phase 1 as the first threaded reply, and persists the session row so
    Slack Events replies in that thread drive the conversation forward.

    Returns the new thread_ts. Raises if the Slack post fails
    (post_message raises on any non-ok Slack response).
    """
    welcome = (
        f"*Nova onboarding for {display_name}*\n"
        f"Hi! I'm Nova. I'll ask a few batches of questions to learn your brand's "
        f"voice and content territory. Reply in this thread, and type `next` when "
        f"you are ready to move on. At the end I'll draft your voice.md and "
        f"config.json for approval."
    )
    thread_ts = post_message(channel, text=welcome)
    phase_msg = format_phase_message(display_name, "identity")
    post_message(channel, text=phase_msg, thread_ts=thread_ts)
    db.create_onboarding_session(brand_id, display_name, channel, thread_ts)
    return thread_ts


def format_draft_message(brand_id, display_name, voice_md, config):
    """Post the synthesized voice.md + config.json with Approve/Reject/Regenerate."""
    import json as _json

    config_str = _json.dumps(config, indent=2, ensure_ascii=False)
    # Slack mrkdwn has a 3000-char limit per text block; split if needed.
    voice_preview = voice_md if len(voice_md) <= 2500 else voice_md[:2500] + "\n... (truncated)"
    config_preview = config_str if len(config_str) <= 2500 else config_str[:2500] + "\n... (truncated)"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Draft voice.md and config.json for {display_name}*\nReview below. Approve to make this brand live.",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*voice.md:*\n```\n{voice_preview}\n```"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*config.json:*\n```\n{config_preview}\n```"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "value": brand_id,
                    "action_id": "onboard_approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "value": brand_id,
                    "action_id": "onboard_reject",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Regenerate"},
                    "value": brand_id,
                    "action_id": "onboard_regenerate",
                },
            ],
        },
    ]
    return blocks


def synthesize_brand(brand_id, display_name, answers):
    """Call Claude with all phase answers and produce voice.md + config.json.

    Returns (voice_md: str, config: dict).
    """
    import json

    client = anthropic.Anthropic()

    answers_block = ""
    for phase_key, title, _questions in PHASES:
        msgs = answers.get(phase_key, [])
        if not msgs:
            continue
        joined = "\n".join(f"- {m}" for m in msgs)
        answers_block += f"\n\n## {title}\n{joined}"

    # Corrections given after a draft was rejected. These carry the
    # operator's latest intent, so they override earlier answers.
    revision_msgs = answers.get("revision_feedback", [])
    if revision_msgs:
        joined = "\n".join(f"- {m}" for m in revision_msgs)
        answers_block += (
            "\n\n## Revision feedback (given after reviewing a draft; "
            "OVERRIDES any conflicting answers above)\n" + joined
        )

    system_prompt = (
        "You are Nova, a brand voice architect. You take a business's onboarding "
        "interview answers and produce two artifacts: a voice.md system prompt and "
        "a config.json brand config. You return STRICT JSON only, no prose, with "
        "two keys: `voice_md` (a string, the full markdown system prompt) and "
        "`config_json` (an object matching the brand config contract).\n\n"
        "voice.md structure (markdown):\n"
        "- A `# {Brand} Voice` header\n"
        "- A tone paragraph\n"
        "- `## Do` (bulleted)\n"
        "- `## Don't` (bulleted, include banned words)\n"
        "- `## Formatting` (per-platform: Facebook, Instagram, and LinkedIn if requested)\n"
        "- `## Content pillars` (the 4-8 themes, one line each)\n\n"
        "config.json contract (all fields required):\n"
        "- brand_id: lowercase slug, must match the input\n"
        "- display_name: human-readable name\n"
        "- active: true\n"
        "- platforms: array, subset of ['facebook','instagram','linkedin']. "
        "Default to ['facebook','instagram'] unless the answers explicitly request LinkedIn.\n"
        "- posting_cadence_days: integer\n"
        "- image_style_prompt: one-sentence visual direction\n"
        "- content_pillars: array of {pillar, description} objects derived from the answers\n"
        "- visual_identity: object with the following keys (infer sensibly from the "
        "Phase 7 answers; never invent hex codes the user did not provide, fall back "
        "to neutral defaults like ['#000000','#FFFFFF'] only if the brand gave nothing):\n"
        "    * colors: array of hex strings, 2-5 entries, in priority order\n"
        "    * typography_style: short descriptive string\n"
        "    * wordmark_text: the wordmark/logo text to render on images (or empty string)\n"
        "    * layout: short descriptive string for the image layout/feel\n"
        "    * avoid: array of strings, things to never include in generated images\n"
        "- footer: object with website, phone, tagline, hashtag, and social "
        "(sub-object with facebook, instagram, linkedin handles). Derive from the "
        "Phase 7 footer-data answer. Use empty strings for anything not provided.\n"
        "- design: object with template (default 'template.svg'), illustrations_dir "
        "(default 'illustrations'), default_illustration (default 'piggy_bank'). "
        "Use the defaults unless the brand specifically requests otherwise.\n\n"
        "Rules: no em-dashes anywhere. Follow the brand's own voice rules in the output. "
        "If the answers are silent on a field, infer sensibly from the brand's industry "
        "and tone. Never invent rates, products, or promotions."
    )

    user_prompt = (
        f"Brand ID: {brand_id}\n"
        f"Display name: {display_name}\n\n"
        f"Onboarding answers:{answers_block}\n\n"
        "Produce the voice.md and config.json now. Return ONLY the JSON object."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # Tolerate ```json fences.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    parsed = json.loads(raw)
    voice_md = parsed["voice_md"]
    config = parsed["config_json"]
    # Force the brand_id to match the session to prevent drift.
    config["brand_id"] = brand_id
    config.setdefault("active", True)
    return voice_md, config
