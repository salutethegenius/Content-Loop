import os
import re
import sys

import anthropic

from core.brand_loader import load_voice

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

PLATFORM_OFFSETS = {
    "facebook": 0,
    "instagram": 1,
    "linkedin": 2,
}

# Facebook (and IG captions) render markdown/list markers as literal
# dashes. Always write short paragraphs, never lists. This block is
# written as sentences on purpose so the model does not copy a list.
FORMAT_RULES = (
    "Formatting (required): Write in short paragraphs or sentences only. "
    "Never use bullet points, numbered lists, markdown, or list markers "
    "(dash, asterisk, dot, 1.). They do not render on Facebook. "
    "No headings, no bold or italic markup. "
    "Vary the opening line. Do not start with a slogan or 'At {brand}, we'. "
    "Never refuse to write the post or ask anyone for missing facts or numbers. "
    "If a figure is not in the voice rules, write around it. Do not stall."
)

PLATFORM_FORMAT_HINTS = {
    "facebook": (
        "Facebook: conversational, community-oriented tone. "
        "Short paragraphs only. No bullets. "
        "Use 2 to 4 hashtags at the end unless voice rules say otherwise."
    ),
    "instagram": (
        "Instagram: punchy opening line, line breaks for readability, "
        "hashtags and emoji per voice rules. No bullet lists."
    ),
    "linkedin": (
        "LinkedIn: professional tone, short paragraphs, "
        "minimal hashtags unless voice rules say otherwise. No bullet lists."
    ),
}

RECENT_DRAFT_LIMIT = 6
RECENT_DRAFT_CHARS = 280
_LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


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


def pick_pillar(brand_config, platform=None, draft_count=0):
    """Pick a content pillar for this run.

    Rotates by how many drafts already exist for this brand/platform so
    each generation advances, including multiple runs on the same day.
    A platform offset keeps same-run Facebook/Instagram/LinkedIn on
    different pillars.
    """
    pillars = _normalize_pillars(brand_config.get("content_pillars"))
    if not pillars:
        return None
    offset = PLATFORM_OFFSETS.get(platform, 0)
    idx = (int(draft_count or 0) + offset) % len(pillars)
    return pillars[idx]


def _load_history(brand_id, platform):
    """Return (recent_draft_texts, draft_count). Empty on any DB issue."""
    try:
        from core import db as _db
        recent = _db.list_recent_drafts(
            brand_id, platform, limit=RECENT_DRAFT_LIMIT
        )
        count = _db.count_drafts(brand_id, platform)
        return recent, count
    except Exception:
        return [], 0


def _history_block(recent_drafts):
    if not recent_drafts:
        return ""
    lines = []
    for i, text in enumerate(recent_drafts, 1):
        clipped = " ".join((text or "").split())
        if len(clipped) > RECENT_DRAFT_CHARS:
            clipped = clipped[: RECENT_DRAFT_CHARS - 1].rstrip() + "…"
        lines.append(f"{i}. {clipped}")
    joined = "\n".join(lines)
    return (
        "\n\nRECENT POSTS for this brand and platform. Do not repeat their "
        "wording, structure, opening lines, or the same specific angle. "
        "Write something that would not be mistaken for any of these:\n"
        f"{joined}"
    )


def _strip_list_markers(text):
    """Remove bullet/number prefixes so Facebook does not show raw dashes."""
    cleaned = [_LIST_LINE.sub("", line).rstrip() for line in (text or "").splitlines()]
    return "\n".join(cleaned).strip()


def generate_draft(brand_config, platform, topic=None):
    """Generate text-only post copy for a single brand/platform pair.

    `topic` is an optional operator-supplied brief. When set, the draft
    is about that topic (still in-voice). When unset, a content pillar is
    picked from memory, rotating past recent drafts so wording does not
    stall on the same savings-style post.
    """
    voice = load_voice(brand_config["brand_id"])
    format_hint = PLATFORM_FORMAT_HINTS.get(platform, "")
    display_name = brand_config.get("display_name") or brand_config["brand_id"]
    recent, draft_count = _load_history(brand_config["brand_id"], platform)
    topic = (topic or "").strip() or None

    system_prompt = (
        f"{voice}\n\n"
        f"You are drafting a {platform} post for {display_name}.\n"
    )
    if format_hint:
        system_prompt += f"{format_hint}\n"
    system_prompt += (
        f"{FORMAT_RULES.replace('{brand}', display_name)}\n"
        "Follow the voice rules above exactly. No em-dashes.\n"
        "Return only the post copy, nothing else. No preamble, no quotes."
    )

    print(
        f"[generator] brand={brand_config.get('brand_id')} platform={platform} "
        f"drafts={draft_count} recent={len(recent)} "
        f"topic={'operator' if topic else 'pillar'}",
        file=sys.stderr,
    )

    if topic:
        user_content = (
            f"Draft a post about this specific topic, provided by the brand owner:\n"
            f"\"\"\"\n{topic}\n\"\"\"\n\n"
            "Stay strictly within the brand's voice and content territory. "
            "Do not ask for clarification. Do not invent products, rates, "
            "promotions, or partnerships that are not in the voice rules. "
            "Return only the post copy."
        )
    else:
        pillar = pick_pillar(brand_config, platform, draft_count=draft_count)
        if pillar:
            print(
                f"[generator] pillar={pillar.get('pillar')}",
                file=sys.stderr,
            )
            topic_line = f"Draft a post about: {pillar['pillar']}."
            if pillar.get("description"):
                topic_line += f"\nDirection: {pillar['description']}"
            topic_line += (
                "\n\nPick a fresh, specific angle inside this pillar. "
                "Do not default to generic savings advice unless this pillar "
                "is specifically about saving and none of the recent posts "
                "already covered it.\n"
                "Stay strictly within the brand's voice and content territory above. "
                "Do not ask for clarification. Do not invent products, rates, promotions, "
                "or partnerships that are not in the voice rules. Return only the post copy."
            )
            user_content = topic_line
        else:
            user_content = (
                "Draft the next post. Choose a topic that fits this brand's stance and "
                "voice territory. Do not ask for a brief or clarification. Do not invent "
                "products, rates, promotions, or partnerships. Do not default to generic "
                "savings advice if another in-voice topic is available. "
                "Return only the post copy."
            )

    user_content += _history_block(recent)

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY read from env at call time
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    return _strip_list_markers(response.content[0].text.strip())
