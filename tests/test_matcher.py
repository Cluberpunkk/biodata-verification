"""
Tests for matcher.py — name fuzzy matching + DOB normalisation.
Run with: python -m pytest tests/
"""
from src.matcher import (
    composite_score,
    normalise_dob,
    normalise_name,
    score_dobs,
    score_names,
)


# ── Name normalisation ─────────────────────────────────────────
def test_normalise_strips_honorifics():
    assert normalise_name("Mr. Rajesh Kumar") == "rajesh kumar"
    assert normalise_name("Smt Priya Sharma") == "priya sharma"


def test_normalise_collapses_whitespace():
    assert normalise_name("Rajesh   Kumar") == "rajesh kumar"


# ── Name scoring ───────────────────────────────────────────────
def test_exact_name_match():
    assert score_names("Rajesh Kumar", "Rajesh Kumar") == 100


def test_word_order_shuffle():
    # token_set_ratio handles "Kumar Rajesh" vs "Rajesh Kumar"
    assert score_names("Rajesh Kumar Sharma", "Sharma Rajesh Kumar") >= 95


def test_initial_vs_full_name():
    # "Rajesh K Sharma" vs "Rajesh Kumar Sharma" — partial credit
    score = score_names("Rajesh K Sharma", "Rajesh Kumar Sharma")
    assert score >= 70  # not perfect but decent


def test_completely_different_names():
    assert score_names("Rajesh Kumar", "Priya Sharma") < 50


def test_empty_name_returns_zero():
    assert score_names(None, "Rajesh") == 0
    assert score_names("Rajesh", "") == 0


# ── DOB normalisation ──────────────────────────────────────────
def test_normalise_dob_iso():
    assert normalise_dob("1993-09-13") == "1993-09-13"


def test_normalise_dob_dd_mm_yyyy_slash():
    assert normalise_dob("13/09/1993") == "1993-09-13"


def test_normalise_dob_dd_mm_yyyy_dash():
    assert normalise_dob("13-09-1993") == "1993-09-13"


def test_normalise_dob_dots():
    assert normalise_dob("13.09.1993") == "1993-09-13"


def test_normalise_dob_invalid_returns_none():
    assert normalise_dob("not a date") is None
    assert normalise_dob("") is None


# ── DOB scoring ────────────────────────────────────────────────
def test_dob_exact_match():
    assert score_dobs("1993-09-13", "13/09/1993") == 100


def test_dob_mismatch():
    assert score_dobs("1993-09-13", "1994-09-13") == 0


# ── Composite ──────────────────────────────────────────────────
def test_composite_perfect_match():
    total, name_s, dob_s = composite_score(
        "Rajesh Kumar", "1993-09-13",
        "Rajesh Kumar", "1993-09-13",
    )
    assert total == 100
    assert name_s == 100
    assert dob_s == 100


def test_composite_name_only():
    total, name_s, dob_s = composite_score(
        "Rajesh Kumar", "1993-09-13",
        "Rajesh Kumar", "1990-01-01",
    )
    # 100*0.70 + 0*0.30 = 70
    assert total == 70.0
    assert dob_s == 0


def test_composite_dob_only():
    total, name_s, dob_s = composite_score(
        "Rajesh Kumar", "1993-09-13",
        "Completely Different", "1993-09-13",
    )
    # name_score will be low, dob = 100; total = low*0.7 + 100*0.3
    assert dob_s == 100
    assert total >= 30  # at least the DOB weight
