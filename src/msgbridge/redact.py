"""Masks applied before text enters the index, so it never reaches a tool response."""
import re

CODE_WORDS = re.compile(r"\b(code|passcode|verification|verify|otp|one[- ]time|pin|security|2fa|login)\b", re.I)
SHORT_NUMBER = re.compile(r"(?<![\d-])(\d{4,8}|\d{3}[- ]\d{3})(?![\d-])")
CARD_LIKE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _mask_card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return f"[redacted card ending {digits[-4:]}]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


def redact(body: str, from_business: bool) -> str:
    body = CARD_LIKE.sub(_mask_card, body)
    body = SSN.sub("[redacted number]", body)
    if from_business and CODE_WORDS.search(body):
        body = SHORT_NUMBER.sub("[code]", body)
    return body
