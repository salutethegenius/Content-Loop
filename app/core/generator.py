import os
from datetime import date

import anthropic

from core.brand_loader import load_voice

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

PLATFORM_OFFSETS = {
    "facebook": 0,
    "instagram": 1,
    "linkedin": 2,
}

PLATFORM_FORMAT_HINTS = {
    "facebook": (
        "Facebook: conversational, community-oriented tone. "
        "Use 2 to 4 hashtags at the end unless voice rules say otherwise."
    ),
    "instagram": (
        "Instagram: punchy opening line, line breaks for readability, "
        "hashtags and emoji per voice rules."
    ),
    "linkedin": (
        "LinkedIn: professional tone, short paragraphs, "
        "minimal hashtags unless voice rules say otherwise."
    ),
}


def _normalize_pillars(raw):
    """Accept either ["name", ...] or [{"pillar":"name","description":"..."}, ...]."""
    if not raw:
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"pillar": item, "description": ""})
        elif isinstance(item, dict) and item.get("pillar"):
            out.append({
                "pillar": item["pillar"],
                "description": item.get("description", ""),
            })
    return out


def pick_pillar(brand_config, platform=None):
    """Pick a content pillar for this run.

    Date-based rotation: deterministic, no DB state needed. The pillar index
    advances each day, so over time the brand cycles through all its pillars.
    If a platform is provided, it offsets the index so same-day drafts across
    platforms are not identical topics.
    """
    pillars = _normalize_pillars(brand_config.get("content_pillars"))
    if not pillars:
        return None
    base = date.toordinal(date.today())
    offset = PLATFORM_OFFSETS.get(platform, 0)
    idx = (base + offset) % len(pillars)
    return pillars[idx]


def generate_draft(brand_config, platform):
    """Generate text-only post copy for a single brand/platform pair.

    If the brand has content_pillars (captured during onboarding), the draft
    is anchored to a specific pillar so Claude never has to ask for a topic.
    Brands without pillars fall back to a strengthened prompt that instructs
    Claude to invent a topic within the brand's voice territory.
    """
    voice = load_voice(brand_config["brand_id"])
    format_hint = PLATFORM_FORMAT_HINTS.get(platform, "")

    system_prompt = (
        f"{voice}\n\n"
        f"You are drafting a {platform} post for {brand_config['display_name']}.\n"
    )
    if format_hint:
        system_prompt += f"{format_hint}\n"
    system_prompt += (
        "Follow the voice rules above exactly. No em-dashes.\n"
        "Return only the post copy, nothing else. No preamble, no quotes."
    )

    pillar = pick_pillar(brand_config, platform)
    if pillar:
        topic_line = f"Draft a post about: {pillar['pillar']}."
        if pillar.get("description"):
            topic_line += f"\nDirection: {pillar['description']}"
        topic_line += (
            "\n\nStay strictly within the brand's voice and content territory above. "
            "Do not ask for clarification. Do not invent products, rates, promotions, "
            "or partnerships that are not in the voice rules. Return only the post copy."
        )
        user_content = topic_line
    else:
        # Fallback for brands without onboarding-captured pillars.
        user_content = (
            "Draft the next post. Choose a topic that fits this brand's stance and "
            "voice territory. Do not ask for a brief or clarification. Do not invent "
            "products, rates, promotions, or partnerships. Return only the post copy."
        )

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY read from env at call time
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return response.content[0].text.strip()


def generate_image(brand_config, platform, draft_text):
    """Stub. V2 wires this via Auto mode across Grok, Gemini, ChatGPT."""
    return None
