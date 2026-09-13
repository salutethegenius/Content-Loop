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
import tempfile
import time
from contextlib import contextmanager

import anthropic

DEFAULT_MODEL = os.environ.get("IMAGE_LLM_MODEL", "claude-sonnet-4-6")
IMAGE_DIR = os.environ.get("IMAGE_DIR", "/data/images")
IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")

CANVAS_SIZE = 1080


class TemplateNotFound(RuntimeError):
    """The brand has no template.svg, so design-system image generation is
    not available for it. Callers show a friendly "no design system yet"
    message instead of a generic failure."""

# Default palette (BICCU). Per-brand overrides come from
# brand_config.visual_identity.colors when present.
PRIMARY_BLUE = "#0079C8"
DARK_NAVY = "#003C71"


def _headline_palette(brand_config=None):
    """Return (emphasis_hex, base_hex) for headline line colors.

    Reads visual_identity.colors when set:
      primary / accent / gold  -> emphasis
      navy / dark / secondary  -> base
    Falls back to BICCU Primary Blue / Dark Navy.
    """
    colors = ((brand_config or {}).get("visual_identity") or {}).get("colors") or {}
    if not isinstance(colors, dict):
        colors = {}

    def _pick(*keys, default):
        for key in keys:
            val = colors.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return default

    emphasis = _pick("primary", "accent", "gold", "emphasis", default=PRIMARY_BLUE)
    base = _pick("navy", "dark", "secondary", "base", default=DARK_NAVY)
    return emphasis, base


def _is_swiss_layout(brand_config):
    layout = ((brand_config or {}).get("visual_identity") or {}).get("layout") or ""
    return "swiss" in str(layout).lower() or "poster" in str(layout).lower()


def build_slot_prompt(brand_config, platform, draft_text, available_illustrations):
    """Assemble the Claude prompt asking for structured JSON slots.

    Claude distills the draft into: a punchy 2-4 line all-caps headline (with
    per-line emphasis flags coloring some lines Primary vs Dark), supporting
    copy, an info card, a short CTA, and an illustration id from the library.
    Swiss-poster brands get tighter copy rules (label, not paragraph).
    """
    vi = brand_config.get("visual_identity") or {}
    display_name = brand_config.get("display_name") or brand_config.get("brand_id")
    typography = vi.get("typography_style") or "geometric sans-serif, bold for headlines"
    layout = vi.get("layout") or ""
    avoid = vi.get("avoid") or []
    style = (brand_config.get("image_style_prompt") or "").strip()
    style_line = f"BRAND IMAGE STYLE: {style}\n" if style else ""
    layout_line = f"LAYOUT: {layout}\n" if layout else ""

    illustrations_list = ", ".join(available_illustrations) if available_illustrations else "(none available)"
    swiss = _is_swiss_layout(brand_config)

    if swiss:
        slot_spec = (
            "Produce a JSON object with EXACTLY these keys:\n"
            "  - headline_lines: array of 2-4 short strings, EACH LINE 14 CHARACTERS OR FEWER "
            "(count spaces). 3-6 words total across the stack, ALL CAPS. One word or two short "
            "words per line. Hero = stacked statement. Stat = a huge number as line 1 "
            "(['30K+', 'SENDABLE']). Price = the figure as line 1 (['$19.99'] or ['$65']). "
            "Do not write credit-union or savings language unless the draft is about that.\n"
            "  - headline_emphasis: array of booleans, same length as headline_lines. "
            "true = this line gets the primary accent (orange). false = black. "
            "Exactly ONE line true. Never emphasize every line. Do not use a second accent "
            "color. Do not invent photography.\n"
            "  - supporting_paragraph: one tiny uppercase label, MAX 42 CHARACTERS. "
            "e.g. 'NASSAU FOOD · SENDABLE'. Not a sentence. No hashtags, no emojis.\n"
            "  - info_card_text: price, proof, or terms. MAX 60 CHARACTERS. "
            "e.g. '$19.99 HOT LIST CAMPAIGN' or '38% AVG OPEN RATE'. Not a help-desk offer.\n"
            "  - cta_text: short uppercase CTA, MAX 28 CHARACTERS. "
            "e.g. 'BOOK AT KEMIS.EMAIL'. Prefer the brand website over a hashtag.\n"
            "  - illustration_id: one of the AVAILABLE ILLUSTRATIONS above. "
            "Those are geometric residues (circle, line, square, triangle, slash, rules, "
            "arc, tick) — NOT icons, envelopes, chimps, or @ marks. Pick a quiet shape. "
            "If the list is empty, use the string \"default\".\n"
        )
    else:
        slot_spec = (
            "Produce a JSON object with EXACTLY these keys:\n"
            "  - headline_lines: array of 2-4 short strings, EACH LINE 14 CHARACTERS OR FEWER "
            "(count spaces). Punchy, ALL CAPS, creative line breaks. One word or two short words "
            "per line. e.g. [\"BUILD\", \"STRONGER\", \"TOGETHER\"] or [\"YOUR FIRST\", \"HOME\", \"STORY\"]. "
            "Distill the draft's core message into a memorable headline. Do not default to "
            "savings language unless the draft is actually about saving.\n"
            "  - headline_emphasis: array of booleans, same length as headline_lines. "
            "true = this line gets the primary emphasis color; false = the dark/base color. "
            "Use emphasis on 1-2 lines to create dramatic hierarchy.\n"
            "  - supporting_paragraph: 1-2 sentences, 120 to 200 characters total. "
            "Elaborates on the headline. Plain text, no hashtags, no emojis, no bullet points.\n"
            f"  - info_card_text: one short sentence, max 95 characters, offering help or a next step. "
            f"e.g. 'Have questions? The {display_name} team is here to help.'\n"
            "  - cta_text: short question or call to action, MAX 45 CHARACTERS, uppercase. "
            "e.g. 'WHAT DOES THIS MOMENT LOOK LIKE FOR YOU?'\n"
            "  - illustration_id: one of the AVAILABLE ILLUSTRATIONS above that best fits the post topic. "
            "If the list is empty, use the string \"default\".\n"
        )

    return (
        f"You are Nova, a content designer for {display_name}.\n"
        f"Given a draft social post and a brand, you distill the draft into structured JSON that fits a fixed visual template.\n\n"
        f"BRAND: {display_name}\n"
        f"PLATFORM: {platform}\n"
        f"TYPOGRAPHY STYLE: {typography}\n"
        f"{layout_line}"
        f"{style_line}"
        f"AVOID: {', '.join(avoid) if avoid else 'hype words, jargon'}\n\n"
        f"DRAFT TEXT (the source material to distill, do not reuse verbatim):\n\"\"\"\n{draft_text}\n\"\"\"\n\n"
        f"AVAILABLE ILLUSTRATIONS: {illustrations_list}\n\n"
        f"{slot_spec}\n"
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
            "You are Nova, a content designer. You distill draft social posts into "
            "structured JSON that fits a fixed visual template. Follow the user "
            "prompt's layout rules exactly. You return ONLY a JSON object, no prose, "
            "no code fences. The JSON must be valid and parseable."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    parsed = json.loads(_extract_json(raw))
    return parsed


def _normalize_slots(slots):
    """Validate + normalize Claude's slots. Pads headline lines to 4, ensures
    headline_emphasis parallels headline_lines, applies sensible defaults.
    Headlines are forced to uppercase so output is consistent regardless of
    which model filled the slots."""
    headline_lines = slots.get("headline_lines") or []
    raw_emphasis = slots.get("headline_emphasis") or []
    # Pair each line with its emphasis flag, trim to 4, drop empties.
    paired = []
    for i, line in enumerate(headline_lines[:4]):
        text = str(line).strip().upper()
        if text:
            emp = bool(raw_emphasis[i]) if i < len(raw_emphasis) else False
            paired.append((text, emp))
    # Hard-split any line a model made too long (>16 chars). Split at the
    # space nearest the middle; both halves keep the original emphasis flag.
    # Recursive so a very long single line gets split into as many pieces as
    # needed (rare, but a model could ignore the 14-char instruction).
    def _split_line(text, emp):
        if len(text) <= 16 or " " not in text:
            return [(text, emp)]
        mid = len(text) // 2
        spaces = [i for i, ch in enumerate(text) if ch == " "]
        split_at = min(spaces, key=lambda i: abs(i - mid))
        left = text[:split_at].strip()
        right = text[split_at:].strip()
        return _split_line(left, emp) + _split_line(right, emp)

    split_pairs = []
    for text, emp in paired:
        split_pairs.extend(_split_line(text, emp))
    # Cap to 4 lines. If content has to be dropped, mark the last kept line
    # with an ellipsis so the reader knows the headline was truncated.
    if len(split_pairs) > 4:
        split_pairs = split_pairs[:4]
        if split_pairs[3][0]:
            last_text, last_emp = split_pairs[3]
            split_pairs[3] = (last_text.rstrip(".,;: ") + "…", last_emp)
    while len(split_pairs) < 4:
        split_pairs.append(("", False))

    headline_lines = [p[0] for p in split_pairs]
    emphasis_bool = [p[1] for p in split_pairs]

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


def _fit_headline(headline_lines, max_width=560, max_font=100, min_font=48,
                  line_height_ratio=1.14, start_y_base=200, char_advance=0.60):
    """Compute a font size that guarantees the longest headline line fits the
    left column, plus the matching line height and first-baseline Y.

    Bold condensed caps average ~0.60 * font_size per character advance
    (override via design.headline_char_advance for tighter faces).
    """
    longest = max((len(line) for line in headline_lines if line), default=1)
    advance = char_advance if char_advance and char_advance > 0 else 0.60
    fitted = int(max_width / (advance * longest))
    font_size = max(min_font, min(max_font, fitted))
    line_height = int(font_size * line_height_ratio)
    start_y = start_y_base + font_size
    return font_size, line_height, start_y


def _design_fit_kwargs(brand_config):
    """Headline-fit overrides from brand_config.design. Missing keys keep
    the original BICCU column (560 / 48–100 / 1.14)."""
    design = (brand_config or {}).get("design") or {}

    def _num(key, default, caster=int):
        raw = design.get(key)
        if raw is None or raw == "":
            return default
        try:
            return caster(raw)
        except (TypeError, ValueError):
            return default

    return dict(
        max_width=_num("headline_max_width", 560),
        max_font=_num("headline_max_font", 100),
        min_font=_num("headline_min_font", 48),
        line_height_ratio=_num("headline_line_height_ratio", 1.14, float),
        start_y_base=_num("headline_start_y_base", 200),
        char_advance=_num("headline_char_advance", 0.60, float),
    )


def compose_svg(template_str, slots, illustration_svg, footer_data,
                brand_config=None):
    """Inject slots + illustration + footer data into the template, returning
    the final SVG string ready to rasterize.

    Token scheme (must match app/brands/{brand_id}/template.svg):
      {{HEADLINE_LINE_1}} .. {{HEADLINE_LINE_4}}        text content
      {{HEADLINE_LINE_1_COLOR}} .. {{HEADLINE_LINE_4}}  fill color (hex)
      {{HEADLINE_FONT_SIZE}} {{HEADLINE_LINE_HEIGHT}} {{HEADLINE_START_Y}}
                                  computed so the longest line always fits
      {{HEADLINE_TRACKING}}       negative letter-spacing (swiss posters)
      {{SUPPORTING_PARA_TSPANS}}  pre-wrapped tspans for the supporting paragraph
      {{INFO_CARD_TSPANS}}        pre-wrapped tspans for the info card text
      {{CTA_TSPANS}}              single-line tspan for the CTA (truncated)
      {{ILLUSTRATION_SVG}}        inline <g> from the illustrations library
      {{FOOTER_WEBSITE}} {{FOOTER_PHONE}} {{FOOTER_TAGLINE}} {{FOOTER_HASHTAG}}

    Text wrapping and headline auto-fit happen here (not in the SVG) because
    cairosvg does not reliably render foreignObject/HTML and SVG has no
    native wrap.
    """
    s = _normalize_slots(slots)
    emphasis_hex, base_hex = _headline_palette(brand_config)
    headline_colors = [
        emphasis_hex if emp else base_hex for emp in s["headline_emphasis"]
    ]
    design = (brand_config or {}).get("design") or {}

    font_size, line_height, start_y = _fit_headline(
        s["headline_lines"], **_design_fit_kwargs(brand_config)
    )
    tracking = round(-0.04 * font_size, 1)

    supporting_text = s["supporting_paragraph"]
    if design.get("supporting_uppercase") and supporting_text:
        supporting_text = supporting_text.upper()
    supporting_font = int(design.get("supporting_font_size") or 23)
    supporting_width = int(design.get("supporting_max_width") or 520)
    supporting_max_lines = int(design.get("supporting_max_lines") or 4)
    supporting_x = int(design.get("supporting_text_x") or 64)
    info_x = int(design.get("info_card_text_x") or 150)
    cta_x = int(design.get("cta_text_x") or 96)

    # Pre-wrap the three text zones. Widths match the template's zone widths.
    supporting_all = _wrap_to_lines(
        supporting_text, font_size=supporting_font, max_width=supporting_width
    )
    supporting_lines = supporting_all[:supporting_max_lines]
    if len(supporting_all) > supporting_max_lines:
        supporting_lines[-1] = supporting_lines[-1].rstrip(".,;: ") + "…"
    info_all = _wrap_to_lines(s["info_card_text"], font_size=16, max_width=414)
    info_lines = info_all[:3]
    if len(info_all) > 3:
        info_lines[-1] = info_lines[-1].rstrip(".,;: ") + "…"

    supporting_lh = 22 if supporting_font <= 16 else 33
    if str(design.get("supporting_anchor") or "") == "below_headline":
        n_hl = sum(1 for line in s["headline_lines"] if line) or 1
        gap = int(design.get("supporting_gap") or 36)
        supporting_y = start_y + line_height * (n_hl - 1) + gap
    else:
        # Anchor bottom-up just above the info card (top at y=806) so the
        # gap stays constant however many lines wrap. BICCU/KGC default.
        supporting_y = 800 - supporting_lh * len(supporting_lines)

    # CTA is a single line; truncate with an ellipsis rather than wrapping.
    cta_text = s["cta_text"].upper()
    if len(cta_text) > 46:
        cta_text = cta_text[:45].rstrip() + "…"
    cta_lines = [cta_text]
    cta_max = int(design.get("cta_max_font") or 17)
    cta_min = int(design.get("cta_min_font") or 13)
    cta_zone = int(design.get("cta_max_width") or 440)
    # width ≈ len * (0.64*font + 0.8 letter-spacing); solve for font.
    cta_font = min(cta_max, int((cta_zone / max(len(cta_text), 1) - 0.8) / 0.64)) if cta_text else cta_max
    cta_font = max(cta_min, cta_font)

    replacements = {
        "{{HEADLINE_LINE_1}}": _escape_xml(s["headline_lines"][0]),
        "{{HEADLINE_LINE_2}}": _escape_xml(s["headline_lines"][1]),
        "{{HEADLINE_LINE_3}}": _escape_xml(s["headline_lines"][2]),
        "{{HEADLINE_LINE_4}}": _escape_xml(s["headline_lines"][3]),
        "{{HEADLINE_LINE_1_COLOR}}": headline_colors[0],
        "{{HEADLINE_LINE_2_COLOR}}": headline_colors[1],
        "{{HEADLINE_LINE_3_COLOR}}": headline_colors[2],
        "{{HEADLINE_LINE_4_COLOR}}": headline_colors[3],
        "{{HEADLINE_FONT_SIZE}}": str(font_size),
        "{{HEADLINE_LINE_HEIGHT}}": str(line_height),
        "{{HEADLINE_START_Y}}": str(start_y),
        "{{HEADLINE_TRACKING}}": str(tracking),
        "{{SUPPORTING_PARA_Y}}": str(supporting_y),
        "{{CTA_FONT_SIZE}}": str(cta_font),
        "{{SUPPORTING_PARA_TSPANS}}": _lines_to_tspans(
            supporting_lines, x=supporting_x, line_height=supporting_lh
        ),
        "{{INFO_CARD_TSPANS}}": _lines_to_tspans(info_lines, x=info_x, line_height=21),
        "{{CTA_TSPANS}}": _lines_to_tspans(cta_lines, x=cta_x, line_height=18),
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


@contextmanager
def _fontconfig_for_dir(font_dir):
    """Point fontconfig at a brand fonts/ dir for the duration of rasterize.

    cairosvg/Pango will not see TTF files sitting next to the template unless
    fontconfig lists that directory. We prepend the brand dir and still
    include the system conf so Arial/DejaVu keep working for other brands.
    """
    if not font_dir or not os.path.isdir(font_dir):
        yield
        return
    conf = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>\n"
        f"  <dir>{font_dir}</dir>\n"
        '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
        '  <include ignore_missing="yes">/opt/homebrew/etc/fonts/fonts.conf</include>\n'
        '  <include ignore_missing="yes">/usr/local/etc/fonts/fonts.conf</include>\n'
        "</fontconfig>\n"
    )
    old = os.environ.get("FONTCONFIG_FILE")
    fd, path = tempfile.mkstemp(prefix="nova-fonts-", suffix=".conf")
    try:
        os.write(fd, conf.encode("utf-8"))
        os.close(fd)
        fd = None
        os.environ["FONTCONFIG_FILE"] = path
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if old is None:
            os.environ.pop("FONTCONFIG_FILE", None)
        else:
            os.environ["FONTCONFIG_FILE"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def rasterize_svg(svg_str, output_width=CANVAS_SIZE, output_height=CANVAS_SIZE,
                  font_dir=None):
    """Convert SVG markup to PNG bytes via cairosvg. Raises on parse/render errors."""
    import cairosvg

    with _fontconfig_for_dir(font_dir):
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

    # Sanitize path components: brand_id comes from config/DB and item_id from
    # callers; neither may introduce separators or dots into the filename.
    safe_brand = re.sub(r"[^a-z0-9_-]", "", str(brand_id).lower()) or "brand"
    safe_item = re.sub(r"[^a-z0-9_-]", "", str(item_id).lower()) or "item"
    filename = f"{safe_brand}_{safe_item}_{int(time.time())}.png"
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
        raise TemplateNotFound(
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
    # Lowercase to match the library keys (lowercase filenames) — Claude
    # sometimes returns "Growth_Arrow" and would silently miss otherwise.
    chosen_id = str(slots.get("illustration_id") or "default").strip().lower()
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
    final_svg = compose_svg(
        template_str, slots, illustration_svg, footer_data,
        brand_config=brand_config,
    )
    font_dir = design_loader.get_fonts_dir(brand_id, brand_config)
    png_bytes = rasterize_svg(final_svg, font_dir=font_dir)
    url = save_image(png_bytes, brand_id, item_id)
    return url, prompt, model_used
