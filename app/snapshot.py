"""A frozen copy of everything the agent knows, read once per call.

The prompt builder, the tools and the admin prompt preview all take a TikiriiSnapshot, so the preview shows exactly
what a caller would get, and admin edits apply from the next call on.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import languages
from .clock import safe_zone
from .models import AgentProfile, Capability, FaqEntry, Industry, Integration

CHANNELS = ("sms", "whatsapp_first", "both", "whatsapp", "off")


@dataclass(frozen=True)
class ProfileInfo:
    company_name: str
    tagline: str
    overview: str
    website: str
    website_say_as: str
    contact_email: str
    email_say_as: str
    sales_phone: str
    pricing_note: str
    getting_started: str
    timezone: str
    agent_name: str
    agent_voice: str
    default_language: str
    languages_enabled: tuple[str, ...]
    greetings: dict
    recording_notices: dict
    temperature: float
    menu_audio: str
    menu_keys: dict
    unavailable_audio: dict
    message_channel: str
    extra_info: str
    agent_instructions: str

    @property
    def company(self) -> str:
        return self.company_name.strip() or "TIKIRII"


@dataclass(frozen=True)
class CapabilityInfo:
    title: str
    description: str


@dataclass(frozen=True)
class IndustryInfo:
    name: str
    name_si: str
    name_ta: str
    pitch: str
    tasks: tuple[str, ...]
    example: str

    def display_name(self, lang: str) -> str:
        return {"si": self.name_si, "ta": self.name_ta}.get(lang, "").strip() or self.name


@dataclass(frozen=True)
class IntegrationInfo:
    name: str
    description: str


@dataclass(frozen=True)
class FaqInfo:
    question: str
    answer: str


@dataclass(frozen=True)
class TikiriiSnapshot:
    profile: ProfileInfo
    capabilities: tuple[CapabilityInfo, ...] = ()
    industries: tuple[IndustryInfo, ...] = ()
    integrations: tuple[IntegrationInfo, ...] = ()
    faqs: tuple[FaqInfo, ...] = ()

    @property
    def tz(self) -> ZoneInfo:
        return safe_zone(self.profile.timezone)

    def industry_by_name(self, text: str) -> IndustryInfo | None:
        """An industry whose name (any language) contains `text` or is contained in it, case-insensitively."""
        t = " ".join((text or "").lower().split())
        if not t:
            return None
        for i in self.industries:
            names = [n.lower() for n in (i.name, i.name_si, i.name_ta) if n.strip()]
            if any(t == n for n in names):
                return i
        for i in self.industries:
            names = [n.lower() for n in (i.name, i.name_si, i.name_ta) if n.strip()]
            words = [w for n in names for w in n.replace("&", " ").replace(",", " ").split() if len(w) > 3]
            if any(t in n or n in t for n in names) or any(w in t for w in words):
                return i
        return None


def get_profile(db: Session) -> AgentProfile:
    profile = db.get(AgentProfile, 1)
    if profile is None:
        profile = AgentProfile(id=1)
        db.add(profile)
        db.commit()
    return profile


def _active(db: Session, model):
    return db.scalars(select(model).where(model.is_active).order_by(model.sort_order, model.id))


def load_snapshot(db: Session) -> TikiriiSnapshot:
    p = get_profile(db)
    codes = languages.codes()
    enabled = tuple(c for c in (p.languages_enabled or []) if c in codes) or (codes[0],)
    channel = p.message_channel if p.message_channel in CHANNELS else "sms"
    profile = ProfileInfo(
        company_name=p.company_name, tagline=p.tagline, overview=p.overview, website=p.website.strip(),
        website_say_as=p.website_say_as.strip(), contact_email=p.contact_email.strip(),
        email_say_as=p.email_say_as.strip(), sales_phone=p.sales_phone.strip(), pricing_note=p.pricing_note,
        getting_started=p.getting_started, timezone=p.timezone,
        agent_name=p.agent_name, agent_voice=p.agent_voice,
        default_language=p.default_language if p.default_language in enabled else enabled[0],
        languages_enabled=enabled, greetings=dict(p.greetings or {}), recording_notices=dict(p.recording_notices or {}),
        temperature=p.temperature, menu_audio=p.menu_audio or "", menu_keys=dict(p.menu_keys or {}),
        unavailable_audio=dict(p.unavailable_audio or {}), message_channel=channel,
        extra_info=p.extra_info, agent_instructions=p.agent_instructions,
    )
    capabilities = tuple(CapabilityInfo(c.title, c.description) for c in _active(db, Capability))
    industries = tuple(
        IndustryInfo(i.name, i.name_si, i.name_ta, i.pitch, tuple(str(t) for t in (i.tasks or []) if str(t).strip()),
                     i.example)
        for i in _active(db, Industry))
    integrations = tuple(IntegrationInfo(i.name, i.description) for i in _active(db, Integration))
    faqs = tuple(FaqInfo(f.question, f.answer) for f in _active(db, FaqEntry))
    return TikiriiSnapshot(profile, capabilities, industries, integrations, faqs)
