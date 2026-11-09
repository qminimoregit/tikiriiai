"""Meta's WhatsApp webhook: the handshake, and delivery reports for the messages we sent.

Meta says whether a message was delivered, read or could not be delivered (for example, the number has no WhatsApp)
only here, minutes after our send was accepted. In `whatsapp_first` mode a failure sends the SMS instead.
Open to the internet (see auth.PUBLIC_PATHS); every POST must carry Meta's signature of the body.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import notify
from ..config import get_settings
from ..db import SessionLocal
from ..models import SmsMessage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])

RANK = {"not_sent": 0, "failed": 1, "sent": 2, "delivered": 3, "read": 4}  # reports can arrive out of order


@router.get("/webhook")
def verify(request: Request):
    q = request.query_params
    token = get_settings().whatsapp_verify_token
    if token and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Wrong verify token")


def _signed(body: bytes, header: str) -> bool:
    secret = get_settings().whatsapp_app_secret
    if not secret:
        return False  # without the app secret anyone could post: refuse
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _error(status: dict) -> str:
    errors = status.get("errors") or []
    if not errors:
        return ""
    e = errors[0]
    return f"{e.get('code', '')} {e.get('title') or e.get('message') or ''}".strip()[:300]


def apply_status(status: dict) -> bool:
    """Update the message a Meta status report is about; True when it is a failure to follow up with an SMS."""
    wamid, new = str(status.get("id") or ""), str(status.get("status") or "")
    if not wamid or new not in ("sent", "delivered", "read", "failed"):
        return False
    with SessionLocal() as db:
        row = db.query(SmsMessage).filter(SmsMessage.provider_id == wamid, SmsMessage.channel == "whatsapp").first()
        if row is None:
            return False
        if new == "failed":
            row.status, row.error = "failed", _error(status)
        elif RANK.get(new, 0) > RANK.get(row.status, 0):
            row.status = new
        db.commit()
    return new == "failed"


@router.post("/webhook")
async def receive(request: Request):
    body = await request.body()
    if not _signed(body, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(403, "Bad signature")
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Not JSON")
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            for status in value.get("statuses", []):
                if apply_status(status):
                    notify.fallback_after_failure(str(status.get("id")))
            if value.get("messages"):  # callers writing to us: two-way is not built, so they are only counted
                log.info("WhatsApp: %d incoming message(s) ignored", len(value["messages"]))
    return {"ok": True}
