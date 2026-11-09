"""API request/response models."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

from .clock import safe_zone

LANGS = ("si", "ta", "en")
CHANNELS = ("sms", "whatsapp_first", "both", "whatsapp", "off")

# Naive UTC datetimes from the DB go out with a "Z" so browsers convert them correctly.
UTCDateTime = Annotated[datetime, PlainSerializer(lambda d: d.isoformat(timespec="seconds") + "Z", return_type=str, when_used="json")]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _required(v: str) -> str:
    if not v.strip():
        raise ValueError("This field is required")
    return v.strip()


# ---------- the company and the agent ----------

class ProfileIn(BaseModel):
    company_name: str = Field("TIKIRII", max_length=200)
    tagline: str = Field("", max_length=300)
    overview: str = ""
    website: str = Field("", max_length=300)
    website_say_as: str = Field("", max_length=300)
    contact_email: str = Field("", max_length=200)
    email_say_as: str = Field("", max_length=300)
    sales_phone: str = Field("", max_length=60)
    pricing_note: str = ""
    getting_started: str = ""
    timezone: str = "Asia/Colombo"
    agent_name: str = Field("", max_length=100)
    agent_voice: str = Field("", max_length=40)
    default_language: Literal["si", "ta", "en"] = "si"
    languages_enabled: list[Literal["si", "ta", "en"]] = ["si", "ta", "en"]
    greetings: dict[str, str] = {}
    recording_notices: dict[str, str] = {}
    menu_keys: dict[str, str] = {"si": "1", "ta": "2", "en": "3"}
    temperature: float = Field(1.0, ge=0.0, le=2.0)
    message_channel: Literal["sms", "whatsapp_first", "both", "whatsapp", "off"] = "sms"
    extra_info: str = ""
    agent_instructions: str = ""

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if safe_zone(v, "UTC").key != v:
            raise ValueError(f"Unknown timezone {v!r} (use a name like Asia/Colombo)")
        return v

    @field_validator("greetings", "recording_notices")
    @classmethod
    def _per_language(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: (v.get(k) or "").strip()[:2000] for k in LANGS if k in v}

    @field_validator("menu_keys")
    @classmethod
    def _menu_keys(cls, v: dict[str, str]) -> dict[str, str]:
        keys = {k: (v.get(k) or "").strip() for k in LANGS if k in v}
        if any(len(d) != 1 or d not in "0123456789" for d in keys.values()):
            raise ValueError("Each language key must be a single digit (0-9)")
        if len(set(keys.values())) != len(keys):
            raise ValueError("Two languages can't share a key")
        return keys

    @model_validator(mode="after")
    def _languages(self):
        if not self.languages_enabled:
            raise ValueError("Enable at least one language")
        if self.default_language not in self.languages_enabled:
            raise ValueError("The main language must be one of the enabled languages")
        return self


class ProfileOut(ProfileIn, ORM):
    menu_audio: str = ""
    unavailable_audio: dict[str, str] = {}


# ---------- capabilities, industries, integrations, FAQ ----------

class CapabilityIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    is_active: bool = True

    _title = field_validator("title")(lambda cls, v: _required(v))


class CapabilityOut(CapabilityIn, ORM):
    id: int
    sort_order: int


class IndustryIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    name_si: str = Field("", max_length=200)
    name_ta: str = Field("", max_length=200)
    pitch: str = ""
    tasks: list[str] = []
    example: str = Field("", max_length=300)
    is_active: bool = True

    _name = field_validator("name")(lambda cls, v: _required(v))

    @field_validator("tasks", mode="before")
    @classmethod
    def _tasks(cls, v):
        """The admin form sends one task per line."""
        items = v.splitlines() if isinstance(v, str) else (v or [])
        return [str(t).strip()[:200] for t in items if str(t).strip()][:12]


class IndustryOut(IndustryIn, ORM):
    id: int
    sort_order: int


class IntegrationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    is_active: bool = True

    _name = field_validator("name")(lambda cls, v: _required(v))


class IntegrationOut(IntegrationIn, ORM):
    id: int
    sort_order: int


class FaqIn(BaseModel):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    is_active: bool = True


class FaqOut(FaqIn, ORM):
    id: int
    sort_order: int


class ReorderIn(BaseModel):
    ids: list[int]


# ---------- calls and messages sent ----------

class CallOut(ORM):
    id: int
    channel: str
    caller_number: str
    started_at: UTCDateTime
    ended_at: UTCDateTime | None
    duration_seconds: int | None
    language: str
    status: str
    end_reason: str
    reconnects: int
    has_recording: bool


class SmsOut(ORM):
    id: int
    call_id: int | None
    channel: str
    phone: str
    language: str
    topics: str
    text: str
    status: str
    error: str
    created_at: UTCDateTime


class LeadOut(ORM):
    id: int
    call_id: int | None
    name: str
    business_name: str
    industry: str
    languages: list[str]
    phone: str
    email: str
    preferred_time: str
    notes: str
    language: str
    status: str
    staff_notes: str
    created_at: UTCDateTime


class LeadUpdate(BaseModel):
    status: Literal["new", "contacted", "demo_booked", "closed"]
    staff_notes: str = ""


class CallDetail(CallOut):
    transcript: list[dict]
    transcript_check: str = ""
    error: str
    messages: list[SmsOut] = []
    leads: list[LeadOut] = []
