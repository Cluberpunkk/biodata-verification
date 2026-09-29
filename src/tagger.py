"""
tagger.py
Turns Gemini's structured result + the weighted overall score into the final
3-category tag and a human-readable reason. All scoring judgment lives in the
Gemini prompt; this module only applies the deterministic threshold.

  VERIFIED     → valid govt ID AND overall >= verified_min
  UNDER_REVIEW → valid govt ID AND overall <  verified_min  (CS decides)
  REJECTED     → not a valid govt ID / no usable image      (short reason)
"""

from __future__ import annotations

from typing import Any, Dict

# Two user-facing rejection buckets shown on the "Verification failed" screen.
# Every detailed rejection reason collapses into exactly one of these.
REJECT_NOT_ID = "This isn't a valid government ID. Upload your PAN, Aadhaar, Voter ID, DL or Passport."
REJECT_UNCLEAR = "We couldn't read your ID clearly. Re-upload a clear photo of the front."

# Keywords that route a rejection to the "unclear / missing / incomplete" bucket;
# anything else (selfie, screenshot, non-accepted card, etc.) is "not a valid ID".
_UNCLEAR_KEYS = (
    "no government id", "blank", "not uploaded", "download", "unreadable", "corrupt",
    "unclear", "blurr", "low quality", "poor quality", "process", "back of", "back side",
    "incomplete", "missing", "not visible", "no personal details", "no identity", "only the back",
    "clear photo", "read your id", "couldn't read",
)


def final_rejection_reason(detailed: str, has_image: bool = True) -> str:
    """Collapse a detailed rejection reason into one of the 2 user-facing buckets."""
    s = (detailed or "").lower()
    if not has_image or any(k in s for k in _UNCLEAR_KEYS):
        return REJECT_UNCLEAR
    return REJECT_NOT_ID


def assign_tag(gemini: Dict[str, Any], overall: float, verified_min: int = 80) -> str:
    if gemini.get("error") or not gemini.get("is_valid_govt_id") or not gemini.get("is_clear", True):
        return "REJECTED"
    return "VERIFIED" if overall >= verified_min else "UNDER_REVIEW"


def tag_reason(tag: str, overall: float, gemini: Dict[str, Any]) -> str:
    """Short explanation for the reason column."""
    if tag == "REJECTED":
        return (gemini.get("rejection_reason")
                or gemini.get("error")
                or "Not a valid government ID — please upload a clear PAN/Aadhaar/Voter ID/DL/Passport.")

    id_type = gemini.get("id_type") or "govt ID"
    name_r = (gemini.get("name_reasoning") or "").strip().rstrip(".")
    dob_r = (gemini.get("dob_reasoning") or "").strip().rstrip(".")
    detail = "; ".join(p for p in (name_r, dob_r) if p)

    if tag == "VERIFIED":
        return f"{id_type} verified. {detail}. Score {overall} → VERIFIED."
    # UNDER_REVIEW
    review = gemini.get("review_reason") or "Partial match — needs manual confirmation"
    return f"{id_type}. {detail}. Score {overall} → UNDER REVIEW ({review})."
