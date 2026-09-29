"""
main.py
Project Trust V1 — Govt ID Verification Runner

Pipeline:
  1. Read Google Sheet
  2. Dedupe by profile_id (keep most recent occurrence)
  3. For each row in parallel:
     a. Download govt ID image from URL
     b. Send image to Gemini Vision
     c. Score against sheet's name/DOB
     d. Assign tag (VERIFIED / REVIEW / REJECTED / INVALID / ERROR)
  4. Write CSV + JSON summary
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv
from tqdm import tqdm

from src.gemini_verifier import GeminiVerifier
from src.image_fetcher import fetch_image_bytes
from src.sheets_client import SheetsClient
from src.tagger import assign_tag, final_rejection_reason, tag_reason

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("project-trust")

# Quiet noisy loggers
logging.getLogger("googleapiclient").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────────────────
def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_env() -> str:
    load_dotenv()
    key = os.getenv("GEMINI_API_KEY")
    if not key or key == "your_gemini_api_key_here":
        sys.exit(
            "✗ GEMINI_API_KEY not set in .env\n"
            "  Get a key at https://aistudio.google.com/apikey"
        )
    return key


# ──────────────────────────────────────────────────────────────
# Dedupe
# ──────────────────────────────────────────────────────────────
def dedupe_rows(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """Keep the LAST occurrence of each key (most recent in sheet order)."""
    seen: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        k = str(row.get(key, "")).strip()
        if not k:
            continue
        seen[k] = row
    return list(seen.values())


# ──────────────────────────────────────────────────────────────
# Per-row processing
# ──────────────────────────────────────────────────────────────
def build_result(
    row: Dict[str, Any],
    gemini: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the CSV row from Gemini's result.

    Gemini supplies name_score & dob_score (all judgment). Python only computes
    the weighted overall (20% name / 80% DOB) and applies the 80% threshold.
    """
    scoring_cfg = cfg["scoring"]
    nw, dw = scoring_cfg["name_weight"], scoring_cfg["dob_weight"]
    name_s = gemini.get("name_score", 0)
    dob_s = gemini.get("dob_score", 0)
    overall = round((name_s * nw + dob_s * dw) / 100.0, 2)

    tag = assign_tag(gemini, overall, verified_min=scoring_cfg["verified_min"])
    reason = tag_reason(tag, overall, gemini)
    review_reason = gemini.get("review_reason") if tag == "UNDER_REVIEW" else ""
    rejected_final_reason = (
        final_rejection_reason(reason, has_image=bool(row.get("image_url", "").strip()))
        if tag == "REJECTED" else ""
    )

    return {
        "row_number": row.get("_row_number"),
        "profile_id": row.get("profile_id"),
        "user_id": row.get("user_id"),
        "original_name": row.get("name", "").strip(),
        "original_dob": row.get("date_of_birth", "").strip(),
        "extracted_name": gemini.get("extracted_name") or "",
        "extracted_dob": gemini.get("extracted_dob") or "",
        "id_type": gemini.get("id_type") or "",
        "name_score": name_s,
        "dob_score": dob_s,
        "score": overall,
        "tag": tag,
        "reason": reason,
        "review_reason": review_reason or "",
        "rejected_final_reason": rejected_final_reason,
        "is_valid_govt_id": gemini.get("is_valid_govt_id"),
        "image_url": row.get("image_url", "").strip(),
    }


def process_row(
    row: Dict[str, Any],
    verifier: GeminiVerifier,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Download + Gemini verify + tag for one row → CSV-ready dict."""
    image_url = row.get("image_url", "").strip()
    image_bytes = fetch_image_bytes(image_url)
    if not image_bytes:
        gemini = {
            "is_valid_govt_id": False, "id_type": None,
            "extracted_name": None, "extracted_dob": None,
            "name_score": 0, "dob_score": 0,
            "review_reason": None, "confidence_notes": None,
            "rejection_reason": ("No government ID uploaded (blank in sheet)"
                                 if not image_url else "Image could not be downloaded"),
            "error": None,
        }
    else:
        gemini = verifier.verify(image_bytes, row.get("name", ""), row.get("date_of_birth", ""))

    return build_result(row, gemini, cfg)


# ──────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────
CSV_COLUMNS = [
    "row_number", "profile_id", "user_id",
    "original_name", "original_dob",
    "extracted_name", "extracted_dob", "id_type",
    "name_score", "dob_score", "score",
    "tag", "reason", "review_reason", "rejected_final_reason",
    "is_valid_govt_id", "image_url",
]


def write_csv(results: List[Dict[str, Any]], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(results)
    logger.info(f"✓ Wrote CSV: {path} ({len(results)} rows)")


def write_summary(results: List[Dict[str, Any]], path: str) -> None:
    tag_counts = Counter(r["tag"] for r in results)
    total = len(results)
    summary = {
        "total_processed": total,
        "tags": dict(tag_counts),
        "tag_percentages": {
            t: round(100 * c / total, 2) for t, c in tag_counts.items()
        } if total else {},
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info(f"✓ Wrote summary: {path}")
    print("\n" + "=" * 60)
    print(" RESULTS SUMMARY")
    print("=" * 60)
    for tag, count in tag_counts.most_common():
        pct = 100 * count / total if total else 0
        print(f"  {tag:10s} {count:>4d}  ({pct:5.1f}%)")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("Project Trust V1 — Govt ID Verifier")
    cfg = load_config()
    api_key = load_env()

    # 1. Read sheet
    sheets = SheetsClient(cfg["google_sheets"]["credentials_path"])
    raw_rows = sheets.read_range(
        cfg["google_sheets"]["sheet_id"],
        cfg["google_sheets"]["range"],
    )
    rows = sheets.rows_to_dicts(raw_rows, cfg["columns"])
    logger.info(f"Loaded {len(rows)} rows from sheet")

    # 2. Dedupe
    dedupe_key = cfg["processing"]["dedupe_by"]
    rows = dedupe_rows(rows, dedupe_key)
    logger.info(f"After dedupe by '{dedupe_key}': {len(rows)} rows")

    # 3. Apply test limit if set
    limit = cfg["processing"].get("row_limit")
    if limit:
        rows = rows[:limit]
        logger.info(f"⚠ Test mode: processing only first {limit} rows")

    # 4. Init Gemini
    verifier = GeminiVerifier(
        api_key=api_key,
        model_name=cfg["gemini"]["model"],
        max_retries=cfg["gemini"]["max_retries"],
        timeout=cfg["gemini"]["timeout_seconds"],
    )

    # 5. Parallel processing
    results: List[Dict[str, Any]] = []
    max_workers = cfg["processing"]["max_workers"]

    logger.info(f"Processing {len(rows)} rows with {max_workers} workers…")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(process_row, row, verifier, cfg): row
            for row in rows
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Verifying"):
            try:
                results.append(fut.result())
            except Exception as e:
                row = futures[fut]
                logger.error(
                    f"Row {row.get('_row_number')} crashed: {e}"
                )
                results.append({
                    "row_number": row.get("_row_number"),
                    "profile_id": row.get("profile_id"),
                    "user_id": row.get("user_id"),
                    "original_name": row.get("name", ""),
                    "original_dob": row.get("date_of_birth", ""),
                    "extracted_name": "",
                    "extracted_dob": "",
                    "id_type": "",
                    "name_score": 0,
                    "dob_score": 0,
                    "score": 0,
                    "tag": "REJECTED",
                    "reason": f"Processing error: {e}",
                    "review_reason": "",
                    "rejected_final_reason": final_rejection_reason("processing error", has_image=False),
                    "is_valid_govt_id": False,
                    "image_url": row.get("image_url", ""),
                })

    # Sort by original row order for clean output
    results.sort(key=lambda r: r.get("row_number") or 0)

    # 6. Write outputs
    write_csv(results, cfg["output"]["csv_path"])
    write_summary(results, cfg["output"]["summary_path"])


if __name__ == "__main__":
    main()
