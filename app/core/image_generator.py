"""Claude SVG image generation for content drafts.

Claude can't emit raster pixels directly, but it writes excellent SVG. For the
"bold typography with brand branding" use case (no photorealism), SVG is
actually the ideal medium: pixel-perfect text, solid brand colors, crisp
wordmarks, infinite scalability. We have Claude produce a standalone SVG,
then rasterize it to PNG with cairosvg so Slack and Meta can consume it.

Pipeline:
  1. `build_svg_prompt(brand_config, platform, draft_text)` assembles the
     instruction prompt from the brand's `visual_identity` block + draft.
  2. `generate_svg(prompt, model)` calls Claude and returns raw SVG markup.
  3. `rasterize_svg(svg_str)` converts SVG → PNG bytes via cairosvg.
  4. `save_image(png_bytes, brand_id, item_id)` writes the PNG to the Railway
     volume and returns a public URL via IMAGE_BASE_URL.

Default model is `claude-sonnet-4-6` (same as text drafts). Override per
brand via `config.image_model` or globally via `IMAGE_LLM_MODEL`.

System libs required (installed via aptfile on Railway NIXPACKS):
  libcairo2, libpango-1.0-0, libpangocairo-1.0-0, libgdk-pixbuf2.0-0, libffi-dev
"""

import os
import re
import time

import anthropic

DEFAULT_MODEL = os.environ.get("IMAGE_LLM_MODEL", "claude-sonnet-4-6")
IMAGE_DIR = os.environ.get("IMAGE_DIR", "/data/images")
# Public base URL for the FastAPI /static/images route.
IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")

# Square 1024x1024 for FB/IG feed.
CANVAS_SIZE = 1024


def build_svg_prompt(brand_config, platform, draft_text):
    """Assemble the Claude prompt asking for a complete standalone SVG.

    Pulls from `brand_config.visual_identity` (colors, typography, layout,
    wordmark, avoid). The draft text is rendered verbatim as the hero
    typography, manually broken into balanced <tspan> lines.
    """
    vi = brand_config.get("visual_identity") or {}
    colors = vi.get("colors") or []
    typography = vi.get("typography_style") or "bold sans-serif, headline weight"
    layout = vi.get("layout") or "centered bold headline on solid brand-color background"
    wordmark = (vi.get("wordmark_text") or "").strip()
    avoid = vi.get("avoid") or []
    image_style = (brand_config.get("image_style_prompt") or "").strip()
    display_name = brand_config.get("display_name") or brand_config.get("brand_id")

    palette_hint = ", ".join(colors) if colors else "brand-appropriate solid colors"
    avoid_hint = ", ".join(avoid) if avoid else "stock photos, clipart, gradients"

    wordmark_instruction = (
        f'Place the wordmark "{wordmark}" small (font-size ~28px) in the '
        f"bottom-right corner with 32px padding, in a muted color from the palette."
        if wordmark else "Do not add a wordmark."
    )

    return (
        f"You are Nova, generating a bold social media image as SVG for "
        f"{display_name} on {platform}.\n\n"
        f"Produce a complete, standalone SVG document with a "
        f"{CANVAS_SIZE}x{CANVAS_SIZE} viewBox that:\n"
        f"- Has a solid background using one of these brand colors: {palette_hint}\n"
        f'- Renders this exact text as the hero typography, centered, bold, '
        f'large (font-size 64-88px), in a contrasting color from the palette: '
        f'"{draft_text}"\n'
        f"  Break the text into balanced lines using multiple <tspan> elements "
        f"with dy offsets (e.g. dy=\"1.2em\"). Do NOT paraphrase, abbreviate, "
        f"or drop any words. Use the full text verbatim. Escape any <, >, & "
        f"as &lt;, &gt;, &amp;.\n"
        f"- Typography style: {typography}.\n"
        f"- Use font-family=\"Arial, Helvetica, sans-serif\" (Linux-safe). "
        f"Do NOT use external fonts or @font-face.\n"
        f"- Layout: {layout}.\n"
        f"- {wordmark_instruction}\n"
        f"- Avoid: {avoid_hint}. No <image> tags, no external refs, no scripts.\n"
        f"- Inline all styles as attributes or a <style> block.\n"
        f"- Make it visually striking and brand-consistent.\n"
        + (f"- Overall feel: {image_style}\n" if image_style else "")
        + "\nReturn ONLY the raw SVG markup. No code fences, no explanation, "
        f"no preamble. Start with <svg and end with </svg>."
    )


def _extract_svg(text):
    """Pull the SVG out of Claude's response, tolerating code fences."""
    # Strip ```svg ... ``` or ``` ... ``` fences if present.
    fenced = re.search(r"```(?:svg)?\s*(<svg.*?</svg>)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    # Otherwise grab the first <svg>...</svg> block.
    match = re.search(r"<svg.*?</svg>", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(0).strip()
    return text.strip()


def generate_svg(prompt, model=None):
    """Call Claude and return raw SVG markup. Raises on SDK/API errors."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=2000,
        system=(
            "You are a senior SVG designer. You produce valid, standalone SVG "
            "documents that render correctly with cairosvg. You never include "
            "external fonts, external images, scripts, or features cairosvg "
            "does not support (no filters, no clip-path userSpaceOnUse "
            "complexity). You return ONLY raw SVG markup."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    return _extract_svg(raw)


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
    """One-shot: build prompt, call Claude for SVG, rasterize, save PNG, return
    (public_url, prompt, model_used). Raises on any failure.

    The caller (Slack interaction handler) is responsible for catching
    exceptions and posting a user-facing error in Slack.
    """
    prompt = build_svg_prompt(brand_config, platform, draft_text)
    model_used = model or brand_config.get("image_model") or DEFAULT_MODEL
    svg_str = generate_svg(prompt, model=model_used)
    png_bytes = rasterize_svg(svg_str)
    url = save_image(png_bytes, brand_config.get("brand_id", "brand"), item_id)
    return url, prompt, model_used
