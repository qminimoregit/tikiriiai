"""Browser call WebSocket for the admin Test Call page (same menu and agent as a phone call)."""

import logging

from fastapi import APIRouter, WebSocket

from ..auth import ws_authorized
from ..call_flow import handle_call
from ..telephony.browser import BrowserTransport

log = logging.getLogger(__name__)
router = APIRouter()

@router.websocket("/ws/test-call")
async def test_call(ws: WebSocket):
    # HTTP middleware doesn't see WebSockets, so check the login cookie (and Origin) here.
    if not ws_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    caller = (ws.query_params.get("caller_number") or "").strip()
    # ?lang=ta skips the language menu and starts in that language (to try each language without a menu recording).
    lang = (ws.query_params.get("lang") or "").strip().lower()
    await handle_call(BrowserTransport(ws), channel="browser_admin", caller_number=caller, menu_lang=lang,
                      pbx_menu=bool(lang))
