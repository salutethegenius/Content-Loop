import json
import os
from datetime import datetime, timedelta, timezone

BRANDS_DIR = os.path.join(os.path.dirname(__file__), "..", "brands")


def get_active_brands():
    """Return configs for every brand folder with active: true."""
    brands = []
    if not os.path.isdir(BRANDS_DIR):
        return brands
    for folder in sorted(os.listdir(BRANDS_DIR)):
        config_path = os.path.join(BRANDS_DIR, folder, "config.json")
        if not os.path.exists(config_path):
            continue
        with open(config_path) as f:
            config = json.load(f)
        if config.get("active"):
            brands.append(config)
    return brands


def load_voice(brand_id):
    """Return the raw markdown system prompt for a brand."""
    voice_path = os.path.join(BRANDS_DIR, brand_id, "voice.md")
    with open(voice_path) as f:
        return f.read()


def is_due_for_post(brand_config, last_posted_at):
    """True when a brand should generate a new draft.

    A brand with no post history yet is always due, so the first draft is not
    silently skipped forever.
    """
    if last_posted_at is None:
        return True
    cadence_days = int(brand_config.get("posting_cadence_days", 1))
    if cadence_days <= 0:
        return True

    last = last_posted_at
    if isinstance(last, str):
        last = datetime.fromisoformat(last.replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc) >= last + timedelta(days=cadence_days)
