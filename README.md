# Project Trust V1 — Govt ID Verifier

AI-powered govt ID verification for **Lokal Matrimony**. Reads a Google Sheet of uploaded govt IDs, runs each through **Gemini Vision** for OCR + identity matching, and produces a CSV with verification tags.

> **V1 goal:** test whether seeding verified male profiles into the female feed improves female retention. This script processes the existing ~1,000-row backlog of govt IDs uploaded by male users.

---

## What it does

```
Google Sheet  →  Download image  →  Gemini Vision  →  Fuzzy match  →  CSV
  (1000 rows)    (parallel x10)     (OCR name+DOB)    (score 0-100)    (tagged)
```

### Output tags

| Tag | Meaning | Action |
|---|---|---|
| `VERIFIED` | Score ≥ 80, govt ID detected, name + DOB match | Badge live in male feed |
| `REVIEW` | Score 50–79, partial match | CS team manual check |
| `REJECTED` | Score < 50, real govt ID but doesn't match profile | No badge, user can appeal |
| `INVALID` | Image is not a govt ID (selfie, screenshot, blank) | User retries with correct upload |
| `ERROR` | Download or API failure | Re-run row |

---

## Setup

### 1. Clone and install

```bash
git clone <repo-url> project-trust-verifier
cd project-trust-verifier
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Google Sheets API credentials

Place the service account JSON at:
```
credentials/service_account.json
```

The service account email **must have Viewer access** to the sheet.
Current service account: `saathi-reader@saathi-sheets.iam.gserviceaccount.com`

### 3. Gemini API key

Get a key from <https://aistudio.google.com/apikey>, then:

```bash
cp .env.example .env
# edit .env and paste your key
```

### 4. Configure (optional)

`config.yaml` controls everything. Defaults are already set for the production sheet. Common tweaks:

```yaml
processing:
  row_limit: 10        # smoke-test on first 10 rows; null for full run
  max_workers: 10      # parallel API calls
gemini:
  model: gemini-2.5-flash-lite   # cheapest; use gemini-2.5-flash for higher accuracy
```

---

## Run

```bash
# Smoke test (set row_limit: 10 in config first)
python main.py

# Full run (set row_limit: null)
python main.py
```

**Expected runtime** for ~1000 rows at 10 parallel workers: ≈ 3–5 minutes.
**Expected cost** with Flash-Lite: ≈ ₹18 total.

---

## Output

After a successful run, `output/` contains:

### `verification_results.csv`

| Column | Description |
|---|---|
| `row_number` | Original row in sheet |
| `profile_id` | Lokal profile ID |
| `user_id` | Lokal user ID |
| `original_name` | Name from sheet |
| `original_dob` | DOB from sheet |
| `extracted_name` | Name OCR'd from ID by Gemini |
| `extracted_dob` | DOB OCR'd from ID by Gemini |
| `id_type` | PAN / AADHAAR / VOTER_ID / DRIVING_LICENSE / OTHER |
| `name_score` | Fuzzy match 0–100 |
| `dob_score` | 0 or 100 |
| `score` | Weighted total (70% name, 30% DOB) |
| `tag` | VERIFIED / REVIEW / REJECTED / INVALID / ERROR |
| `reason` | Human-readable explanation |
| `is_govt_id` | True if Gemini detected a valid govt ID |
| `image_url` | Original CDN URL |

### `summary.json`

```json
{
  "total_processed": 824,
  "tags": { "VERIFIED": 612, "REVIEW": 98, "REJECTED": 71, "INVALID": 35, "ERROR": 8 },
  "tag_percentages": { "VERIFIED": 74.27, "REVIEW": 11.89, ... }
}
```

---

## Scoring logic

```python
name_score = rapidfuzz.token_set_ratio(sheet_name, extracted_name)  # 0–100
dob_score  = 100 if normalise(sheet_dob) == normalise(extracted_dob) else 0

total = (name_score * 0.70) + (dob_score * 0.30)
```

**`token_set_ratio`** handles:
- Word-order swaps: `"Rajesh Kumar Sharma"` ↔ `"Sharma Rajesh Kumar"` → 100
- Initials vs full names: `"Rajesh K Sharma"` ↔ `"Rajesh Kumar Sharma"` → ~90
- Honorifics stripped automatically (`Mr.`, `Mrs.`, `Smt`, `Sri`, etc.)

**DOB** is normalised across `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`.

---

## Testing

```bash
python -m pytest tests/ -v
```

17 unit tests cover name normalisation, fuzzy matching, DOB parsing, and composite scoring.

---

## Known edge cases

| Edge case | Behaviour | Mitigation |
|---|---|---|
| Tamil/Telugu script in sheet, Latin on ID (or vice versa) | Low score → REJECTED or REVIEW | CS reviews manually; we transliterate to Latin in extraction |
| Image is a selfie not an ID | Gemini returns `is_govt_id: false` → INVALID | User retries (PRD Flow 4) |
| CDN URL returns 404 | Row tagged ERROR with reason | Re-run only those rows |
| Gemini rate limit / outage | Auto-retry 3x with backoff | Last resort: ERROR row, re-run |
| Recently-married woman, name on ID is maiden name | REJECTED → CS reviews on appeal | Acceptable for V1 (rare) |

---

## File structure

```
project-trust-verifier/
├── README.md
├── requirements.txt
├── config.yaml                  # tweak thresholds, workers, model
├── .env.example                 # copy to .env, add GEMINI_API_KEY
├── .gitignore
├── main.py                      # entry point
├── credentials/
│   └── service_account.json     # NEVER commit
├── output/
│   ├── verification_results.csv # CSV result
│   └── summary.json             # tag counts
├── src/
│   ├── sheets_client.py         # Google Sheets API
│   ├── image_fetcher.py         # download CDN images
│   ├── gemini_verifier.py       # Gemini Vision call + JSON parsing
│   ├── matcher.py               # fuzzy name + DOB scoring
│   └── tagger.py                # threshold → tag
└── tests/
    └── test_matcher.py          # 17 unit tests
```

---

## V1 → V2 roadmap (out of scope here)

- ❌ Auto-verify at signup (move from backlog → onboarding flow)
- ❌ Face matching (selfie ↔ ID photo)
- ❌ DigiLocker integration
- ❌ Aadhaar support (legal/UIDAI review pending)

This script handles only the V1 backlog cleanup.

---

**Owner:** Mohd Shamshul Qamar, PM — Lokal Matrimony
**Related docs:** Project Trust V1 PRD · Privacy & Compliance Note
