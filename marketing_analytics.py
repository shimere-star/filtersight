"""Small, explicit marketing instrumentation. Never render in member views."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    CONFIG = json.loads((ROOT / "analytics_config.json").read_text())
except (OSError, ValueError):
    CONFIG = {"website_id": "", "campaigns": {}}
SCRIPT = (ROOT / "assets" / "analytics.js").read_text()


def campaign_tags(params):
    """Only reviewed campaign labels may reach analytics or Stripe metadata."""
    return {
        key: params.get(key)
        for key, allowed in CONFIG["campaigns"].items()
        if params.get(key) in allowed
    }


def analytics_html(event, tags, dedup_key):
    """Render our own sender, not a third-party script with access to form fields."""
    if not CONFIG["website_id"]:
        return ""
    # Only a digest is used locally for duplicate protection; it is never sent.
    key = "filtersight.analytics." + hashlib.sha256(dedup_key.encode()).hexdigest()
    args = json.dumps([CONFIG, event, campaign_tags(tags), key]).replace("<", "\\u003c")
    return f"<script>{SCRIPT}\nwindow.FilterSightAnalytics.signupEvent(...{args});</script>"
