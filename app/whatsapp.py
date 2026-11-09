"""WhatsApp to callers through Meta's Cloud API.

A business can only start a chat with a pre-approved template, so every message is a template (see
scripts/whatsapp_templates.py) filled with named parameters. The template's language is each language file's
`whatsapp_language`. Sending never raises: a failure comes back as a status and a reason. Meta accepting a message
does not mean it arrived; the later "delivered", "read" or "failed" arrives on the webhook (routers/whatsapp.py).
"""

from __future__ import annotations

import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"

# What the message is called at Meta: one template, submitted once per language (scripts/whatsapp_templates.py).
TEMPLATE = "tikirii_details"


def template_name() -> str:
    return TEMPLATE


def setup_problem() -> str:
    """Why WhatsApp can't be sent right now; empty when it is ready."""
    s = get_settings()
    missing = [name for name, value in (("WHATSAPP_TOKEN", s.whatsapp_token),
                                        ("WHATSAPP_PHONE_NUMBER_ID", s.whatsapp_phone_number_id)) if not value.strip()]
    return f"{' and '.join(missing)} not set in .env" if missing else ""


def _error_text(body) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        detail = (err.get("error_data") or {}).get("details") or err.get("message") or ""
        return f"{err.get('code', '')} {err.get('type', '')}: {detail}".strip()
    return str(body)


def send_template(number: str, template: str, language: str, params: dict[str, str]) -> tuple[str, str, str]:
    """(status, error, message id). `number` is 947XXXXXXXX; `params` are the template's named parameters."""
    s = get_settings()
    payload = {
        "messaging_product": "whatsapp", "to": number, "type": "template",
        "template": {"name": template, "language": {"code": language},
                     "components": [{"type": "body",
                                     "parameters": [{"type": "text", "parameter_name": k, "text": str(v)}
                                                    for k, v in params.items()]}]},
    }
    url = f"{GRAPH_URL}/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}/messages"
    try:
        r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {s.whatsapp_token}"},
                       timeout=s.whatsapp_timeout_seconds)
        body = r.json()
    except Exception as exc:  # noqa: BLE001 - network, timeout or bad JSON: record it, never raise
        return "failed", f"{type(exc).__name__}: {exc}"[:300], ""
    messages = body.get("messages") if isinstance(body, dict) else None
    if r.status_code == 200 and messages:
        return "sent", "", str(messages[0].get("id", ""))
    return "failed", f"HTTP {r.status_code} {_error_text(body)}"[:300], ""
