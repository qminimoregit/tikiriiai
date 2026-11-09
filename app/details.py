"""The text of the message a caller receives with the details they asked for.

Built in Python from the snapshot, in the caller's language (languages/<code>.yaml `sms` block), so the model never
writes it and every claim is one an admin entered. Pure: no database, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import languages
from .phones import display_phone
from .sms import MAX_SMS_CHARS
from .snapshot import TikiriiSnapshot

TOPICS = ("overview", "capabilities", "industry", "contact", "all")
MAX_PARTS = 4  # a message that needs more SMS than this is cut: the agent is told, and offers a narrower one


@dataclass
class Details:
    text: str
    params: dict[str, str] = field(default_factory=dict)  # the WhatsApp template's parameters
    topics: tuple[str, ...] = ()
    empty: bool = False  # nothing matched what the caller asked for
    truncated: bool = False  # more than MAX_PARTS SMS were needed: the end was left out


def contact_lines(snap: TikiriiSnapshot, lang) -> list[str]:
    p = snap.profile
    lines = []
    if p.website:
        lines.append(lang.sms.website_line.format(url=p.website))
    if p.contact_email:
        lines.append(lang.sms.email_line.format(email=p.contact_email))
    if p.sales_phone:
        lines.append(lang.sms.phone_line.format(phone=display_phone(p.sales_phone)))
    return lines


def build(snap: TikiriiSnapshot, language: str, topics, *, industry: str = "") -> Details:
    """`industry`: the caller's kind of business, as they said it (matched to an industry by name)."""
    lang = languages.get(language)
    topics = tuple(t for t in topics if t in TOPICS) or ("all",)
    every = "all" in topics
    company = snap.profile.company
    matched = snap.industry_by_name(industry) if industry else None

    sections: list[str] = []
    if (every or "industry" in topics) and matched is not None and matched.tasks:
        sections.append("\n".join([lang.sms.industry_title.format(industry=matched.display_name(language)),
                                   *(f"- {t}" for t in matched.tasks)]))
    if every or "capabilities" in topics or ("industry" in topics and matched is None):
        # The six call jobs come first in the admin list; an SMS keeps to them.
        lines = [f"- {c.title}" for c in snap.capabilities[:6]]
        if lines:
            sections.append("\n".join([lang.sms.capabilities_title, *lines]))
    if "overview" in topics and not every and snap.profile.tagline.strip():
        sections.append("\n".join([lang.sms.overview_title, snap.profile.tagline.strip()]))
    lines = contact_lines(snap, lang)
    if lines:
        sections.append("\n".join([lang.sms.contact_title, *lines]))
    elif "contact" in topics and len(topics) == 1:
        return Details("", {}, topics, empty=True)

    intro, footer = lang.sms.intro.format(company=company), lang.sms.footer
    full = "\n\n".join([intro, *sections, footer])
    parts = split_text(full)
    truncated = len(parts) > MAX_PARTS
    text = "\n".join(parts[:MAX_PARTS]) if truncated else full
    params = {"company": company, "details": " | ".join(s.replace("\n", "; ") for s in sections)[:900],
              "contact": " ".join(contact_lines(snap, lang))[:200] or "-"}
    return Details(text, params, topics, truncated=truncated)


def split_text(text: str, limit: int = MAX_SMS_CHARS) -> list[str]:
    """`text` as one or more SMS of at most `limit` characters, split at line ends (never in the middle of a line),
    keeping a section's title with its first line."""
    parts: list[list[str]] = [[]]
    size = 0
    for line in text.split("\n"):
        line = line[:limit]  # one line longer than an SMS is cut: never expected from the admin's data
        if parts[-1] and size + 1 + len(line) > limit:
            carry = []
            body = parts[-1]
            # a title (a line after a blank one) must not end a part
            if len(body) >= 2 and body[-1] and not body[-2]:
                carry = [body.pop()]
            while body and not body[-1]:
                body.pop()
            parts.append(carry)
            size = sum(len(l) + 1 for l in carry)
        parts[-1].append(line)
        size += len(line) + 1
    return ["\n".join(p).strip("\n") for p in parts if any(p)]
