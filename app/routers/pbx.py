import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..config import get_settings
from ..schemas import LANGS
from ..telephony.asterisk import ip_allowed, pick_caller, register_caller

router = APIRouter(prefix="/api/pbx", tags=["pbx"])


@router.get("/call-start", response_class=PlainTextResponse)
def call_start(uuid: str, request: Request, caller: str = "", pai: str = "", name: str = "", token: str = "",
               lang: str = "", menu: int = 0, chan: str = "", ctx: str = "", exten: str = ""):
    """Called by the Asterisk dialplan just before AudioSocket(), to pass the caller ID of the call:
    CALLERID(num), the P-Asserted-Identity header and CALLERID(name).
    When the dialplan plays the language menu itself (the PBX does not pass key presses to AudioSocket), menu=1 and
    lang is the language the caller picked (si / ta / en, empty = none picked).
    chan, ctx and exten (the dialplan's CHANNEL, CONTEXT and EXTEN) let the caller press # to return to the menu."""
    s = get_settings()
    ip = request.client.host if request.client else ""
    if not ip_allowed(ip, s.pbx_allowed_ip_list):
        raise HTTPException(403, "not allowed")
    if s.pbx_hook_token and not hmac.compare_digest(token.encode(), s.pbx_hook_token.encode()):
        raise HTTPException(403, "bad token")
    lang = lang.strip().lower()
    register_caller(uuid, pick_caller(caller, pai, name), lang if lang in LANGS else "", bool(menu), chan, ctx, exten)
    return "ok"
