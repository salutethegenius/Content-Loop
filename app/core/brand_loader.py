import json
import os
from datetime import datetime, timedelta, timezone

BRANDS_DIR = os.path.join(os.path.dirname(__file__), "..", "brands")

# Keys that may land in the filesystem seed after a brand was already
# onboarded to Postgres. Fill them in when the DB row is missing them so
# deploy-time seed additions (meta_page_id, design) take effect without a
# mandatory DB patch. DB values always win when present and non-empty.
_SEED_FILL_KEYS = (
    "meta_page_id",
    "meta_token_env",
    "design",
)


def _load_seed_config(brand_id):
    """Return filesystem seed config.json for a brand, or None."""
    config_path = os.path.join(BRANDS_DIR, brand_id, "config.json")
    if not os.path.isfile(config_path):
        return None
    with open(config_path) as f:
        return json.load(f)


def _merge_seed_fill(config):
    """Overlay missing seed-only keys onto a DB (or partial) brand config."""
    if not config:
        return config
    brand_id = config.get("brand_id")
    if not brand_id:
        return config
    seed = _load_seed_config(brand_id)
    if not seed:
        return config
    merged = dict(config)
    for key in _SEED_FILL_KEYS:
        current = merged.get(key)
        empty = current is None or current == "" or current == {}
        if empty and seed.get(key) not in (None, "", {}):
            merged[key] = seed[key]

    # Overlay visual_identity.colors from seed. Seed is the design-system
    # source of truth for headline palette (e.g. KGC cream-on-navy); fill
    # missing keys and let non-empty seed values refresh stale DB colors.
    seed_vi = seed.get("visual_identity") or {}
    seed_colors = seed_vi.get("colors") if isinstance(seed_vi, dict) else None
    if seed_colors:
        vi = merged.get("visual_identity")
        vi = dict(vi) if isinstance(vi, dict) else {}
        # Onboarding may store colors as a flat list of hexes; the design
        # system needs the named dict, so a non-dict value is replaced.
        current_colors = vi.get("colors")
        colors = dict(current_colors) if isinstance(current_colors, dict) else {}
        for key, val in seed_colors.items():
            if isinstance(val, str) and val.strip():
                colors[key] = val.strip()
        vi["colors"] = colors
        if not vi.get("wordmark_text") and seed_vi.get("wordmark_text"):
            vi["wordmark_text"] = seed_vi["wordmark_text"]
        merged["visual_identity"] = vi

    # Fill empty footer fields from seed. Onboarding may create the footer
    # dict with blank values (so the dict-level fill above never fires);
    # per-field fill lets the seed supply website/phone/tagline without
    # clobbering anything the DB already has.
    seed_footer = seed.get("footer")
    if isinstance(seed_footer, dict) and seed_footer:
        footer = dict(merged.get("footer") or {})
        for key, val in seed_footer.items():
            if key == "social":
                continue
            if not footer.get(key) and isinstance(val, str) and val.strip():
                footer[key] = val.strip()
        seed_social = seed_footer.get("social") or {}
        if isinstance(seed_social, dict):
            social = dict(footer.get("social") or {})
            for key, val in seed_social.items():
                if not social.get(key) and isinstance(val, str) and val.strip():
                    social[key] = val.strip()
            footer["social"] = social
        merged["footer"] = footer
    return merged


def get_active_brands():
    """Return configs for every active brand.

    DB-backed brands (created via onboarding) take precedence. Filesystem
    brands under app/brands/{brand}/ are the seed/fallback. A brand present
    in both is served from the DB so onboarding edits win. Missing meta/design
    keys are filled from the filesystem seed.
    """
    brands = []
    seen = set()

    # 1. DB-backed brands.
    try:
        from core import db as _db
        for config in _db.list_db_brands():
            if config.get("active") and config.get("brand_id") not in seen:
                brands.append(_merge_seed_fill(config))
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

    `last_posted_at` is the cadence baseline — the caller passes the newest
    non-rejected draft's created_at (db.get_last_activity), so schedule-only
    brands with no `posted` rows still respect their cadence. A brand with no
    history at all is always due, so the first draft is not skipped forever.
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
