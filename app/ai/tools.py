"""Functions the agent can call during a call, and their handlers.

Declarations are plain dicts (JSON schema) passed as tools=[{"function_declarations": DECLARATIONS}].
gemini-3.8-live runs tool calls non-blocking by default (the model keeps talking while a tool runs). Every tool but
end_call is BLOCKING: the agent has to wait for the checked phone number, the booking or the send result. Tool
calls are awaited inline in the session loop, so anything slow (the SMS or WhatsApp to the caller, the team's lead
alert) is queued in the background, never done inside a tool.
dispatch() never raises: a failing tool returns {"error": ...} so the model can recover and the call goes on.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .. import details, languages, notify, sms
from ..db import SessionLocal
from ..models import Lead
from ..phones import display_phone, say_phone, sri_lankan
from ..snapshot import TikiriiSnapshot

log = logging.getLogger(__name__)

END_REASONS = ["completed", "demo_booked", "details_sent", "out_of_scope", "caller_goodbye", "no_response"]

DECLARATIONS = [
    {
        "name": "book_demo",
        "behavior": "BLOCKING",
        "description": (
            "Save the caller's request for a free demo, so the TIKIRII team calls them back to arrange it. Call it "
            "after the caller said yes to a demo and gave their name, business name and a call-back time. It first "
            "has the caller confirm the number and the details (its 'say' asks), so it is called again with the same "
            "details after each yes, or with corrected details when the caller fixes something; a correction after "
            "saving updates the same request. Write every value in English letters. The result's 'say' is what to "
            "tell the caller."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The caller's name."},
                "business_name": {"type": "string", "description": "The name of their business."},
                "industry": {"type": "string", "description": "The kind of business in English, e.g. \"salon\", "
                                                              "\"restaurant\", \"clinic\"."},
                "languages": {"type": "array", "items": {"type": "string", "enum": ["si", "ta", "en"]},
                              "description": "The languages their customers speak, if they said: si (Sinhala), ta "
                                             "(Tamil), en (English)."},
                "phone": {"type": "string", "description": "Leave empty to use the number check_phone accepted or the "
                                                           "caller-ID number the caller agreed to."},
                "email": {"type": "string", "description": "Only if the caller gave an email address."},
                "preferred_time": {"type": "string", "description": "When the team should call, as the caller said "
                                                                    "it, in English, e.g. \"tomorrow morning\"."},
                "notes": {"type": "string", "description": "What they want TIKIRII to handle (in English, short)."},
            },
            "required": ["name", "business_name", "preferred_time"],
        },
    },
    {
        "name": "send_details",
        "behavior": "BLOCKING",
        "description": (
            "Send the caller a short summary about TIKIRII as a message on their phone (SMS or WhatsApp). Call it only "
            "after the caller said yes to receiving it and the number is settled: either the caller-ID number they "
            "agreed to (leave phone empty) or a number you checked with check_phone and read back. The message is "
            "written by the app: never write it yourself. The result's 'say' is what to tell the caller."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(details.TOPICS)},
                    "description": "What to send: overview (what TIKIRII is), capabilities (what it handles), industry "
                                   "(what it does for the caller's kind of business), contact (how to reach the team), "
                                   "or all.",
                },
                "industry": {"type": "string", "description": "The caller's kind of business in English, e.g. "
                                                              "\"salon\". Empty if not known."},
                "phone": {"type": "string", "description": "Leave empty to use the number check_phone accepted or the "
                                                           "caller-ID number the caller agreed to."},
            },
            "required": ["topics"],
        },
    },
    {
        "name": "check_phone",
        "behavior": "BLOCKING",
        "description": (
            "Check a phone number the caller said and get the exact words to read it back. Call it every time the "
            "caller gives a number, before book_demo or send_details. Never turn digits into words yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {"phone": {"type": "string", "description": "The number the caller said, digits only, "
                                                                      "starting with 0, e.g. 0771234567."}},
            "required": ["phone"],
        },
    },
    {
        "name": "end_call",
        "description": "End the call. Call it as soon as the caller has nothing more to ask, without saying goodbye "
                       "first: the result holds the goodbye to say, and the call hangs up once it has been said.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": END_REASONS, "description": "Why the call is ending."},
            },
            "required": ["reason"],
        },
    },
]


@dataclass
class CallContext:
    """Per-call state shared by the tools and the session loop."""

    call_id: int
    channel: str
    caller_number: str
    snap: TikiriiSnapshot
    language: str = ""
    ended: bool = False
    end_reason: str = ""
    phone: str = ""  # the number as check_phone accepted it (0771234567)
    sent: set[tuple[str, str]] = field(default_factory=set)  # (number, message text) already sent in this call
    lead_id: int | None = None  # the demo request book_demo saved in this call
    ended_event: asyncio.Event = field(default_factory=asyncio.Event)
    deadline: float = 0.0  # time.monotonic() when the call must end
    said_goodbye: bool = False  # the agent already said goodbye before calling end_call
    caller_id_asked: bool = False  # the tools already had the agent ask "is the number you are calling from right?"
    read_back: tuple | None = None  # the demo details book_demo last read back to the caller
    saved: tuple | None = None  # the demo details saved in this call (lead_id)

    @property
    def winding_down(self) -> bool:
        """The call is ending (end_call): nothing may cut in or restart it."""
        return self.ended


def _lang(ctx: CallContext) -> languages.Language:
    """The language of this call (the main language if the call has none)."""
    code = ctx.language if ctx.language in languages.codes() else ctx.snap.profile.default_language
    return languages.get(code)


def _bad_phone(given: str, lang: languages.Language) -> dict:
    digits = re.sub(r"\D", "", given)
    if not digits:
        return {"error": "No phone number was given. Ask for one, then call check_phone with it.",
                "say": lang.common.ask_phone}
    return {"error": f"{given!r} has {len(digits)} digits: a Sri Lankan phone number has 10, starting with 0. Ask for "
                     "the whole number again, then call check_phone with it.",
            "say": lang.common.phone_ten_digits}


async def check_phone(args: dict, ctx: CallContext) -> dict:
    lang = _lang(ctx)
    given = str(args.get("phone") or "").strip()
    number = sri_lankan(given)
    if not number:
        return _bad_phone(given, lang)
    ctx.phone = number
    words = say_phone(number, lang)
    result = {"result": "a full number. Say the 'say' to read it back.", "phone": display_phone(number),
              "phone_say": words, "say": languages.fill(lang.common.read_back_phone, phone=words)}
    if sms.sms_number(number) is None:
        result["note"] = "This is a landline: a message cannot be sent to it. If they want the details sent, ask for a mobile."
    return result


def _target_phone(args: dict, ctx: CallContext) -> str:
    """The number to message: the one given if it is a full Sri Lankan number, else the one check_phone accepted,
    else the caller ID."""
    given = str(args.get("phone") or "").strip()
    return sri_lankan(given) or ctx.phone or sri_lankan(ctx.caller_number)


def _confirm_caller_id(args: dict, ctx: CallContext, lang: languages.Language, line: str, tool: str) -> dict | None:
    """The question to put to the caller before their caller-ID number is used (once per call), or None.

    Asked by the tool, not left to the model: it is only skipped when the caller gave a number, or check_phone
    accepted one, or the caller ID is not a Sri Lankan number the team could call back."""
    given = str(args.get("phone") or "").strip()
    if given:
        return None if sri_lankan(given) else _bad_phone(given, lang)  # never fall back to the caller ID unasked
    caller = sri_lankan(ctx.caller_number)
    if ctx.caller_id_asked or not caller or ctx.phone:
        return None
    ctx.caller_id_asked = True
    return {"error": "The caller's number is not confirmed yet. Say the 'say' exactly and wait for the answer, then "
                     f"call {tool} again: on a yes leave phone empty; on a no ask for the number they want, call "
                     "check_phone and read it back.",
            "say": languages.fill(line, phone=say_phone(caller, lang))}


async def send_details(args: dict, ctx: CallContext) -> dict:
    lang = _lang(ctx)
    raw = args.get("topics") or ["all"]
    topics = [str(t) for t in (raw if isinstance(raw, list) else [raw])]
    if ask := _confirm_caller_id(args, ctx, lang, lang.send.use_caller_id, "send_details"):
        return ask
    number = _target_phone(args, ctx)
    if not number:
        given = str(args.get("phone") or "").strip()
        return _bad_phone(given, lang) if given else {
            "error": "No phone number yet. Ask for a mobile number, then call check_phone with it.",
            "say": lang.send.ask_mobile}
    if sms.sms_number(number) is None:
        return {"error": "That is not a mobile number, so a message cannot be sent to it.", "say": lang.send.not_mobile}
    if not notify.will_send(number):
        return {"result": "messages are not set up or switched off; tell the caller and give the details by voice",
                "say": lang.send.not_set_up}
    built = details.build(ctx.snap, lang.code, topics, industry=str(args.get("industry") or ""))
    if built.empty:
        return {"result": "nothing to send for that; give the details by voice or ask what to send",
                "say": lang.send.nothing_to_send}
    key = (number, built.text)
    if key not in ctx.sent:
        ctx.sent.add(key)
        notify.queue_details(notify.Event(ctx.call_id, number, lang.code, ",".join(built.topics), built.text, built.params))
    result = {"result": "the details are being sent to the caller's phone", "phone": display_phone(number),
              "say": lang.send.sent}
    if built.truncated:
        result["note"] = ("The message could not hold everything, so the end was left out. Tell the caller, and give "
                          "the rest by voice if they want it.")
    return result


def _text(args: dict, key: str, limit: int) -> str:
    return " ".join(str(args.get(key) or "").split())[:limit]


def _lead_fields(args: dict, number: str, lang: languages.Language) -> dict:
    return dict(name=_text(args, "name", 200), business_name=_text(args, "business_name", 200),
                industry=_text(args, "industry", 200),
                languages=[c for c in (args.get("languages") or []) if c in languages.codes()],
                phone=number, email=_text(args, "email", 200), preferred_time=_text(args, "preferred_time", 200),
                notes=_text(args, "notes", 2000), language=lang.code)


def _save_lead(ctx: CallContext, args: dict, number: str, lang: languages.Language) -> Lead:
    """A new demo request, or the corrections to the one already saved in this call (the same row)."""
    with SessionLocal() as db:
        lead = db.get(Lead, ctx.lead_id) if ctx.lead_id is not None else None
        if lead is None:
            lead = Lead(call_id=ctx.call_id)
            db.add(lead)
        for key, value in _lead_fields(args, number, lang).items():
            setattr(lead, key, value)
        db.commit()
        db.refresh(lead)
        return lead


def _demo_key(args: dict, number: str) -> tuple:
    """What is read back and saved, compared without case or spacing differences."""
    return tuple(_text(args, k, 200).casefold() for k in ("name", "business_name", "preferred_time")) + (number,)


async def book_demo(args: dict, ctx: CallContext) -> dict:
    lang = _lang(ctx)
    if ctx.lead_id is not None and ctx.saved == _demo_key(args, _target_phone(args, ctx)):
        return {"result": "the demo request was already saved in this call with these details",
                "say": lang.lead.already_booked}
    if not _text(args, "name", 200):
        return {"error": "No name yet. Ask the caller's name, then call book_demo again.", "say": lang.lead.ask_name}
    if not _text(args, "business_name", 200):
        return {"error": "No business name yet. Ask for it, then call book_demo again.", "say": lang.lead.ask_business}
    if not _text(args, "preferred_time", 200):
        return {"error": "No call-back time yet. Ask for it, then call book_demo again.", "say": lang.lead.ask_time}
    if ask := _confirm_caller_id(args, ctx, lang, lang.lead.use_caller_id, "book_demo"):
        return ask
    number = _target_phone(args, ctx)
    if not number:
        given = str(args.get("phone") or "").strip()
        return _bad_phone(given, lang) if given else {
            "error": "No phone number yet. Ask for one, call check_phone, read it back, then call book_demo again.",
            "say": lang.common.ask_phone}
    key = _demo_key(args, number)
    if ctx.read_back != key:
        ctx.read_back = key
        return {"error": "Not saved yet. Read the details back: say the 'say' exactly and wait. On a yes, call "
                         "book_demo again with exactly the same details. If something is wrong, ask for the right "
                         "detail and call book_demo again with it corrected (it is read back again).",
                "say": languages.fill(lang.lead.confirm_details, name=_text(args, "name", 200),
                                      business=_text(args, "business_name", 200),
                                      time=_text(args, "preferred_time", 200), phone=say_phone(number, lang))}
    updated = ctx.lead_id is not None
    lead = await asyncio.to_thread(_save_lead, ctx, args, number, lang)
    ctx.lead_id, ctx.saved = lead.id, key
    notify.queue_lead_alert(ctx.call_id, notify.lead_alert_text(lead, updated=updated))
    texted = notify.will_confirm_by_sms(number)
    if texted:
        notify.queue_demo_confirm(ctx.call_id, number, lang.code,
                                  notify.demo_confirm_text(lead, lang, ctx.snap.profile.company, updated=updated))
    if updated:
        say = lang.lead.updated_sms if texted else lang.lead.updated
    else:
        say = lang.lead.booked_sms if texted else lang.lead.booked
    return {"result": "the corrected demo request is saved" if updated else
            "the demo request is saved; the team will call the caller back",
            "phone": display_phone(number), "say": say,
            "note": "Never promise a date or time for the demo. After the 'say', ask if there is anything else."}


async def end_call(args: dict, ctx: CallContext) -> dict:
    reason = str(args.get("reason") or "completed")
    ctx.ended = True
    ctx.end_reason = reason if reason in END_REASONS else "completed"
    if ctx.said_goodbye:
        return {"result": "Call is ending. Say nothing more."}
    common = _lang(ctx).common
    line = common.no_response if ctx.end_reason == "no_response" else common.goodbye
    return {"result": "Call is ending.", "say": line,
            "instruction": "Say the line in 'say' word for word now, and nothing else. Do not greet or ask anything."}


HANDLERS: dict[str, Callable[[dict, CallContext], Awaitable[dict]]] = {
    "book_demo": book_demo,
    "send_details": send_details,
    "check_phone": check_phone,
    "end_call": end_call,
}


async def dispatch(name: str, args: dict, ctx: CallContext) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown function {name}"}
    try:
        return await handler(args or {}, ctx)
    except Exception as exc:  # noqa: BLE001 - a broken tool must not drop the call
        log.exception("call %s: tool %s failed", ctx.call_id, name)
        return {"error": f"{name} failed: {exc}. Apologise, and ask the caller to call again later."}
