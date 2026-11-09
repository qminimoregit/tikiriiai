"""Phone and reference numbers: which phone numbers are Sri Lankan, how numbers are shown to the model, and how they
are said (each language file's spoken_numbers words, or digits)."""

import re

_AMOUNT = re.compile(r"\bLKR\s*(\d{1,3}(?:,\d{3})+|\d+)")


def sri_lankan(raw: str) -> str:
    """A Sri Lankan phone number as its 10 digits (0771234567, landlines 0112345678), or "" if `raw` is not one.

    Takes +94, 0094 and 94 in front, spaces and dashes, and a 9-digit number said without its first 0 (771234567).
    Anything else is not a number staff can call back: 9 digits starting with 0 (call 63: 071320420), 7 digits (call
    57: 0450350), short codes, extensions and trunk names (ucmtrunk2)."""
    raw = (raw or "").strip()
    if not re.fullmatch(r"\+?[\d\s()\-]+", raw):
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0094"):
        digits = digits[4:]
    elif digits.startswith("94") and len(digits) == 11:
        digits = digits[2:]
    if len(digits) == 9 and digits[0] != "0":
        digits = "0" + digits
    return digits if re.fullmatch(r"0[1-9]\d{8}", digits) else ""


def display_phone(raw: str) -> str:
    """A Sri Lankan number as 077 123 4567 (landlines 011 234 5678); anything else is returned as it was."""
    digits = sri_lankan(raw)
    return f"{digits[:3]} {digits[3:6]} {digits[6:]}" if digits else (raw or "").strip()


def say_phone(raw: str, lang) -> str:
    """A phone number as the agent says it in `lang` (a languages.Language): in words if the language file has them,
    else grouped digits."""
    digits = sri_lankan(raw)
    words = lang.spoken_numbers.say(digits, lang.spoken_numbers.phone_groups) if digits else ""
    return words or display_phone(raw)


def with_spoken_amounts(text: str, lang) -> str:
    """`text` with the words of every 'LKR 119,990' added after it: 'LKR 119,990 (say: ...)'. The model read six-digit
    amounts wrongly when it turned them into words itself. Unchanged if the language file has no amount words."""
    def add(m: re.Match) -> str:
        words = lang.spoken_numbers.say_amount(int(m.group(1).replace(",", "")))
        return f"{m.group()} (say: {words})" if words else m.group()
    return _AMOUNT.sub(add, text)
