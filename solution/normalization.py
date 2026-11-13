"""Data normalization and validation utilities for headers, phone numbers, dates, decimals, enums, and timestamps."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

# IST timezone: UTC+05:30 per specification
IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

# Phone regex: 10 digits starting with 6, 7, 8, 9
RE_VALID_PHONE10 = re.compile(r"^[6-9]\d{9}$")

# Month name mapping for case-insensitive DD-Mon-YYYY parsing
MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
}

# Currency prefix regex: ₹, Rs, Rs., INR (case-insensitive, optional trailing space)
RE_CURRENCY_PREFIX = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)


def normalize_header(header: str) -> str:
    """Normalizes a CSV header per specification:
    
    1. Lower-case it
    2. Trim it
    3. Replace every run of '_' or whitespace with a single space
    """
    if not header:
        return ""
    lowered = header.strip().lower()
    return re.sub(r"[\s_]+", " ", lowered)


def is_blank_row(row_values: list[Any] | dict[str, Any]) -> bool:
    """Returns True if every cell in the row is empty or whitespace."""
    if isinstance(row_values, dict):
        values = row_values.values()
    else:
        values = row_values
    return all(val is None or str(val).strip() == "" for val in values)


def normalize_phone(raw_value: str | None) -> str:
    """Normalizes and validates an Indian mobile phone number.
    
    Rules:
    - Strip every non-digit
    - If 12 digits starting with '91', drop the '91'
    - If 11 digits starting with '0', drop the '0'
    - Valid ONLY if exactly 10 digits and starts with 6, 7, 8, or 9
    - Store canonically as +91XXXXXXXXXX
    
    Raises ValueError("invalid_phone") on failure.
    """
    if raw_value is None or str(raw_value).strip() == "":
        raise ValueError("empty_phone")

    digits = re.sub(r"\D", "", str(raw_value).strip())

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if not RE_VALID_PHONE10.match(digits):
        raise ValueError("invalid_phone")

    return f"+91{digits}"


def normalize_date(raw_value: str | None) -> str:
    """Normalizes a date string to canonical ISO 'YYYY-MM-DD'.
    
    Accepts:
    - YYYY-MM-DD
    - DD/MM/YYYY (day-first)
    - DD-MM-YYYY (day-first)
    - DD-Mon-YYYY (e.g. 01-Dec-2026, month name case-insensitive)
    
    Raises ValueError("invalid_date") if format is unknown or date is impossible (e.g. 31/02).
    """
    if raw_value is None or str(raw_value).strip() == "":
        raise ValueError("empty_date")

    val = str(raw_value).strip()

    # Try YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", val):
        try:
            d = datetime.strptime(val, "%Y-%m-%d").date()
            return d.isoformat()
        except ValueError:
            raise ValueError("invalid_date")

    # Try DD/MM/YYYY
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", val):
        parts = val.split("/")
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        try:
            d = date(year, month, day)
            return d.isoformat()
        except ValueError:
            raise ValueError("invalid_date")

    # Try DD-MM-YYYY
    if re.match(r"^\d{1,2}-\d{1,2}-\d{4}$", val):
        parts = val.split("-")
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        try:
            d = date(year, month, day)
            return d.isoformat()
        except ValueError:
            raise ValueError("invalid_date")

    # Try DD-Mon-YYYY (case-insensitive month)
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", val)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2).lower()
        year = int(m.group(3))
        if mon_str not in MONTH_NAMES:
            raise ValueError("invalid_date")
        month = MONTH_NAMES[mon_str]
        try:
            d = date(year, month, day)
            return d.isoformat()
        except ValueError:
            raise ValueError("invalid_date")

    raise ValueError("invalid_date")


def normalize_decimal(raw_value: str | None) -> float:
    """Normalizes a decimal currency value.
    
    Rules:
    - Optionally prefixed by ₹, Rs, Rs., INR (case-insensitive, optional space)
    - Commas are thousands separators
    - Must be >= 0
    - Rounded to 2 decimal places
    
    Raises ValueError("invalid_decimal") on failure.
    """
    if raw_value is None or str(raw_value).strip() == "":
        raise ValueError("empty_decimal")

    val = str(raw_value).strip()

    # Strip currency prefix
    val = RE_CURRENCY_PREFIX.sub("", val).strip()

    # Remove thousands separators
    val = val.replace(",", "")

    try:
        dec = Decimal(val)
    except InvalidOperation:
        raise ValueError("invalid_decimal")

    if dec < Decimal("0"):
        raise ValueError("invalid_decimal")

    # Round to 2 decimal places
    rounded = round(float(dec), 2)
    return rounded


def normalize_enum(raw_value: str | None, allowed_values: list[str]) -> str:
    """Normalizes an enum value against allowed values (case-insensitive), storing lowercase.
    
    Raises ValueError("invalid_enum") if value is not in allowed_values.
    """
    if raw_value is None or str(raw_value).strip() == "":
        raise ValueError("empty_enum")

    val = str(raw_value).strip().lower()
    allowed_map = {str(a).lower(): str(a).lower() for a in allowed_values}

    if val not in allowed_map:
        raise ValueError("invalid_enum")

    return allowed_map[val]


def normalize_timestamp_to_utc(ts_str: str) -> datetime:
    """Parses an event timestamp in UTC, offset, or naive format, and normalizes to aware UTC datetime.
    
    Per specification:
    - UTC: ends with 'Z'
    - Offset: e.g. '+05:30'
    - Naive: e.g. '2026-10-03 10:01:00' -> Naive means IST (+05:30)
    """
    if not ts_str or not ts_str.strip():
        raise ValueError("empty_timestamp")

    s = ts_str.strip()

    # UTC format ending with Z
    if s.endswith("Z"):
        # Remove trailing Z and parse as UTC
        clean_s = s[:-1]
        dt = datetime.fromisoformat(clean_s).replace(tzinfo=timezone.utc)
        return dt

    # Check for offset with + or -
    # datetime.fromisoformat parses '+05:30' or '-04:00' directly if T or space separates date/time
    if "+" in s or ("-" in s[10:]):  # after the date portion
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    # Naive timestamp -> Specification rule: "Naive means IST."
    # Try parsing YYYY-MM-DD HH:MM:SS or ISO format
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TIMEZONE)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    raise ValueError(f"unparseable_timestamp: {ts_str}")


def utc_to_ist_str(utc_dt: datetime) -> str:
    """Converts a UTC datetime to an ISO-8601 string in IST (+05:30)."""
    ist_dt = utc_dt.astimezone(IST_TIMEZONE)
    return ist_dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")


def utc_to_ist_date(utc_dt: datetime) -> str:
    """Converts a UTC datetime to its IST calendar date 'YYYY-MM-DD'."""
    ist_dt = utc_dt.astimezone(IST_TIMEZONE)
    return ist_dt.strftime("%Y-%m-%d")

