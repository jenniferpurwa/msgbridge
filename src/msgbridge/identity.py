"""Normalize phone numbers and emails so every source agrees on one key per handle."""
import re

import phonenumbers


def normalize(value: str, region: str = "US") -> str:
    """Return a stable key: lowercase email, E.164 phone, or bare digits for short codes."""
    v = (value or "").strip()
    if "@" in v:
        return v.lower()
    digits = re.sub(r"\D", "", v)
    if not digits:
        return v.lower()  # alphanumeric sender such as a bank name
    if len(digits) <= 6:
        return digits
    try:
        n = phonenumbers.parse(v, region)
        if phonenumbers.is_possible_number(n):
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return ("+" if v.startswith("+") else "") + digits


def is_business(handle: str) -> bool:
    """Short codes (5-6 digits) and alphanumeric sender IDs, i.e. not a person."""
    h = (handle or "").strip()
    if "@" in h:
        return False
    digits = re.sub(r"\D", "", h)
    return bool((digits and len(digits) <= 6 and h.isdigit()) or (not digits and h))


def pretty(handle: str, region: str = "US") -> str:
    """Readable label for a handle with no contact card."""
    if "@" in handle:
        return handle
    try:
        n = phonenumbers.parse(handle, region)
        if phonenumbers.is_possible_number(n):
            fmt = (phonenumbers.PhoneNumberFormat.NATIONAL
                   if phonenumbers.region_code_for_number(n) == region
                   else phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            return phonenumbers.format_number(n, fmt)
    except phonenumbers.NumberParseException:
        pass
    return handle
