"""The languages the agent speaks: one file each in languages/ (si.yaml, ta.yaml, en.yaml).

Every file has the same keys (the Language model below). Database-free: greetings, speaking rules, the lines the
agent says word for word, goodbye phrases, speech-to-text codes, the SMS wording and the way phone numbers are said
all come from here. A call speaks exactly one of these languages (the caller picks it in the language menu). A missing, misspelt or extra key, or an unknown {placeholder}, stops the app at startup with the
file and the key named. Adding a language means adding one file (and its code to the admin schemas).
"""

from __future__ import annotations

import re
import string
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

LANG_DIR = Path(__file__).resolve().parent.parent / "languages"


class LanguageFileError(RuntimeError):
    pass


class _Lines(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*")
    @classmethod
    def _not_empty(cls, v):
        if isinstance(v, str) and not v.strip():
            raise ValueError("must not be empty")
        return v


class CommonLines(_Lines):
    """Lines every conversation uses. The agent says each one exactly as written."""

    ask_phone: str
    read_back_phone: str  # {phone}
    phone_ten_digits: str
    ask_new_phone: str
    which_detail_wrong: str
    please_repeat: str
    sorry_wrong: str  # said once when the caller says the agent's answer was wrong
    anything_else: str
    no_information: str
    out_of_scope: str  # {company}
    other_language: str  # a caller asks to switch language: the call stays in this one
    still_there: str
    no_response: str
    wrap_up: str
    goodbye: str


class SendLines(_Lines):
    """Sending details to the caller's phone. The tool results carry these as ready-made `say` lines."""

    offer: str  # the agent's offer, after answering
    ask_mobile: str
    use_caller_id: str  # {phone}
    not_mobile: str
    sent: str
    not_set_up: str
    nothing_to_send: str


class LeadLines(_Lines):
    """Booking a demo (book_demo). The agent says each one exactly as written."""

    offer: str  # the offer to book a free demo with the team
    ask_name: str
    ask_business: str
    ask_time: str  # when the team should call them back for the demo
    use_caller_id: str  # {phone}: may the team call back on the caller-ID number
    confirm_details: str  # {name} {business} {time} {phone}: read back before the request is saved
    booked: str  # book_demo saved the request
    booked_sms: str  # ... and a confirmation SMS is on its way to the caller
    updated: str  # a correction later in the call updated the saved request
    updated_sms: str  # ... and the corrected confirmation SMS is on its way
    already_booked: str  # book_demo again in the same call with nothing changed


class SmsLines(_Lines):
    """The text of the message the caller receives. Built in Python from the snapshot, never by the model."""

    intro: str  # {company}
    overview_title: str
    capabilities_title: str
    industry_title: str  # {industry}
    contact_title: str
    website_line: str  # {url}
    email_line: str  # {email}
    phone_line: str  # {phone}
    footer: str  # e.g. how to book a demo
    demo_booked: str  # {company} {name}: first line of the booking confirmation SMS
    demo_updated: str  # {company} {name}: first line when a correction updated the booking
    demo_business: str  # {business}
    demo_time: str  # {time}: when the team will call
    demo_footer: str
    weekdays: list[str]  # Monday to Sunday, for the date the agent is told


    @field_validator("weekdays")
    @classmethod
    def _seven(cls, v):
        if len(v) != 7 or not all(str(x).strip() for x in v):
            raise ValueError("needs exactly 7 weekday names, Monday first, none empty")
        return [str(x).strip() for x in v]


class LanguageLines(_Lines):
    """Names of the languages, each written in this language."""

    names: dict[str, str]  # language code -> its name


class SpokenNumbers(_Lines):
    """How phone numbers are said. The app writes them out in these words for the agent to read, since the model got
    the digits wrong when it turned them into words itself."""

    words: list[str] = []  # 0 to 99 (exactly 100); empty = the numbers stay grouped digits
    hundreds: list[str] = []  # 100 to 900 when more digits follow (exactly 9)
    hundreds_alone: list[str] = []  # exactly 100 to 900 (exactly 9)
    joined: str  # {words}: a group that another group follows
    separator: str  # between groups
    phone_groups: list[int]  # a 10-digit phone number in groups, e.g. [3, 3, 2, 2]
    lakh: str = ""  # {words}: the lakhs of an amount (119,990 = 1 lakh); empty = amounts stay digits
    thousand: str = ""  # {words}: the thousands of an amount
    amounts: dict[int, str] = {}  # amount -> the exact words for it (the package prices); others use lakh/thousand

    @field_validator("words", "hundreds", "hundreds_alone")
    @classmethod
    def _word_lists(cls, v, info):
        want = 100 if info.field_name == "words" else 9
        if v and (len(v) != want or not all(str(x).strip() for x in v)):
            raise ValueError(f"needs exactly {want} words (or leave it empty to keep digits), none empty")
        return [str(x).strip() for x in v]

    @field_validator("phone_groups")
    @classmethod
    def _groups(cls, v):
        if not v or any(n < 1 or n > 3 for n in v):
            raise ValueError("list group sizes from 1 to 3, e.g. [3, 3, 2, 2]")
        return v

    def group(self, digits: str) -> str:
        """One group of up to three digits as words: a leading 0 is said on its own first ('016' = zero, sixteen)."""
        if len(digits) > 1 and digits[0] == "0":
            return f"{self.joined.format(words=self.words[0])} {self.group(digits[1:])}"
        n = int(digits)
        if n < 100:
            return self.words[n]
        hundred, rest = divmod(n, 100)
        return self.hundreds_alone[hundred - 1] if rest == 0 else f"{self.hundreds[hundred - 1]} {self.words[rest]}"

    @field_validator("amounts")
    @classmethod
    def _amounts(cls, v):
        if not all(str(w).strip() for w in v.values()):
            raise ValueError("every amount needs its words")
        return {int(k): str(w).strip() for k, w in v.items()}

    def say_amount(self, n: int) -> str:
        """A whole amount below ten lakhs as words (119,990 = one lakh, nineteen thousand, nine hundred ninety).
        The `amounts` table wins over the formula. "" if the language has no lakh/thousand words, or the amount is
        outside 1 to 999,999 and not in the table."""
        if n in self.amounts:
            return self.amounts[n]
        if not (self.words and self.lakh and self.thousand) or not 0 < n < 1_000_000:
            return ""
        lakhs, rest = divmod(n, 100_000)
        thousands, rest = divmod(rest, 1000)
        parts = []
        if lakhs:
            parts.append(self.lakh.format(words=self.words[lakhs]))
        if thousands:
            parts.append(self.thousand.format(words=self.words[thousands]))
        if rest:
            parts.append(self.group(str(rest)))
        return " ".join(parts)

    def say(self, digits: str, sizes: list[int]) -> str:
        """`digits` in groups of `sizes`, as words. "" if the language keeps digits or the length doesn't fit."""
        if not self.words or not digits.isdigit() or len(digits) != sum(sizes):
            return ""
        groups, start = [], 0
        for size in sizes:
            groups.append(self.group(digits[start:start + size]))
            start += size
        return self.separator.join([self.joined.format(words=g) for g in groups[:-1]] + groups[-1:])


class Language(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    name: str
    native_name: str
    transcription_codes: list[str]
    whatsapp_language: str  # Meta's template language code: si, ta, en
    greeting: str  # {company} {agent}
    recording_notice: str
    style_rules: str
    hearing_hints: str  # words that sound alike on a phone line; "" = none
    glossary: dict[str, str]
    goodbye_phrases: list[str]
    join_word: str
    common: CommonLines
    send: SendLines
    lead: LeadLines
    sms: SmsLines
    language: LanguageLines
    spoken_numbers: SpokenNumbers
    say_instead: dict[str, str]

    @field_validator("transcription_codes")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("list at least one speech-to-text code, e.g. si-LK")
        return v

    @field_validator("goodbye_phrases")
    @classmethod
    def _goodbyes(cls, v: list[str]) -> list[str]:
        if not v or not all(str(p).strip() for p in v):
            raise ValueError("list at least one phrase, none empty")
        return v

    def join(self, items: list[str]) -> str:
        items = [str(i) for i in items]
        if len(items) < 2:
            return "".join(items)
        return f"{', '.join(items[:-1])} {self.join_word} {items[-1]}"


# Placeholders each text may use; anything else is a typo. Lines not listed take none.
_LINE_PLACEHOLDERS = {
    "greeting": {"company", "agent"},
    "common.read_back_phone": {"phone"}, "common.out_of_scope": {"company"},
    "send.use_caller_id": {"phone"}, "lead.use_caller_id": {"phone"},
    "lead.confirm_details": {"name", "business", "time", "phone"},
    "sms.demo_booked": {"company", "name"}, "sms.demo_updated": {"company", "name"},
    "sms.demo_business": {"business"}, "sms.demo_time": {"time"},
    "sms.intro": {"company"}, "sms.industry_title": {"industry"}, "sms.website_line": {"url"},
    "sms.email_line": {"email"}, "sms.phone_line": {"phone"},
    "spoken_numbers.joined": {"words"}, "spoken_numbers.lakh": {"words"}, "spoken_numbers.thousand": {"words"},
}
REQUIRED_PLACEHOLDERS = {
    "common.read_back_phone": {"phone"}, "send.use_caller_id": {"phone"}, "lead.use_caller_id": {"phone"},
    "lead.confirm_details": {"name", "business", "time", "phone"},
    "sms.demo_booked": {"name"}, "sms.demo_updated": {"name"}, "sms.demo_business": {"business"},
    "sms.demo_time": {"time"},
    "sms.intro": {"company"},
    "sms.industry_title": {"industry"}, "sms.website_line": {"url"}, "sms.email_line": {"email"},
    "sms.phone_line": {"phone"}, "spoken_numbers.joined": {"words"},
}
_BLOCKS = {"common": CommonLines, "send": SendLines, "lead": LeadLines, "sms": SmsLines, "language": LanguageLines,
           "spoken_numbers": SpokenNumbers}
PLACEHOLDERS: dict[str, set[str]] = {"greeting": _LINE_PLACEHOLDERS["greeting"], "recording_notice": set()}
for _name, _block in _BLOCKS.items():
    for _field, _info in _block.model_fields.items():
        if _info.annotation is str:
            PLACEHOLDERS[f"{_name}.{_field}"] = _LINE_PLACEHOLDERS.get(f"{_name}.{_field}", set())


def fill(text: str, **values) -> str:
    """Fill the placeholders given in `values`; any other {placeholder} stays for the agent to fill in."""

    class _Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return string.Formatter().vformat(text, (), _Keep(values))


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _check_placeholders(lang: Language, file: str) -> None:
    for where, allowed in PLACEHOLDERS.items():
        obj = lang
        for part in where.split("."):
            obj = getattr(obj, part)
        try:
            used = _placeholders(obj)
        except ValueError as exc:
            raise LanguageFileError(f"{file}: {where}: {exc} (use {{ }} only around placeholders)") from exc
        if unknown := used - allowed:
            raise LanguageFileError(f"{file}: {where}: unknown placeholder {{{sorted(unknown)[0]}}} "
                                    f"(allowed: {', '.join('{' + a + '}' for a in sorted(allowed)) or 'none'})")
        if missing := REQUIRED_PLACEHOLDERS.get(where, set()) - used:
            raise LanguageFileError(f"{file}: {where}: must contain {{{sorted(missing)[0]}}}")


def _describe(err: ValidationError, file: str) -> str:
    first = err.errors()[0]
    where = ".".join(str(p) for p in first["loc"]) or "(file)"
    kind = first["type"]
    if kind == "missing":
        return f"{file}: missing key '{where}'"
    if kind == "extra_forbidden":
        return f"{file}: unknown key '{where}' (misspelt?)"
    return f"{file}: {where}: {first['msg']}"


def load_file(path: Path) -> Language:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LanguageFileError(f"{path.name}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LanguageFileError(f"{path.name}: expected keys like 'code:' and 'name:'")
    try:
        lang = Language.model_validate(data)
    except ValidationError as exc:
        raise LanguageFileError(_describe(exc, path.name)) from exc
    if lang.code != path.stem:
        raise LanguageFileError(f"{path.name}: code is '{lang.code}' but the file is named {path.name}")
    _check_placeholders(lang, path.name)
    return lang


# The order languages are listed in everywhere (prompt, admin panel).
ORDER = ("si", "ta", "en")


def load_languages(directory: Path = LANG_DIR) -> dict[str, Language]:
    """Every language file, checked, in ORDER (others after, by code)."""
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise LanguageFileError(f"no language files in {directory}")
    langs = [load_file(f) for f in files]
    codes = {lang.code for lang in langs}
    for lang in langs:
        if missing := sorted(codes - set(lang.language.names)):
            raise LanguageFileError(f"{lang.code}.yaml: language.names has no name for {', '.join(missing)}")
    langs.sort(key=lambda l: (ORDER.index(l.code) if l.code in ORDER else len(ORDER), l.code))
    return {lang.code: lang for lang in langs}


@lru_cache
def languages() -> dict[str, Language]:
    return load_languages()


def codes() -> list[str]:
    return list(languages())


def get(code: str) -> Language:
    return languages()[code]


def transcription_codes(enabled) -> list[str]:
    """Speech-to-text codes for the enabled languages, in order, without repeats."""
    out: list[str] = []
    for code in enabled:
        if code in languages():
            out.extend(c for c in languages()[code].transcription_codes if c not in out)
    return out


def _phrase_regex(phrases_of) -> re.Pattern:
    """Any of the phrases, in any language. Word boundaries only around Latin-script phrases: Sinhala and Tamil
    vowel signs don't count as word characters, so \\b would break inside their words. An apostrophe is optional and
    may be straight or curly, since transcripts vary ("that's", "thats", "that\u2019s")."""
    parts = []
    for lang in languages().values():
        for phrase in phrases_of(lang):
            p = re.escape(phrase.strip()).replace(r"\ ", r"\s+").replace("'", "['\u2019]?")
            parts.append(rf"\b{p}\b" if phrase.isascii() else p)
    return re.compile("|".join(parts) or r"(?!)", re.IGNORECASE)


def goodbye_regex() -> re.Pattern:
    """Any goodbye phrase (the agent's side) in any language."""
    return _phrase_regex(lambda lang: lang.goodbye_phrases)
