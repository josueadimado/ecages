from __future__ import annotations

from django import template
import unicodedata

register = template.Library()


@register.filter(name="fix_encoding")
def fix_encoding(value: str) -> str:
    """Attempt to fix common mojibake (e.g., â€™) and normalize Unicode.

    Strategy:
    - Best-effort round-trip: interpret the current string as if bytes were decoded
      using latin-1, then decode as UTF-8 to restore original characters when
      the source was double-decoded. If this fails, fall back to the original.
    - Normalize to NFC to render accents consistently.
    """
    if not isinstance(value, str):
        return value
    repaired = value
    try:
        # Try to reverse common mojibake: "â€™" -> "’", etc.
        repaired = value.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        repaired = value

    # Additional direct replacements for frequent cases
    replacements = {
        "â€™": "’",  # right single quotation mark
        "â€œ": "“",  # left double quotation mark
        "â€�": "”",  # right double quotation mark
        "â€“": "–",  # en dash
        "â€”": "—",  # em dash
        "â€¦": "…",  # ellipsis
        "â€˜": "‘",  # left single quotation mark
        "Ã©": "é",
        "Ã": "À",  # very rough fallback; real fix should be DB clean-up
    }
    for bad, good in replacements.items():
        repaired = repaired.replace(bad, good)

    try:
        repaired = unicodedata.normalize("NFC", repaired)
    except Exception:
        pass
    return repaired


