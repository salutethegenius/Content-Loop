import json
import os
from datetime import datetime, timedelta, timezone

BRANDS_DIR = os.path.join(os.path.dirname(__file__), "..", "brands")


def get_active_brands():
    """Return configs for every active brand.

    DB-backed brands (created via onboarding) take precedence. Filesystem
    brands under app/brands/{brand}/ are the seed/fallback. A brand present
    in both is served from the DB so onboarding edits win.
    """
    brands = []
    seen = set()

    # 1. DB-backed brands.
    try:
        from core import db as _db
        for config in _db.list_db_brands():
            if config.get("active") and config.get("brand_id") not in seen:
                brands.append(config)
                seen.add(config["brand_id"])
    except Exception:
        # DB not reachable / not configured: fall through to filesystem.
        pass

    # 2. Filesystem brands not already in DB.
    if os.path.isdir(BRANDS_DIR):
        for folder in sorted(os.listdir(BRANDS_DIR)):
            config_path = os.path.join(BRANDS_DIR, folder, "config.json")
            if not os.path.exists(config_path):
                continue
            with open(config_path) as f:
                config = json.load(f)
            if config.get("active") and config.get("brand_id") not in seen:
                brands.append(config)
                seen.add(config["brand_id"])

    return brands


def get_brand_by_id(brand_id):
    """Return an active brand config by id, or None."""
    for brand in get_active_brands():
        if brand.get("brand_id") == brand_id:
            return brand
    return None


def load_voice(brand_id):
    """Return the raw markdown system prompt for a brand.

    DB-backed voice wins; falls back to app/brands/{brand}/voice.md on disk.
    """
    try:
        from core import db as _db
        result = _db.get_brand_from_db(brand_id)
        if result is not None:
            _config, voice_md = result
            if voice_md:
                return voice_md
    except Exception:
        pass

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
