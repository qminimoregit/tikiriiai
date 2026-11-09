"""Send callers the details they asked for (SMS, WhatsApp or both), and the sales team an SMS for each new lead.

The profile's `message_channel` decides:
  sms             SMS only
  whatsapp        WhatsApp only
  both            one message on each
  whatsapp_first  WhatsApp, then an SMS when WhatsApp is not set up, refuses the message at once, or (webhook) reports
                  later that it could not be delivered
  off             nothing is sent (the agent gives the details by voice only)

Every message is saved in `sms_message` (one row per channel), sent or not, so staff can see what the caller was told.
The text comes from details.py; the WhatsApp template is made from the same wording (scripts/whatsapp_templates.py).
Sending never raises: an SMS or WhatsApp problem must never break a call.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import details, languages, sms, whatsapp
from .db import SessionLocal
from .config import get_settings
from .models import AgentProfile, SmsMessage

log = logging.getLogger(__name__)

CHANNELS = ("sms", "whatsapp_first", "both", "whatsapp", "off")


@dataclass
class Event:
    call_id: int | None
    phone: str
    language: str
    topics: str
    text: str  # the SMS text (also kept as the record of what a WhatsApp message said)
    params: dict[str, str] = field(default_factory=dict)  # the WhatsApp template's parameters


def channel() -> str:
    with SessionLocal() as db:
        p = db.get(AgentProfile, 1)
    mode = p.message_channel if p is not None else "sms"
    return mode if mode in CHANNELS else "sms"


def _save(ev: Event, ch: str, number: str | None, status: str, error: str, provider_id: str = "",
          kind: str = "details") -> SmsMessage:
    with SessionLocal() as db:
        row = SmsMessage(call_id=ev.call_id, kind=kind, channel=ch, phone=(number or ev.phone)[:30],
                         language=ev.language, topics=ev.topics[:200], text=ev.text, status=status, error=error,
                         provider_id=provider_id)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def _send_sms(ev: Event) -> SmsMessage:
    """The text as one SMS, or several when it is longer than one; the row of the last one."""
    row = None
    for part in details.split_text(ev.text, sms.MAX_SMS_CHARS):
        number, status, error = sms._deliver(ev.phone, part)
        row = _save(Event(ev.call_id, ev.phone, ev.language, ev.topics, part, ev.params), "sms", number, status, error)
    return row


def _send_whatsapp(ev: Event) -> SmsMessage:
    number = sms.sms_number(ev.phone)  # the same 947XXXXXXXX form: only Sri Lankan mobiles are messaged
    if (problem := whatsapp.setup_problem()):
        return _save(ev, "whatsapp", number, "not_sent", problem)
    if number is None:
        return _save(ev, "whatsapp", number, "not_sent", "the caller's number is not a Sri Lankan mobile number")
    params = {k: " ".join(str(v).split()) for k, v in ev.params.items()}  # WhatsApp parameters can't hold line breaks
    status, error, wamid = whatsapp.send_template(number, whatsapp.template_name(), languages.get(ev.language)
                                                  .whatsapp_language, params)
    return _save(ev, "whatsapp", number, status, error, wamid)


def dispatch(ev: Event) -> SmsMessage | None:
    """Send one event as the channel setting says; the row of the message the caller is most likely to get."""
    mode = channel()
    if mode == "off":
        return None
    if mode == "sms":
        return _send_sms(ev)
    if mode == "whatsapp":
        return _send_whatsapp(ev)
    if mode == "both":
        _send_whatsapp(ev)
        return _send_sms(ev)
    # whatsapp_first: SMS at once when WhatsApp can't be tried (not set up, or not a mobile) or says no straight away
    if whatsapp.setup_problem() or sms.sms_number(ev.phone) is None:
        return _send_sms(ev)
    row = _send_whatsapp(ev)
    return _send_sms(ev) if row.status == "failed" else row


def send_details(ev: Event) -> SmsMessage | None:
    try:
        return dispatch(ev)
    except Exception:  # noqa: BLE001 - a message problem must never break a call
        log.exception("call %s: could not send the details message", ev.call_id)
        return None


_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")


def queue_details(ev: Event) -> None:
    """Send in the background, so a call never waits on Meta or the SMS provider."""
    _pool.submit(send_details, ev)


def lead_alert_text(lead, updated: bool = False) -> str:
    """The SMS the sales team gets for a new demo request, or for one the caller corrected later in the call.
    English: it is for staff, not the caller."""
    bits = [f"{'Updated' if updated else 'New'} TIKIRII demo request: {lead.name or 'no name'}"]
    if lead.business_name:
        bits.append(f"Business: {lead.business_name}")
    if lead.industry:
        bits.append(f"Type: {lead.industry}")
    bits.append(f"Phone: {lead.phone or 'not given'}")
    if lead.preferred_time:
        bits.append(f"Call back: {lead.preferred_time}")
    if lead.languages:
        bits.append("Languages: " + ", ".join(lead.languages))
    if lead.notes:
        bits.append(f"Wants: {lead.notes}")
    return "\n".join(bits)[:sms.MAX_SMS_CHARS]


def send_lead_alert(call_id: int | None, text: str) -> SmsMessage | None:
    """SMS the new demo request to LEAD_NOTIFY_PHONE (by SMS only: WhatsApp needs an approved template per message).
    Saved like any other message. Never raises."""
    try:
        to = get_settings().lead_notify_phone.strip()
        if not to:
            return None
        number, status, error = sms._deliver(to, text)
        return _save(Event(call_id, to, "en", "lead", text), "sms", number, status, error, kind="lead_alert")
    except Exception:  # noqa: BLE001 - an alert problem must never break a call
        log.exception("call %s: could not send the lead alert", call_id)
        return None


def queue_lead_alert(call_id: int | None, text: str) -> None:
    _pool.submit(send_lead_alert, call_id, text)


def demo_confirm_text(lead, lang, company: str, updated: bool = False) -> str:
    """The SMS that confirms a demo request to the caller, in the call's language. Built here, never by the model."""
    first = lang.sms.demo_updated if updated else lang.sms.demo_booked
    lines = [languages.fill(first, company=company, name=lead.name)]
    if lead.business_name:
        lines.append(languages.fill(lang.sms.demo_business, business=lead.business_name))
    if lead.preferred_time:
        lines.append(languages.fill(lang.sms.demo_time, time=lead.preferred_time))
    lines.append(lang.sms.demo_footer)
    return "\n".join(lines)


def will_confirm_by_sms(phone: str) -> bool:
    """True when a demo booking is confirmed to this caller by SMS: a Sri Lankan mobile, SMS set up, and the
    message setting uses SMS (a booking confirmation has no WhatsApp template)."""
    return channel() in ("sms", "whatsapp_first", "both") and not sms.setup_problem() and \
        sms.sms_number(phone) is not None


def send_demo_confirm(call_id: int | None, phone: str, language: str, text: str) -> SmsMessage | None:
    """SMS the booking confirmation to the caller (in parts if long). Saved like any other message. Never raises."""
    try:
        row = None
        for part in details.split_text(text, sms.MAX_SMS_CHARS):
            number, status, error = sms._deliver(phone, part)
            row = _save(Event(call_id, phone, language, "demo", part), "sms", number, status, error,
                        kind="demo_confirm")
        return row
    except Exception:  # noqa: BLE001 - a message problem must never break a call
        log.exception("call %s: could not send the demo confirmation", call_id)
        return None


def queue_demo_confirm(call_id: int | None, phone: str, language: str, text: str) -> None:
    _pool.submit(send_demo_confirm, call_id, phone, language, text)


def will_send(phone: str) -> bool:
    """True when this caller will really be messaged on at least one channel (the agent then promises a message)."""
    if sms.sms_number(phone) is None:
        return False
    mode = channel()
    sms_ok, whatsapp_ok = not sms.setup_problem(), not whatsapp.setup_problem()
    if mode == "off":
        return False
    if mode == "sms":
        return sms_ok
    if mode == "whatsapp":
        return whatsapp_ok
    return sms_ok or whatsapp_ok


def problems() -> dict[str, str]:
    """What the admin panel shows: why a channel in use can't send ("" = it can)."""
    mode = channel()
    return {"channel": mode,
            "sms": sms.setup_problem() if mode not in ("whatsapp", "off") else "",
            "whatsapp": whatsapp.setup_problem() if mode not in ("sms", "off") else ""}


def fallback_after_failure(provider_id: str) -> SmsMessage | None:
    """The webhook says WhatsApp could not deliver: in `whatsapp_first` mode, send the SMS now (once)."""
    try:
        with SessionLocal() as db:
            row = db.query(SmsMessage).filter(SmsMessage.provider_id == provider_id,
                                              SmsMessage.channel == "whatsapp").first()
            if row is None or channel() != "whatsapp_first":
                return None
            already = db.query(SmsMessage).filter(SmsMessage.channel == "sms", SmsMessage.call_id == row.call_id,
                                                  SmsMessage.phone == row.phone, SmsMessage.text == row.text,
                                                  SmsMessage.id > row.id).first()
            ev = Event(row.call_id, row.phone, row.language, row.topics, row.text)
        return None if already is not None else _send_sms(ev)
    except Exception:  # noqa: BLE001
        log.exception("could not send the SMS after a WhatsApp failure (%s)", provider_id)
        return None
