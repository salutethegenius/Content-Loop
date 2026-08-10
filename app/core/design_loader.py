"""Load brand design-system assets (SVG template + illustration library).

Each brand can ship a fixed design system on disk under
`app/brands/{brand_id}/`:
  - `template.svg`           the fixed 1080x1080 layout with named tokens
  - `illustrations/*.svg`    a library of `<g>` groups for the illustration zone

The brand's DB config carries a `design` block pointing at these files:
  {"template": "template.svg", "illustrations_dir": "illustrations",
   "default_illustration": "piggy_bank"}

Footer data (website, phone, tagline, social handles, hashtag) comes from
`brand_config.footer` in the DB so onboarding can capture it per brand.
"""

import os

BRANDS_DIR = os.path.join(os.path.dirname(__file__), "..", "brands")


def _brand_dir(brand_id):
    return os.path.join(BRANDS_DIR, brand_id)


def _resolve_inside_brand(brand_id, relative_path):
    """Join a config-supplied relative path to the brand dir, refusing
    absolute paths or ../ traversal that would escape it. Design paths come
    from brand configs (including onboarding output), so they are not
    trusted blindly. Returns the absolute path, or None if it escapes."""
    base = os.path.abspath(_brand_dir(brand_id))
    path = os.path.abspath(os.path.join(base, relative_path))
    if path != base and not path.startswith(base + os.sep):
        return None
    return path


def get_design_config(brand_config):
    """Return the brand's `design` block with defaults applied."""
    design = (brand_config.get("design") or {}).copy() if brand_config else {}
    design.setdefault("template", "template.svg")
    design.setdefault("illustrations_dir", "illustrations")
    design.setdefault("default_illustration", None)
    return design


def load_template(brand_id, brand_config=None):
    """Read the brand's SVG template as a string. Falls back to None if no
    template file exists (the caller can decide whether to error or fall back
    to a legacy generation path)."""
    design = get_design_config(brand_config or {})
    path = _resolve_inside_brand(brand_id, design["template"])
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def list_illustrations(brand_id, brand_config=None):
    """Return a dict mapping illustration_id -> svg_str for every `.svg` in the
    brand's illustrations dir. The id is the filename without extension.
    Returns {} if the dir is missing.
    """
    design = get_design_config(brand_config or {})
    illustrations_dir = _resolve_inside_brand(brand_id, design["illustrations_dir"])
    if not illustrations_dir or not os.path.isdir(illustrations_dir):
        return {}
    out = {}
    for fname in sorted(os.listdir(illustrations_dir)):
        if not fname.lower().endswith(".svg"):
            continue
        illustration_id = os.path.splitext(fname)[0]
        with open(os.path.join(illustrations_dir, fname), "r", encoding="utf-8") as f:
            out[illustration_id] = f.read()
    return out


def get_footer_data(brand_config):
    """Return footer data for the template. Pulls from `brand_config.footer`
    (DB) with sensible empty-string defaults so the template always renders.
    """
    footer = (brand_config.get("footer") or {}) if brand_config else {}
    social = footer.get("social") or {}
    return {
        "website": footer.get("website") or "",
        "phone": footer.get("phone") or "",
        "tagline": footer.get("tagline") or "",
        "hashtag": footer.get("hashtag") or "",
        "facebook": social.get("facebook") or "",
        "instagram": social.get("instagram") or "",
        "linkedin": social.get("linkedin") or "",
    }
