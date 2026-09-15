"""Text sanitization and canonicalization helpers.

Covers two engineering principles from the technical specification:

* *Sanitization*: clean text extraction from HTML/RSS feeds;
* *Entity Resolution*: canonicalizing name variations ("OpenAI" vs "Open AI").
"""

from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher

__all__ = [
    "clean_text",
    "strip_html",
    "slugify",
    "canonical_name",
    "name_tokens",
    "similarity",
    "token_set_ratio",
    "truncate",
    "collapse_whitespace",
]

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?\s*>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\u00a0\u200b\u200c\u200d\ufeff]+")
_NL_RE = re.compile(r"\n{3,}")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

#: Marketing/legal suffixes removed before name comparison.
_NAME_NOISE = {
    "ai", "aiapp", "app", "io", "co", "inc", "inc.", "llc", "ltd", "ltd.", "gmbh",
    "corp", "corporation", "company", "the", "labs", "lab", "studio", "studios",
    "technologies", "technology", "tech", "software", "platform", "official",
    "online", "web", "tool", "tools", "com", "org", "net", "hq", "plus", "pro",
    "beta", "alpha", "free", "get", "try", "use", "my",
}

#: Common directory decorations, e.g. "Jasper AI - AI Writing Assistant".
_TAGLINE_SEPARATORS = (" | ", " — ", " – ", " - ", " :: ", " • ", " · ")


def strip_html(value: str | None) -> str:
    """Remove HTML markup while preserving readable line structure."""
    if not value:
        return ""
    text = _SCRIPT_RE.sub(" ", value)
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return collapse_whitespace(text)


def collapse_whitespace(value: str | None) -> str:
    if not value:
        return ""
    text = _CTRL_RE.sub(" ", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def clean_text(value: str | None, *, max_length: int | None = None) -> str | None:
    """Sanitize free text. Returns ``None`` for empty/placeholder values.

    Returning ``None`` (rather than "") keeps unverifiable fields blank, as the
    guideline requires.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = strip_html(value) if "<" in value and ">" in value else collapse_whitespace(value)
    text = unicodedata.normalize("NFKC", text)
    if not text:
        return None
    if text.strip().lower() in {
        "n/a", "na", "none", "null", "unknown", "-", "--", "tbd", "todo", "?",
        "no description", "no description available", "description",
    }:
        return None
    if max_length is not None:
        text = truncate(text, max_length)
    return text


def truncate(value: str, max_length: int, suffix: str = "…") -> str:
    """Truncate on a word boundary when possible."""
    if len(value) <= max_length:
        return value
    cut = value[: max_length - len(suffix)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + suffix


def slugify(value: str | None) -> str | None:
    """ASCII slug suitable for IDs and filenames."""
    if not value:
        return None
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_ALNUM_RE.sub("-", text).strip("-")
    return slug or None


def _drop_tagline(name: str) -> str:
    """Trim a trailing directory tagline from a product name."""
    for separator in _TAGLINE_SEPARATORS:
        if separator in name:
            head = name.split(separator)[0].strip()
            if len(head) >= 2:
                return head
    return name


def canonical_name(
    value: str | None, *, drop_noise: bool = True, drop_tagline: bool = True
) -> str | None:
    """Return a comparison key for a product/company name.

    ``"Open AI"``, ``"OpenAI"`` and ``"OpenAI Inc."`` all canonicalize to
    ``"openai"``, which is what makes entity resolution possible without
    hard-coded alias tables.

    ``drop_tagline=False`` keeps everything after a separator such as ``" | "``.
    Trimming the tagline is right when the input *is* a name, but wrong when the
    input is a free-text haystack being searched for a name: a page title like
    ``"Free AI Math Solver Online | Tenorshare AI Math"`` brands itself in the
    half that the trim would discard.
    """
    if not value:
        return None
    text = clean_text(value)
    if not text:
        return None
    if drop_tagline:
        text = _drop_tagline(text)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[\u2122\u00ae\u00a9]", " ", text)
    tokens = [t for t in _NON_ALNUM_RE.split(text) if t]
    if drop_noise and len(tokens) > 1:
        filtered = [t for t in tokens if t not in _NAME_NOISE]
        if filtered:
            tokens = filtered
    joined = "".join(tokens)
    return joined or None


def name_tokens(value: str | None) -> set[str]:
    """Token set used for fuzzy name comparison."""
    if not value:
        return set()
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return {t for t in _NON_ALNUM_RE.split(text) if t and t not in _NAME_NOISE}


def similarity(a: str | None, b: str | None) -> float:
    """Character-level similarity in ``[0, 1]`` on canonicalized names."""
    ca, cb = canonical_name(a), canonical_name(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    return SequenceMatcher(None, ca, cb).ratio()


def token_set_ratio(a: str | None, b: str | None) -> float:
    """Jaccard-style token overlap; robust to word reordering."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    return intersection / len(ta | tb) if intersection else 0.0
