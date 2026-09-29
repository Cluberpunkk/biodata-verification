"""
gemini_verifier.py
The verification "brain": a single, heavily-engineered Gemini prompt that does
EVERYTHING in one call —
  1. decides if the image is a valid Indian government ID
  2. extracts the cardholder's name + DOB (transliterating Indic scripts)
  3. scores the NAME match (0-100) and the DOB match (0-100) with full
     edge-case handling and worked examples
  4. picks a fixed-set review reason / rejection reason

Python only does the deterministic arithmetic (0.2*name + 0.8*dob) and the
80% threshold — all judgment lives in the prompt.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional

import google.generativeai as genai

from .image_fetcher import enhance_image_bytes, guess_mime_type

logger = logging.getLogger(__name__)

# ── The prompt. {profile_name} / {profile_dob} are injected per row. ──────────
PROMPT_TEMPLATE = """You are a SENIOR IDENTITY-VERIFICATION ANALYST for an Indian matrimony platform.
Your job: decide whether an uploaded document is a GENUINE Indian government photo ID, and whether it
belongs to the user — by comparing it against the details the user entered when creating their profile.

You are given:
- IMAGE: the uploaded document.
- PROFILE_NAME (what the user entered): "{profile_name}"
- PROFILE_DOB  (what the user entered): "{profile_dob}"   [format YYYY-MM-DD; may be blank]

Work through these steps precisely and return ONLY the JSON described at the end.

────────────────────────────────────────────────────────
STEP 1 — IS IT A VALID GOVERNMENT ID?
A VALID government ID is EXACTLY ONE of these Indian documents:
  • Aadhaar card   • PAN card   • Driving License   • Voter ID (EPIC)   • Passport
NOT valid: selfies, personal/family photos, screenshots, random images, blank/black images,
bank/debit/credit cards, marksheets, or anything unreadable.
→ If it is NOT a valid government ID: set "is_valid_govt_id": false, write a SHORT "rejection_reason"
  (e.g. "Selfie — not a government ID", "Screenshot — not a government ID", "Not a government ID",
  "Image unreadable"), set name_score and dob_score to 0, other fields null, and STOP.

────────────────────────────────────────────────────────
STEP 1B — CLEAR, COMPLETE & READABLE?  (quality / completeness gate)
COMPLETENESS: the upload MUST be the COMPLETE FRONT of the ID showing the cardholder's NAME and
DATE OF BIRTH TOGETHER. If it is a PARTIAL or CROPPED fragment — e.g. only the DOB line/section,
only a strip or corner, the NAME cropped out or absent, a zoom-in of a single field, or the BACK
side — then it is NOT acceptable: set "is_clear": false and "rejection_reason":
  "We couldn't read your ID clearly. Re-upload a clear photo of the front."
  Set extracted_name and extracted_dob to null, name_score and dob_score to 0, and STOP.
Never verify from a fragment that does not show the full front with BOTH name and DOB.
Even a GENUINE, COMPLETE government ID must also be CLEAR enough to trust. If the image is blurred, out of focus,
low-resolution, pixelated, has heavy glare/reflection, is OVEREXPOSED / WASHED-OUT / FADED / very low contrast, is too dark, or is cropped so that the printed
NAME or DATE OF BIRTH cannot be read with CONFIDENCE (EITHER one being illegible is enough):
→ Do NOT guess or infer a value you cannot actually read. set "is_clear": false and "rejection_reason":
  "We couldn't read your ID clearly. Re-upload a clear photo of the front."
  Set extracted_name and extracted_dob to null, name_score and dob_score to 0, and STOP.
  (You may still fill id_type if the document type is identifiable.)
Only continue if you can read the name and DOB clearly → set "is_clear": true.

────────────────────────────────────────────────────────
STEP 2 — EXTRACT DETAILS (only if valid and clear)
• extracted_name: the CARDHOLDER's full name exactly as printed. Prefer the cardholder — NEVER the
  father's / guardian's / spouse's name. If printed in an Indic script (Devanagari, Tamil, Telugu,
  Kannada, Malayalam, Bengali, Gujarati, etc.), TRANSLITERATE it into English/Latin letters.
• extracted_dob: normalise to YYYY-MM-DD. If the card prints ONLY a year of birth (common on Aadhaar),
  return just "YYYY".
• Verification REQUIRES a clearly legible cardholder name. If the name text is blurred, faded,
  or otherwise not clearly readable, treat it as illegible → go back to STEP 1B and reject
  (do NOT verify on the DOB alone).

────────────────────────────────────────────────────────
STEP 3 — SCORE THE NAME MATCH (integer 0-100)   [carries 50% weight]
CONTEXT MATCHING — judge whether the two names refer to the SAME PERSON, NOT whether the strings are
identical. Indian names vary a lot between what a user types on a profile and what is printed on an ID
(initials vs full form, expansions, surname-first order, dropped middle names, father's name appended,
and spelling/transliteration differences). GIVE THE BENEFIT OF THE DOUBT when they are plausibly the
same person — the goal is to not wrongly hold back genuine users.
IGNORE: case, honorifics (Mr, Mrs, Ms, Smt, Sri, Shri, Dr, S/o, D/o, W/o), punctuation, extra spaces,
AND treat an initial as equivalent to any full name starting with that letter ("R" ≡ "Rahul"/"Rajesh").
Rubric:
  100    = same name (identical after ignoring the above)
  90-99  = clearly the SAME PERSON, e.g.:
             • initial ↔ full name: "R Sharma" ≡ "Rahul Sharma"; "S Saravanan" ≡ "Saravanan S";
               "A R Rahman" ≡ "Allah Rakha Rahman"
             • word-order / surname-first: "Rajesh Kumar Sharma" ≡ "Sharma Rajesh Kumar"
             • dropped or extra middle name: "Rahul Kumar Sharma" ≡ "Rahul Sharma"
             • spelling / transliteration variants: "Mohammed"≡"Mohammad", "Shivshankar"≡"Siva Sankar",
               "Lakshmi"≡"Laxmi", "Syed"≡"Sayyad"
  75-89  = strong partial: one full token matches and the other is only an initial or is missing
             ("Rahul Sharma" ↔ "Rahul"; "Priya Devi" ↔ "Priya"), OR first name matches and surname is
             a near-variant
  50-74  = one clear name component matches but the other(s) genuinely differ
  20-49  = only a very common/weak component overlaps
  1-19   = largely different
  0      = completely different person, or name unreadable
Worked examples:
  "Rahul Sharma"        vs "R SHARMA"          → 96
  "Rahul Sharma"        vs "RAHUL"             → 80
  "Saravanan S"         vs "S SARAVANAN"        → 100
  "Rajesh Kumar Sharma" vs "Sharma Rajesh"      → 92
  "Mohammed Irfan"      vs "MOHAMMAD IRFAAN"     → 95
  "Lakshmi Priya"       vs "LAXMIPRIYA"          → 96
  "Priya Nair"          vs "Priya Menon"         → 55
  "Suresh Babu"         vs "Ramesh Babu"         → 40
  "Arun Kumar"          vs "Vijay Singh"         → 0

────────────────────────────────────────────────────────
STEP 4 — SCORE THE DOB MATCH (integer 0-100)   [carries 50% weight]
Compare PROFILE_DOB with extracted_dob. Reward genuine matches and near-matches (OCR errors, year-only
cards), but still catch the fraud pattern where the year matches yet the day+month were clearly changed.
Rubric:
  100    = EXACT match (same day, month AND year)
  88-95  = card shows YEAR ONLY and that year equals PROFILE_DOB's year (legitimate ID limitation)
  65-80  = OCR-style discrepancy that still implies the SAME date: one digit off, OR a day↔month
           transposition ("12/05/1990" vs "05/12/1990")
  20-40  = YEAR matches but day AND month are genuinely different (NOT a transposition).
           ⚠ FRAUD PATTERN: user changed day/month to fake an approximately-similar age. Score LOW.
  0      = year differs, DOB unreadable/absent, or clearly a different date
Examples:
  1994-03-12 vs 1994-03-12         → 100
  1994-03-12 vs "1994" (year only) → 90
  1994-03-12 vs 1994-12-03         → 72   (transposed day/month)
  1994-03-12 vs 1994-08-25         → 30   (same year, different day+month → suspicious)
  1994-03-12 vs 1988-03-12         → 0    (year differs)
If PROFILE_DOB is blank, set dob_score = 0.

────────────────────────────────────────────────────────
STEP 5 — REVIEW REASON (only relevant if the ID is valid)
Pick the SINGLE best-fitting value from this FIXED set (used when the case needs manual review):
  • "Poor image quality / improper lighting"
  • "Name and/or DOB do not fully match"
  • "Partial match — needs manual confirmation"

────────────────────────────────────────────────────────
RETURN ONLY THIS JSON (no markdown fences, no commentary):
{{
  "is_valid_govt_id": true | false,
  "is_clear": true | false,
  "id_type": "AADHAAR" | "PAN" | "DRIVING_LICENSE" | "VOTER_ID" | "PASSPORT" | null,
  "extracted_name": "<name in Latin script, or null>",
  "extracted_dob": "<YYYY-MM-DD or YYYY, or null>",
  "name_score": <integer 0-100>,
  "dob_score": <integer 0-100>,
  "name_reasoning": "<one short sentence>",
  "dob_reasoning": "<one short sentence>",
  "review_reason": "<one value from STEP 5, or null>",
  "rejection_reason": "<short reason if not a valid govt id, else null>",
  "confidence_notes": "<one short sentence on image quality/clarity, or null>"
}}"""


class GeminiVerifier:
    def __init__(self, api_key: str, model_name: str, max_retries: int = 3, timeout: int = 30):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model_name)
        self.max_retries = max_retries
        self.timeout = timeout
        logger.info(f"Initialised Gemini model: {model_name}")

    def verify(self, image_bytes: bytes, profile_name: str, profile_dob: str) -> Dict[str, Any]:
        """Full verification for one image + the profile's claimed name/DOB."""
        if not image_bytes:
            return self._error_result("No image bytes provided")

        result = self._extract(image_bytes, profile_name, profile_dob)

        # Salvage: API rejected the file (corrupt/odd encoding) → re-encode & retry.
        if result.get("error"):
            reencoded = enhance_image_bytes(image_bytes)
            if reencoded:
                logger.info("Extraction errored — retrying on re-encoded image")
                salvaged = self._extract(reencoded, profile_name, profile_dob)
                if salvaged.get("error") is None:
                    result = salvaged

        # NOTE: no blur/low-res "rescue" pass — an ID that cannot be read clearly
        # must be REJECTED (see STEP 1B), not enhanced into acceptance.
        return result

    def _extract(self, image_bytes: bytes, profile_name: str, profile_dob: str) -> Dict[str, Any]:
        """Single Gemini call (with retry/backoff on transient failures)."""
        mime = guess_mime_type(image_bytes)
        prompt = PROMPT_TEMPLATE.format(
            profile_name=(profile_name or "").strip(),
            profile_dob=(profile_dob or "").strip(),
        )

        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.model.generate_content(
                    [prompt, {"mime_type": mime, "data": image_bytes}],
                    generation_config={
                        "temperature": 0.0,
                        "response_mime_type": "application/json",
                        "max_output_tokens": 2048,
                    },
                )
                text = (response.text or "").strip()
                parsed = self._parse_json(text)
                if parsed is None:
                    raise ValueError(f"Could not parse JSON from Gemini response: {text[:200]}")
                return self._normalise(parsed)
            except Exception as e:
                last_err = e
                logger.warning(f"Gemini attempt {attempt}/{self.max_retries} failed: {e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)

        return self._error_result(f"All retries failed: {last_err}")

    @staticmethod
    def _normalise(parsed: Dict[str, Any]) -> Dict[str, Any]:
        def clamp(v):
            try:
                return max(0, min(100, int(round(float(v)))))
            except (TypeError, ValueError):
                return 0
        return {
            "is_valid_govt_id": bool(parsed.get("is_valid_govt_id", False)),
            "is_clear": bool(parsed.get("is_clear", True)),
            "id_type": parsed.get("id_type"),
            "extracted_name": parsed.get("extracted_name"),
            "extracted_dob": parsed.get("extracted_dob"),
            "name_score": clamp(parsed.get("name_score", 0)),
            "dob_score": clamp(parsed.get("dob_score", 0)),
            "name_reasoning": parsed.get("name_reasoning"),
            "dob_reasoning": parsed.get("dob_reasoning"),
            "review_reason": parsed.get("review_reason"),
            "rejection_reason": parsed.get("rejection_reason"),
            "confidence_notes": parsed.get("confidence_notes"),
            "error": None,
        }

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE).strip()

        def _coerce(obj):
            # Some models wrap the object in a list — take the first dict.
            if isinstance(obj, list):
                obj = next((x for x in obj if isinstance(x, dict)), None)
            return obj if isinstance(obj, dict) else None

        try:
            return _coerce(json.loads(text))
        except json.JSONDecodeError:
            pass
        # Salvage: extract the first balanced {...} object (handles trailing
        # prose or a truncated tail after a complete object).
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return _coerce(json.loads(text[start:i + 1]))
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _error_result(msg: str) -> Dict[str, Any]:
        return {
            "is_valid_govt_id": False, "is_clear": False, "id_type": None,
            "extracted_name": None, "extracted_dob": None,
            "name_score": 0, "dob_score": 0,
            "name_reasoning": None, "dob_reasoning": None,
            "review_reason": None, "rejection_reason": None,
            "confidence_notes": None, "error": msg,
        }
