"""
matcher.py
Computes a fair similarity score between (sheet_name, sheet_dob) and
(extracted_name, extracted_dob) from Gemini.

Scoring (weighted, total = 100):
  - name_weight (default 70): rapidfuzz token-set ratio
  - dob_weight  (default 30): exact match after normalisation
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from typing import Optional, Tuple

from indic_transliteration import detect, sanscript
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

HONORIFICS = {"mr", "mrs", "miss", "ms", "shri", "smt", "sri", "dr", "er"}


def _strip_diacritics(s: str) -> str:
    """Remove combining accent marks left by transliteration (ā→a, ṣ→s)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def romanize(name: Optional[str]) -> str:
    """Transliterate an Indic-script name (Tamil/Telugu/Devanagari/etc.) to Latin.

    Indian govt IDs print names in Latin, but the sheet may store the native
    script. Without this, a Tamil sheet name vs a Latin PAN name scores ~0 even
    for the same person. Names already in Latin are returned untouched.
    """
    if not name:
        return ""
    # Pure ASCII/Latin → leave as-is (guard runs BEFORE detect, so English
    # names are never mis-detected as a romanisation scheme and mangled).
    if all(ord(c) < 128 for c in name):
        return name
    try:
        scheme = detect.detect(name)
        # detect returns Roman schemes (IAST/ITRANS/HK/SLP1) for Latin-ish input;
        # only transliterate genuine Indic scripts.
        indic = {
            sanscript.DEVANAGARI, sanscript.TAMIL, sanscript.TELUGU,
            sanscript.KANNADA, sanscript.MALAYALAM, sanscript.BENGALI,
            sanscript.GUJARATI, sanscript.GURMUKHI, sanscript.ORIYA,
        }
        if scheme not in indic:
            return name
        return _strip_diacritics(sanscript.transliterate(name, scheme, sanscript.IAST))
    except Exception as e:
        logger.warning(f"Romanisation failed for {name!r}: {e}")
        return name


def normalise_name(name: Optional[str]) -> str:
    """Romanise (if Indic), lowercase, strip honorifics, collapse whitespace."""
    if not name:
        return ""
    s = romanize(name).lower().strip()
    # Remove dots, special chars, but keep spaces and letters
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [t for t in s.split() if t not in HONORIFICS]
    return " ".join(tokens)


def score_names(sheet_name: Optional[str], extracted_name: Optional[str]) -> float:
    """
    Returns 0-100. Uses token_set_ratio — handles word order, initials,
    extra/missing middle names.
    """
    a = normalise_name(sheet_name)
    b = normalise_name(extracted_name)
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b)


DATE_FORMATS = [
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d %m %Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%B-%Y",
    "%d %B %Y",
]


def normalise_dob(dob: Optional[str]) -> Optional[str]:
    """Convert any common date format to YYYY-MM-DD. Returns None on failure."""
    if not dob:
        return None
    dob = dob.strip()
    # Strip time components if any
    dob = re.split(r"[T\s]", dob)[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(dob, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


_YEAR_RE = re.compile(r"^\d{4}$")


def _year_of(dob: Optional[str]) -> Optional[str]:
    """Best-effort birth year: from a full date, or a bare 4-digit year."""
    if not dob:
        return None
    dob = dob.strip()
    if _YEAR_RE.match(dob):
        return dob
    norm = normalise_dob(dob)
    return norm[:4] if norm else None


def score_dobs(sheet_dob: Optional[str], extracted_dob: Optional[str]) -> float:
    """
    Returns 0, 50, or 100.
      - 100: both sides are full dates and match exactly
      - 50 : the ID prints only a birth YEAR (no full date) and the year matches
             the sheet (partial credit — see config policy)
      - 0  : mismatch, or no parseable date on either side
    """
    a = normalise_dob(sheet_dob)
    b = normalise_dob(extracted_dob)
    if a and b:
        # Both are full dates → exact match required.
        return 100.0 if a == b else 0.0

    # One side has no full date (e.g. Aadhaar showing year-of-birth only).
    # Award partial credit when the birth years line up.
    ay, by = _year_of(sheet_dob), _year_of(extracted_dob)
    if ay and by:
        return 50.0 if ay == by else 0.0
    return 0.0


def composite_score(
    sheet_name: Optional[str],
    sheet_dob: Optional[str],
    extracted_name: Optional[str],
    extracted_dob: Optional[str],
    name_weight: int = 70,
    dob_weight: int = 30,
) -> Tuple[float, float, float]:
    """
    Returns (total_score, name_score, dob_score) on 0-100 scale.
    """
    if name_weight + dob_weight != 100:
        raise ValueError(
            f"name_weight + dob_weight must = 100, got {name_weight + dob_weight}"
        )
    name_s = score_names(sheet_name, extracted_name)
    dob_s = score_dobs(sheet_dob, extracted_dob)
    total = (name_s * name_weight + dob_s * dob_weight) / 100.0
    return round(total, 2), round(name_s, 2), round(dob_s, 2)
