"""BICCU design-system image generation.

Instead of asking Claude to design a full SVG each time (inconsistent, mediocre
quality), we use a fixed design system: a hand-crafted SVG template + a curated
illustration library on disk. Claude's job is reduced to filling structured
slots (headline lines, supporting paragraph, info card, CTA, illustration id),
which it does reliably. Python composes the slots into the template, cairosvg
rasterizes to PNG, and we save to the Railway volume.

Pipeline:
  1. design_loader.load_template(brand_id, brand_config) -> template SVG str
  2. design_loader.list_illustrations(brand_id, brand_config) -> {id: svg_str}
  3. build_slot_prompt(...) asks Claude for structured JSON slots
  4. generate_slots(...) calls Claude, parses JSON
  5. compose_svg(template, slots, illustration_svg, footer_data) injects
     the slots + selected illustration into the template, returns final SVG
  6. rasterize_svg(...) -> PNG bytes via cairosvg
  7. save_image(...) -> public URL on the Railway volume

The same generate_and_save() interface is preserved so
slack_interactions._handle_generate_image is unchanged.

System libs required (installed via aptfile on Railway NIXPACKS):
  libcairo2, libpango-1.0-0, libpangocairo-1.0-0, libgdk-pixbuf2.0-0, libffi-dev
"""

import json
import os
import re
import time

import anthropic

DEFAULT_MODEL = os.environ.get("IMAGE_LLM_MODEL", "claude-sonnet-4-6")
IMAGE_DIR = os.environ.get("IMAGE_DIR", "/data/images")
IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")

CANVAS_SIZE = 1080

# Brand palette constants (BICCU). Used to color-code headline emphasis lines
# without Claude having to pick exact hex codes every time.
PRIMARY_BLUE = "#0079C8"
DARK_NAVY = "#003C71"
LIGHT_BLUE = "#47B8E8"
ACCENT_ORANGE = "#F47A20"


def build_slot_prompt(brand_config, platform, draft_text, available_illustrations):
    """Assemble the Claude prompt asking for structured JSON slots.

    Claude distills the draft into: a punchy 2-4 line all-caps headline (with
    per-line emphasis flags coloring some lines Primary Blue vs Dark Navy), a
    short supporting paragraph, a one-sentence info card, a short CTA, and an
    illustration id chosen from the available library.
    """
    vi = brand_config.get("visual_identity") or {}
    display_name = brand_config.get("display_name") or brand_config.get("brand_id")
    typography = vi.get("typography_style") or "geometric sans-serif, bold for headlines"
    avoid = vi.get("avoid") or []

    illustrations_list = ", ".join(available_illustrations) if available_illustrations else "(none available)"

    return (
        f"You are Nova, a content designer for premium financial institutions.\n"
        f"Given a draft social post and a brand, you distill the draft into structured JSON that fits a fixed visual template.\n\n"
        f"BRAND: {display_name}\n"
        f"PLATFORM: {platform}\n"
        f"TYPOGRAPHY STYLE: {typography}\n"
        f"AVOID IN COPY: {', '.join(avoid) if avoid else 'hype words, jargon'}\n\n"
        f"DRAFT TEXT (the source material to distill, do not reuse verbatim):\n\"\"\"\n{draft_text}\n\"\"\"\n\n"
        f"AVAILABLE ILLUSTRATIONS: {illustrations_list}\n\n"
        f"Produce a JSON object with EXACTLY these keys:\n"
        f"  - headline_lines: array of 2-4 short strings. Punchy, all-caps OK, creative line breaks. "
        f"Distill the draft's core message into a memorable headline. Each line is one visual line on the image.\n"
        f"  - headline_emphasis: array of booleans, same length as headline_lines. "
        f"true = this line gets the Primary Blue emphasis color; false = this line stays Dark Navy. "
        f"Use emphasis sparingly (1-2 lines true) to create dramatic hierarchy.\n"
        f"  - supporting_paragraph: 1-2 sentences (max ~220 chars) that elaborate on the headline. "
        f"Plain text, no hashtags, no emojis.\n"
        f"  - info_card_text: one short sentence (max ~110 chars) offering help or a next step. "
        f"e.g. 'Have questions about savings options at {display_name}? Our team is here to help.'\n"
        f"  - cta_text: short question or call to action (max ~60 chars), uppercase OK. "
        f"e.g. 'WHAT DOES YOUR SAVINGS ROUTINE LOOK LIKE?'\n"
        f"  - illustration_id: one of the AVAILABLE ILLUSTRATIONS above that best fits the post topic. "
        f"If the list is empty, use the string \"default\".\n\n"
        f"Return ONLY the JSON object. No code fences, no explanation, no preamble. "
        f"Start with {{ and end with }}."
    )


def _extract_json(text):
    """Pull a JSON object out of Claude's response, tolerating code fences."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0).strip()
    return text.strip()


def generate_slots(prompt, model=None):
    """Call Claude and return the parsed slots dict. Raises on SDK/parse errors."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=1200,
        system=(
            "You are Nova, a content designer for premium financial institutions. "
            "You distill draft social posts into structured JSON that fits a fixed "
            "visual template. Headlines are punchy, short, often all-caps, broken "
            "into 2-4 lines with creative line breaks. You pick the best illustration "
            "from the available list. You return ONLY a JSON object, no prose, no "
            "code fences. The JSON must be valid and parseable."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    parsed = json.loads(_extract_json(raw))
    return parsed


def _normalize_slots(slots):
    """Validate + normalize Claude's slots. Pads headline lines to 4, ensures
    headline_emphasis parallels headline_lines, applies sensible defaults."""
    headline_lines = slots.get("headline_lines") or []
    # Trim to 4 lines, strip whitespace, drop empty trailing lines.
    headline_lines = [str(line).strip() for line in headline_lines[:4] if str(line).strip()]
    while len(headline_lines) < 4:
        headline_lines.append("")

    emphasis = slots.get("headline_emphasis") or []
    # Normalize to booleans, pad/truncate to match headline_lines length.
    emphasis_bool = []
    for i in range(4):
        if i < len(emphasis):
            emphasis_bool.append(bool(emphasis[i]))
        else:
            emphasis_bool.append(False)

    return {
        "headline_lines": headline_lines,
        "headline_emphasis": emphasis_bool,
        "supporting_paragraph": str(slots.get("supporting_paragraph") or "").strip(),
        "info_card_text": str(slots.get("info_card_text") or "").strip(),
        "cta_text": str(slots.get("cta_text") or "").strip(),
        "illustration_id": str(slots.get("illustration_id") or "default").strip().lower(),
    }


def _escape_xml(text):
    """Escape XML special characters for safe injection into SVG text content."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _wrap_to_lines(text, font_size, max_width):
    """Greedy word-wrap. Returns a list of lines, each <= max_width when
    rendered at the given font_size.

    Uses a rough average-advance-width estimate of 0.52 * font_size per
    character (good enough for Arial/Helvetica/DejaVu Sans at the sizes we
    use). Long single words are hard-broken at the max width.
    """
    if not text:
        return []
    avg_char_w = 0.52 * font_size
    max_chars = max(1, int(max_width / avg_char_w))
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            # Hard-break a single word that's longer than max_chars.
            while len(current) > max_chars:
                lines.append(current[:max_chars])
                current = current[max_chars:]
    if current:
        lines.append(current)
    return lines or [""]


def _lines_to_tspans(lines, x, line_height):
    """Render a list of strings as <tspan> elements for a <text> node.

    The first tspan uses dy=0; subsequent tspans use dy=line_height. All
    tspans share the same x so lines stay left-aligned. Empty line lists
    collapse to a single empty tspan so the <text> element stays well-formed.
    """
    if not lines:
        lines = [""]
    parts = []
    for i, line in enumerate(lines):
        dy = 0 if i == 0 else line_height
        parts.append(
            f'<tspan x="{x}" dy="{dy}">{_escape_xml(line)}</tspan>'
        )
    return "".join(parts)


def compose_svg(template_str, slots, illustration_svg, footer_data):
    """Inject slots + illustration + footer data into the template, returning
    the final SVG string ready to rasterize.

    Token scheme (must match app/brands/{brand_id}/template.svg):
      {{HEADLINE_LINE_1}} .. {{HEADLINE_LINE_4}}        text content
      {{HEADLINE_LINE_1_COLOR}} .. {{HEADLINE_LINE_4}}  fill color (hex)
      {{SUPPORTING_PARA_TSPANS}}  pre-wrapped tspans for the supporting paragraph
      {{INFO_CARD_TSPANS}}        pre-wrapped tspans for the info card text
      {{CTA_TSPANS}}              pre-wrapped tspans for the CTA text
      {{ILLUSTRATION_SVG}}        inline <g> from the illustrations library
      {{FOOTER_WEBSITE}} {{FOOTER_PHONE}} {{FOOTER_TAGLINE}} {{FOOTER_HASHTAG}}

    Text wrapping happens here (not in the SVG) because cairosvg does not
    reliably render foreignObject/HTML. We measure width using a rough
    average-advance-width estimate and break into tspans.
    """
    s = _normalize_slots(slots)
    headline_colors = [
        PRIMARY_BLUE if emp else DARK_NAVY for emp in s["headline_emphasis"]
    ]

    # Pre-wrap the three text zones. Widths match the template's zone widths.
    supporting_lines = _wrap_to_lines(s["supporting_paragraph"], font_size=22, max_width=460)
    info_lines = _wrap_to_lines(s["info_card_text"], font_size=15, max_width=372)
    # CTA is uppercase in the template spirit; wrap the uppercase form so
    # wider letters don't overflow the navy bar.
    cta_text = s["cta_text"]
    cta_lines = _wrap_to_lines(cta_text, font_size=15, max_width=360)

    # Cap line counts so zones don't overflow their containers.
    supporting_lines = supporting_lines[:4]
    info_lines = info_lines[:3]
    cta_lines = cta_lines[:2]

    replacements = {
        "{{HEADLINE_LINE_1}}": _escape_xml(s["headline_lines"][0]),
        "{{HEADLINE_LINE_2}}": _escape_xml(s["headline_lines"][1]),
        "{{HEADLINE_LINE_3}}": _escape_xml(s["headline_lines"][2]),
        "{{HEADLINE_LINE_4}}": _escape_xml(s["headline_lines"][3]),
        "{{HEADLINE_LINE_1_COLOR}}": headline_colors[0],
        "{{HEADLINE_LINE_2_COLOR}}": headline_colors[1],
        "{{HEADLINE_LINE_3_COLOR}}": headline_colors[2],
        "{{HEADLINE_LINE_4_COLOR}}": headline_colors[3],
        "{{SUPPORTING_PARA_TSPANS}}": _lines_to_tspans(supporting_lines, x=80, line_height=30),
        "{{INFO_CARD_TSPANS}}": _lines_to_tspans(info_lines, x=156, line_height=20),
        "{{CTA_TSPANS}}": _lines_to_tspans(cta_lines, x=92, line_height=18),
        "{{ILLUSTRATION_SVG}}": illustration_svg or "",
        "{{FOOTER_WEBSITE}}": _escape_xml(footer_data.get("website", "")),
        "{{FOOTER_PHONE}}": _escape_xml(footer_data.get("phone", "")),
        "{{FOOTER_TAGLINE}}": _escape_xml(footer_data.get("tagline", "")),
        "{{FOOTER_HASHTAG}}": _escape_xml(footer_data.get("hashtag", "")),
    }

    out = template_str
    for token, value in replacements.items():
        out = out.replace(token, value)
    return out


def rasterize_svg(svg_str, output_width=CANVAS_SIZE, output_height=CANVAS_SIZE):
    """Convert SVG markup to PNG bytes via cairosvg. Raises on parse/render errors."""
    import cairosvg

    return cairosvg.svg2png(
        bytestring=svg_str.encode("utf-8"),
        output_width=output_width,
        output_height=output_height,
    )


def save_image(image_bytes, brand_id, item_id):
    """Write image bytes to the Railway volume and return a public URL.

    Files are named `{brand_id}_{item_id}_{unix_ts}.png` so regenerating an
    image produces a new file (not an overwrite) — Slack and Meta both see a
    fresh URL and re-fetch rather than serving cached bytes.

    Returns None if IMAGE_BASE_URL is not configured (e.g. local dev), so the
    caller can fall back to a text-only draft instead of crashing.
    """
    if not os.path.isdir(IMAGE_DIR):
        try:
            os.makedirs(IMAGE_DIR, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"IMAGE_DIR {IMAGE_DIR} does not exist and could not be created: {exc}"
            )

    filename = f"{brand_id}_{item_id}_{int(time.time())}.png"
    path = os.path.join(IMAGE_DIR, filename)
    with open(path, "wb") as f:
        f.write(image_bytes)

    if not IMAGE_BASE_URL:
        return None
    return f"{IMAGE_BASE_URL}/static/images/{filename}"


def generate_and_save(brand_config, platform, draft_text, item_id, model=None):
    """One-shot: load template + illustrations, call Claude for slots, compose,
    rasterize, save PNG, return (public_url, prompt, model_used).

    Raises on any failure. The caller (Slack interaction handler) is
    responsible for catching exceptions and posting a user-facing error.

    Falls back to the brand's `default_illustration` if Claude picks an id that
    isn't in the library, or "default" — and if neither exists, the
    illustration zone is left empty (the template still renders).
    """
    from core import design_loader

    brand_id = brand_config.get("brand_id", "brand")
    template_str = design_loader.load_template(brand_id, brand_config)
    if not template_str:
        raise RuntimeError(
            f"No template.svg found for brand '{brand_id}'. "
            f"Expected at app/brands/{brand_id}/template.svg."
        )

    illustrations = design_loader.list_illustrations(brand_id, brand_config)
    illustration_ids = list(illustrations.keys())

    prompt = build_slot_prompt(
        brand_config, platform, draft_text, illustration_ids
    )
    model_used = model or brand_config.get("image_model") or DEFAULT_MODEL
    slots = generate_slots(prompt, model=model_used)

    # Resolve the illustration. Fall back gracefully: chosen -> default -> empty.
    chosen_id = slots.get("illustration_id") or "default"
    design_cfg = design_loader.get_design_config(brand_config)
    default_id = design_cfg.get("default_illustration")
    if chosen_id in illustrations:
        illustration_svg = illustrations[chosen_id]
    elif default_id and default_id in illustrations:
        illustration_svg = illustrations[default_id]
    elif illustrations:
        illustration_svg = next(iter(illustrations.values()))
    else:
        illustration_svg = ""

    footer_data = design_loader.get_footer_data(brand_config)
    final_svg = compose_svg(template_str, slots, illustration_svg, footer_data)
    png_bytes = rasterize_svg(final_svg)
    url = save_image(png_bytes, brand_id, item_id)
    return url, prompt, model_used
