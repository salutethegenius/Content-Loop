"""Gemini image generation for content drafts.

Uses Google's `google-genai` SDK against the Gemini 3.x image model family
(Nano Banana). Default model is `gemini-3.1-flash-lite-image` (~$0.034/image,
4-second latency, 1K resolution, legible in-image text rendering) — the
cheapest option in the family and a good fit for "bold typography with brand
branding" posts. Per-brand override via `config.image_model`.

Pipeline:
  1. `build_prompt(brand_config, platform, draft_text)` assembles the prompt
     from the brand's `visual_identity` block + `image_style_prompt` + draft.
  2. `generate_image(prompt, model)` calls Gemini and returns raw PNG bytes.
  3. `save_image(image_bytes, brand_id, item_id)` writes the bytes to the
     Railway volume mounted at IMAGE_DIR (default /data/images) and returns
     a public URL via IMAGE_BASE_URL.

The public URL is what gets stored on `content_items.image_url` and shown in
Slack + sent to Meta Graph /photos when publishing.
"""

import base64
import os
import time
from datetime import datetime, timezone

DEFAULT_MODEL = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-lite-image")
IMAGE_DIR = os.environ.get("IMAGE_DIR", "/data/images")
# Public base URL for the FastAPI /static/images route. Set to the Railway
# app URL on the nova service so Meta can fetch the image.
IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL", "").rstrip("/")

# Per-platform aspect ratio hints. FB/IG feed are 1:1; IG can also do 4:5.
# Lite model only supports 1K (1024x1024 at 1:1), so we stick to 1:1 for now.
PLATFORM_ASPECT_RATIO = {
    "facebook": "1:1",
    "instagram": "1:1",
    "linkedin": "1:1",
}


def build_prompt(brand_config, platform, draft_text):
    """Assemble the Gemini prompt for one draft's image.

    Pulls from `brand_config.visual_identity` (colors, typography, layout,
    wordmark, avoid) plus the legacy one-sentence `image_style_prompt`. The
    draft text is rendered verbatim as the hero typography.
    """
    vi = brand_config.get("visual_identity") or {}
    colors = vi.get("colors") or []
    typography = vi.get("typography_style") or "bold sans-serif, headline weight"
    layout = vi.get("layout") or "centered bold headline on solid brand-color background"
    wordmark = (vi.get("wordmark_text") or "").strip()
    avoid = vi.get("avoid") or []
    image_style = (brand_config.get("image_style_prompt") or "").strip()
    display_name = brand_config.get("display_name") or brand_config.get("brand_id")

    aspect = PLATFORM_ASPECT_RATIO.get(platform, "1:1")

    parts = [
        f"Bold social media image for {display_name}, {aspect} aspect ratio.",
        f'Render this exact text as the hero typography, verbatim, no paraphrasing: "{draft_text}"',
        f"Typography style: {typography}.",
    ]
    if colors:
        parts.append(f"Color palette (use these only): {', '.join(colors)}.")
    parts.append(f"Layout: {layout}.")
    if wordmark:
        parts.append(
            f'Place the wordmark "{wordmark}" small in the bottom-right corner.'
        )
    if avoid:
        parts.append(f"Avoid: {', '.join(avoid)}.")
    if image_style:
        parts.append(f"Overall feel: {image_style}")
    parts.append(
        "Do not add any other text, slogans, watermarks, or invented content. "
        "Render the draft text exactly as given. Clean, brand-consistent, "
        "ready to post on social media."
    )
    return "\n".join(parts)


def generate_image(prompt, model=None):
    """Call Gemini and return raw PNG bytes. Raises on SDK/API errors.

    `model` defaults to IMAGE_MODEL env var or `gemini-3.1-flash-lite-image`.
    """
    from google import genai  # imported lazily so DB-only paths don't require the SDK

    client = genai.Client()  # GEMINI_API_KEY read from env at call time
    model = model or DEFAULT_MODEL

    interaction = client.interactions.create(
        model=model,
        input=prompt,
        response_format={
            "type": "image",
            "mime_type": "image/jpeg",
            "aspect_ratio": "1:1",
        },
    )

    output_image = getattr(interaction, "output_image", None)
    if not output_image or not getattr(output_image, "data", None):
        raise RuntimeError(
            f"Gemini returned no image data. Interaction: {interaction!r}"
        )
    return base64.b64decode(output_image.data)


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

    filename = f"{brand_id}_{item_id}_{int(time.time())}.jpg"
    path = os.path.join(IMAGE_DIR, filename)
    with open(path, "wb") as f:
        f.write(image_bytes)

    if not IMAGE_BASE_URL:
        # Local dev / no public URL configured: still saved on disk, but no
        # URL to hand to Slack/Meta. Caller should treat as "no image".
        return None
    return f"{IMAGE_BASE_URL}/static/images/{filename}"


def generate_and_save(brand_config, platform, draft_text, item_id, model=None):
    """One-shot: build prompt, call Gemini, save bytes, return
    (public_url, prompt, model_used). Raises on any failure.

    The caller (Slack interaction handler) is responsible for catching
    exceptions and posting a user-facing error in Slack.
    """
    prompt = build_prompt(brand_config, platform, draft_text)
    model_used = model or brand_config.get("image_model") or DEFAULT_MODEL
    image_bytes = generate_image(prompt, model=model_used)
    url = save_image(image_bytes, brand_config.get("brand_id", "brand"), item_id)
    return url, prompt, model_used
