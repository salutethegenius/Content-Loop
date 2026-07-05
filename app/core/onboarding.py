"""Nova onboarding engine.

Walks a business through a Slack thread interview in six phases, then
synthesizes a voice.md + config.json via Claude and posts it back with
Approve/Edit/Reject buttons. On Approve, the brand is persisted to the
`brands` table so the content loop can use it immediately.
"""

import os

import anthropic

from core import db

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
        "Phase 5 - Platform behavior (Instagram vs LinkedIn)",
        [
            "How should your voice differ between Instagram and LinkedIn, if at all? Same tone different length, or different tone entirely?",
            "Instagram: preferred post length, hashtag count, emoji policy, any visual conventions.",
            "LinkedIn: preferred post length, paragraph style, links in body or in comments, hashtag policy.",
            "Are there other platforms you want to add in V2? Threads, X, Facebook, TikTok. Flag for later.",
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
            "Image style in one sentence, for when V2 image generation ships. (e.g. 'sovereign navy and gold, institutional, clean'.)",
            "Anything else Nova should know that has not come up yet?",
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
        "- `## Formatting` (per-platform: Instagram and LinkedIn rules)\n"
        "- `## Content pillars` (the 4-8 themes, one line each)\n\n"
        "config.json contract (all fields required):\n"
        "- brand_id: lowercase slug, must match the input\n"
        "- display_name: human-readable name\n"
        "- active: true\n"
        "- platforms: array, subset of ['instagram','linkedin']\n"
        "- posting_cadence_days: integer\n"
        "- image_style_prompt: one-sentence visual direction\n"
        "- content_pillars: array of {pillar, description} objects derived from the answers\n\n"
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
