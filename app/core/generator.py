import os

import anthropic

from core.brand_loader import load_voice

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def generate_draft(brand_config, platform):
    """Generate text-only post copy for a single brand/platform pair."""
    voice = load_voice(brand_config["brand_id"])

    system_prompt = (
        f"{voice}\n\n"
        f"You are drafting a {platform} post for {brand_config['display_name']}.\n"
        "Follow the voice rules above exactly. No em-dashes.\n"
        "Return only the post copy, nothing else. No preamble, no quotes."
    )

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY read from env at call time
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": "Draft the next post."}],
    )

    return response.content[0].text.strip()


def generate_image(brand_config, platform, draft_text):
    """Stub. V2 wires this via Auto mode across Grok, Gemini, ChatGPT."""
    return None
