"""Database tables. Datetimes are naive UTC."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .clock import now_utc
from .db import Base


class AgentProfile(Base):
    """The company the agent answers for (TIKIRII) and the agent's own settings (always id=1)."""

    __tablename__ = "agent_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200), default="TIKIRII")
    tagline: Mapped[str] = mapped_column(String(300), default="")
    overview: Mapped[str] = mapped_column(Text, default="")  # what the product is, in a few sentences
    website: Mapped[str] = mapped_column(String(300), default="")
    website_say_as: Mapped[str] = mapped_column(String(300), default="")  # how the agent says the web address
    contact_email: Mapped[str] = mapped_column(String(200), default="")
    email_say_as: Mapped[str] = mapped_column(String(300), default="")
    # The number the agent may give out for the sales team. Blank = the agent offers to book a demo instead.
    sales_phone: Mapped[str] = mapped_column(String(60), default="")
    pricing_note: Mapped[str] = mapped_column(Text, default="")  # how pricing works; the agent never invents a price
    getting_started: Mapped[str] = mapped_column(Text, default="")  # the steps from demo to going live
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Colombo")

    agent_name: Mapped[str] = mapped_column(String(100), default="")
    agent_voice: Mapped[str] = mapped_column(String(40), default="")
    default_language: Mapped[str] = mapped_column(String(5), default="si")
    languages_enabled: Mapped[list] = mapped_column(JSON, default=lambda: ["si", "ta", "en"])
    greetings: Mapped[dict] = mapped_column(JSON, default=dict)  # {lang: text}; blank = built-in greeting
    recording_notices: Mapped[dict] = mapped_column(JSON, default=dict)  # {lang: text}; blank = built-in notice
    # Gemini 3 default: lower can loop or go quiet
    temperature: Mapped[float] = mapped_column(Float, default=1.0)
    menu_audio: Mapped[str] = mapped_column(String(200), default="")  # file in MEDIA_DIR; blank = no language menu
    menu_keys: Mapped[dict] = mapped_column(JSON, default=lambda: {"si": "1", "ta": "2", "en": "3"})  # {lang: digit}
    unavailable_audio: Mapped[dict] = mapped_column(JSON, default=dict)  # {lang: file in MEDIA_DIR}
    message_channel: Mapped[str] = mapped_column(String(20), default="sms")  # sms|whatsapp|whatsapp_first|both|off

    extra_info: Mapped[str] = mapped_column(Text, default="")
    agent_instructions: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class Capability(Base):
    """A call job the product does (incoming calls, appointments, reminders ...)."""

    __tablename__ = "capability"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Industry(Base):
    """A kind of business the product is pitched to, with the calls it handles there."""

    __tablename__ = "industry"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    name_si: Mapped[str] = mapped_column(String(200), default="")
    name_ta: Mapped[str] = mapped_column(String(200), default="")
    pitch: Mapped[str] = mapped_column(Text, default="")
    tasks: Mapped[list] = mapped_column(JSON, default=list)  # typical calls, e.g. ["Table reservation requests"]
    example: Mapped[str] = mapped_column(String(300), default="")  # a line a caller would say
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Integration(Base):
    """A system the product connects to (CRM, Google Calendar, WhatsApp ...)."""

    __tablename__ = "integration"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class FaqEntry(Base):
    __tablename__ = "faq_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Call(Base):
    __tablename__ = "call"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), index=True)  # browser_admin | browser_public | pbx
    caller_number: Mapped[str] = mapped_column(String(60), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    menu_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # time spent in the language menu
    language: Mapped[str] = mapped_column(String(5), default="")
    status: Mapped[str] = mapped_column(String(20), default="in_progress")  # in_progress|completed|failed|interrupted
    end_reason: Mapped[str] = mapped_column(String(200), default="")
    transcript: Mapped[list] = mapped_column(JSON, default=list)  # [{role, text, t}], roles: menu, caller, agent, tool
    # The caller's lines heard again from the recording after the call: ""|checking|done|failed.
    transcript_check: Mapped[str] = mapped_column(String(20), default="")
    reconnects: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    recording_path: Mapped[str] = mapped_column(String(300), default="")  # relative to RECORDINGS_DIR; "" = none

    @property
    def has_recording(self) -> bool:
        return bool(self.recording_path)


class SmsMessage(Base):
    """A message (SMS or WhatsApp) with details the caller asked for, sent or not (so staff can see what was sent)."""

    __tablename__ = "sms_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("call.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="details")
    channel: Mapped[str] = mapped_column(String(10), default="sms")  # sms | whatsapp
    provider_id: Mapped[str] = mapped_column(String(100), default="", index=True)  # WhatsApp's message id (wamid)
    phone: Mapped[str] = mapped_column(String(30), default="")
    language: Mapped[str] = mapped_column(String(5), default="")
    topics: Mapped[str] = mapped_column(String(200), default="")  # what the caller asked to be sent
    text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="not_sent")  # sent | delivered | read | failed | not_sent
    error: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)

LEAD_STATUSES = ("new", "contacted", "demo_booked", "closed")


class Lead(Base):
    """A demo request taken on a call (book_demo), for the sales team to follow up."""

    __tablename__ = "lead"

    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int | None] = mapped_column(ForeignKey("call.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    business_name: Mapped[str] = mapped_column(String(200), default="")
    industry: Mapped[str] = mapped_column(String(200), default="")
    languages: Mapped[list] = mapped_column(JSON, default=list)  # language codes the business needs
    phone: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    preferred_time: Mapped[str] = mapped_column(String(200), default="")  # as the caller said it
    notes: Mapped[str] = mapped_column(Text, default="")  # what they want the agent to handle
    language: Mapped[str] = mapped_column(String(5), default="")  # the language of the call
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new|contacted|demo_booked|closed
    staff_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
