"""Comprehensive tests for Stage 3: Normalization and Validation Utilities."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from solution.normalization import (
    IST_TIMEZONE,
    is_blank_row,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    normalize_timestamp_to_utc,
    utc_to_ist_date,
    utc_to_ist_str,
)


# --- Header Normalization Tests ---

def test_normalize_header():
    assert normalize_header("customer_name") == "customer name"
    assert normalize_header("Insured_Name") == "insured name"
    assert normalize_header("  Mobile___No  ") == "mobile no"
    assert normalize_header("Policy Number") == "policy number"
    assert normalize_header("order__id") == "order id"
    assert normalize_header("Order   Date") == "order date"
    assert normalize_header("") == ""


# --- Blank Row Tests ---

def test_is_blank_row():
    assert is_blank_row(["", "   ", None, ""]) is True
    assert is_blank_row({"col1": "", "col2": "   ", "col3": None}) is True
    assert is_blank_row(["", "val", ""]) is False
    assert is_blank_row({"col1": " ", "col2": "0"}) is False


# --- Phone Normalization Tests ---

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("91-98765-43210", "+919876543210"),
        ("(+91) 9876543210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        ("6123456789", "+916123456789"),
        ("7123456789", "+917123456789"),
        ("8123456789", "+918123456789"),
        ("91-77187-50036", "+917718750036"),
    ],
)
def test_normalize_phone_valid(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "987654321",          # 9 digits
        "98765432100",        # 11 digits not starting with 0
        "5987654321",         # starts with 5 (only 6,7,8,9 valid)
        "1234567890",         # starts with 1
        "022-2845-1234",      # landline
        "+1 415 555 0134",    # US number
        "N/A",                # non-numeric
        "abc",                # letters
    ],
)
def test_normalize_phone_invalid(raw):
    with pytest.raises(ValueError) as exc_info:
        normalize_phone(raw)
    assert "invalid_phone" in str(exc_info.value)


def test_normalize_phone_empty():
    with pytest.raises(ValueError) as exc_info:
        normalize_phone("")
    assert "empty_phone" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        normalize_phone(None)
    assert "empty_phone" in str(exc_info.value)


# --- Date Normalization Tests ---

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-10-05", "2026-10-05"),
        ("05/10/2026", "2026-10-05"),
        ("5/10/2026", "2026-10-05"),
        ("05-10-2026", "2026-10-05"),
        ("05-Oct-2026", "2026-10-05"),
        ("01-dec-2026", "2026-12-01"),
        ("15-DEC-2026", "2026-12-15"),
        ("28-Feb-2024", "2024-02-28"),
        ("29-Feb-2024", "2024-02-29"),  # Leap year valid
    ],
)
def test_normalize_date_valid(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "31/02/2026",         # Impossible date: Feb 31
        "29/02/2025",         # Impossible date: non-leap Feb 29
        "2026-13-01",         # Impossible month 13
        "32-10-2026",         # Impossible day 32
        "soon",               # String
        "TBD",                # String
        "2026/10/05",         # Non-accepted delimiter combination
    ],
)
def test_normalize_date_invalid(raw):
    with pytest.raises(ValueError) as exc_info:
        normalize_date(raw)
    assert "invalid_date" in str(exc_info.value)


# --- Decimal Normalization Tests ---

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12000", 12000.0),
        ("12000.50", 12000.5),
        ("₹12,500.50", 12500.5),
        ("Rs. 4500", 4500.0),
        ("Rs 10,000", 10000.0),
        ("INR 12000.5", 12000.5),
        ("inr 500.25", 500.25),
        ("₹ 38,000.00", 38000.0),
        ("12345.678", 12345.68),  # Round to 2 places
        ("0", 0.0),
    ],
)
def test_normalize_decimal_valid(raw, expected):
    assert normalize_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "-500",               # Negative
        "₹ -100",             # Negative with symbol
        "N/A",                # Non-numeric
        "twelve thousand",    # Text
        "-",                  # Dash
    ],
)
def test_normalize_decimal_invalid(raw):
    with pytest.raises(ValueError) as exc_info:
        normalize_decimal(raw)
    assert "invalid_decimal" in str(exc_info.value)


# --- Enum Normalization Tests ---

def test_normalize_enum():
    langs = ["en", "hi", "ta", "kn", "te", "mr"]

    assert normalize_enum("en", langs) == "en"
    assert normalize_enum("HI", langs) == "hi"
    assert normalize_enum(" ta ", langs) == "ta"
    assert normalize_enum("Kn", langs) == "kn"

    with pytest.raises(ValueError):
        normalize_enum("Hindi", langs)

    with pytest.raises(ValueError):
        normalize_enum("english", langs)

    with pytest.raises(ValueError):
        normalize_enum("fr", langs)


# --- Timestamp Normalization Tests ---

def test_normalize_timestamp():
    # 1. UTC format (ending in Z)
    ts_utc = "2026-10-05T13:20:21Z"
    dt1 = normalize_timestamp_to_utc(ts_utc)
    assert dt1.tzinfo == timezone.utc
    assert dt1.year == 2026 and dt1.month == 10 and dt1.day == 5
    assert dt1.hour == 13 and dt1.minute == 20 and dt1.second == 21

    # 2. Offset format (+05:30)
    ts_offset = "2026-10-05T18:50:21+05:30"
    dt2 = normalize_timestamp_to_utc(ts_offset)
    assert dt2 == dt1, "18:50:21+05:30 must equal 13:20:21Z"

    # 3. Naive format -> Per spec: "Naive means IST."
    ts_naive = "2026-10-05 18:50:21"
    dt3 = normalize_timestamp_to_utc(ts_naive)
    assert dt3 == dt1, "Naive '2026-10-05 18:50:21' interpreted as IST must equal 13:20:21Z"

    # Conversion back to IST string and date
    assert utc_to_ist_str(dt1) == "2026-10-05T18:50:21+05:30"
    assert utc_to_ist_date(dt1) == "2026-10-05"

