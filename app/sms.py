"""SMS to callers (the details they asked for) and to the sales team (new demo requests).

The provider is a setting (SMS_PROVIDER). With none set up nothing is sent, but every message is still saved as
"not sent" so staff can see what the caller should have been told. Sending never raises: a failure is saved with its
reason. The texts come from each language's `sms` block, so no language is hard-coded here.
"""

from __future__ import annotations

import logging
import re

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

NOTIFYLK_URL = "https://app.notify.lk/api/v1/send"
GETMORE_URL = "https://sms.getmore.lk/api/v1/send_sms.php"
MAX_SMS_CHARS = 621  # Notify.lk's limit


def sms_number(phone: str) -> str | None:
    """A Sri Lankan mobile as 947XXXXXXXX (from 07X…, 7X…, +947X…, 00947X…); None for landlines and anything else."""
    digits = re.sub(r"\D", "", phone or "")
    for prefix in ("0094", "94", "0"):
        if digits.startswith(prefix) and len(digits) - len(prefix) == 9:
            digits = digits[len(prefix):]
            break
    return "94" + digits if re.fullmatch(r"7\d{8}", digits) else None


def setup_problem() -> str:
    """Why SMS can't be sent right now; empty when the provider is ready."""
    s = get_settings()
    provider = s.sms_provider.strip().lower()
    if not provider:
        return "SMS is not set up (SMS_PROVIDER is empty in .env)"
    if provider not in PROVIDERS:
        return f"unknown SMS_PROVIDER '{provider}'"
    if provider == "getmore":
        return "" if s.sms_api_key else "SMS_API_KEY is empty in .env"
    if not (s.sms_user_id and s.sms_api_key and s.sms_sender_id):
        return "SMS_USER_ID, SMS_API_KEY or SMS_SENDER_ID is empty in .env"
    return ""


def will_send(phone: str) -> bool:
    return not setup_problem() and sms_number(phone) is not None


def _send_notifylk(phone: str, text: str) -> tuple[str, str]:
    """(status, error). POSTs a form body so the API key stays out of any logged URL."""
    s = get_settings()
    data = {"user_id": s.sms_user_id, "api_key": s.sms_api_key, "sender_id": s.sms_sender_id, "to": phone,
            "message": text}
    if not text.isascii():
        data["type"] = "unicode"
    try:
        r = httpx.post(NOTIFYLK_URL, data=data, timeout=s.sms_timeout_seconds)
        body = r.json()
    except Exception as exc:  # noqa: BLE001 - network, timeout or bad JSON: record it, never raise
        return "failed", f"{type(exc).__name__}: {exc}"[:300]
    if r.status_code == 200 and isinstance(body, dict) and body.get("status") == "success":
        return "sent", ""
    return "failed", f"HTTP {r.status_code}: {body}"[:300]


def _send_getmore(phone: str, text: str) -> tuple[str, str]:
    """(status, error) for Getmore (sms.getmore.lk): a JSON POST with the API key as a Bearer token, number as +94…"""
    s = get_settings()
    try:
        r = httpx.post(GETMORE_URL, json={"to": "+" + phone, "message": text},
                       headers={"Authorization": f"Bearer {s.sms_api_key}"}, timeout=s.sms_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - network or timeout: record it, never raise
        return "failed", f"{type(exc).__name__}: {exc}"[:300]
    if r.is_success:
        return "sent", ""
    return "failed", f"HTTP {r.status_code}: {r.text}"[:300]


PROVIDERS = {"notifylk": _send_notifylk, "getmore": _send_getmore}


def _deliver(phone: str, text: str) -> tuple[str | None, str, str]:
    """Send `text` to a caller's number: (number used, status, error). Never raises."""
    number = sms_number(phone)
    problem = setup_problem()
    if problem:
        return number, "not_sent", problem
    if number is None:
        return number, "not_sent", "the caller's number is not a Sri Lankan mobile number"
    status, error = PROVIDERS[get_settings().sms_provider.strip().lower()](number, text)
    return number, status, error
