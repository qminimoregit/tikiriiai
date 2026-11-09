"""Catch the caller pressing # during the agent conversation, and send them back to the language menu.

Asterisk 20's AudioSocket does not pass key presses to the app, so the app listens to Asterisk's manager interface (AMI)
for DTMFEnd events instead. When the caller presses # on a call the dialplan registered (its channel name comes with
/api/pbx/call-start), the channel is redirected to the `start` label of the dialplan extension, which plays the language
menu again, and the AudioSocket connection of this call is closed.

Off unless AMI_USER and AMI_SECRET are set. A lost connection is retried; a failure here never touches a call.
"""

import asyncio
import logging
from typing import Protocol

from ..config import get_settings

log = logging.getLogger("tikiriiai.ami")

MENU_KEY = "#"
RECONNECT_SECONDS = (1, 2, 5, 10, 30)
LOGIN_TIMEOUT = 5


class MenuTransport(Protocol):
    menu_requested: bool
    closed: bool
    writer: asyncio.StreamWriter


# calls the dialplan registered and that are now on the line: {channel: (transport, context, exten)}
_active: dict[str, tuple[MenuTransport, str, str]] = {}
_writer: asyncio.StreamWriter | None = None
_task: asyncio.Task | None = None


def track(channel: str, transport: MenuTransport, context: str, exten: str) -> None:
    if channel and exten:
        _active[channel] = (transport, context, exten)


def untrack(channel: str) -> None:
    _active.pop(channel, None)


def parse_event(block: str) -> dict[str, str]:
    """One AMI message ("Key: value" lines) as a dict."""
    out: dict[str, str] = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def menu_request(event: dict[str, str]) -> str | None:
    """The channel of a caller who pressed #, or None for any other event."""
    if (event.get("Event") == "DTMFEnd" and event.get("Digit") == MENU_KEY
            and event.get("Direction") == "Received" and event.get("Channel") in _active):
        return event["Channel"]
    return None


def _action(**fields: str) -> bytes:
    return ("".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n").encode()


async def _back_to_menu(channel: str) -> None:
    item = _active.pop(channel, None)
    if not item or _writer is None:
        return
    transport, context, exten = item
    log.info("caller on %s pressed %s: back to the language menu", channel, MENU_KEY)
    transport.menu_requested = True
    _writer.write(_action(Action="Redirect", Channel=channel, Context=context, Exten=exten, Priority="start"))
    await _writer.drain()
    transport.closed = True
    try:
        transport.writer.close()
    except Exception:  # noqa: BLE001 - the redirect already ended the AudioSocket leg
        pass


async def _read_blocks(reader: asyncio.StreamReader):
    buf = ""
    while True:
        data = await reader.read(4096)
        if not data:
            return
        buf += data.decode("utf-8", "replace")
        while "\r\n\r\n" in buf:
            block, buf = buf.split("\r\n\r\n", 1)
            yield block


async def _session(host: str, port: int, user: str, secret: str) -> None:
    global _writer
    reader, writer = await asyncio.open_connection(host, port)
    try:
        await asyncio.wait_for(reader.readline(), LOGIN_TIMEOUT)  # banner
        writer.write(_action(Action="Login", Username=user, Secret=secret, Events="dtmf"))
        await writer.drain()
        _writer = writer
        log.info("connected to Asterisk AMI at %s:%s", host, port)
        async for block in _read_blocks(reader):
            event = parse_event(block)
            if event.get("Response") == "Error":
                log.warning("AMI: %s", event.get("Message", "error"))
            if channel := menu_request(event):
                asyncio.create_task(_back_to_menu(channel))
    finally:
        _writer = None
        writer.close()


async def _run() -> None:
    s = get_settings()
    attempt = 0
    while True:
        try:
            await _session(s.ami_host, s.ami_port, s.ami_user, s.ami_secret)
            attempt = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep retrying; calls work without this
            log.warning("AMI connection failed: %s", exc)
        await asyncio.sleep(RECONNECT_SECONDS[min(attempt, len(RECONNECT_SECONDS) - 1)])
        attempt += 1


def start() -> None:
    global _task
    s = get_settings()
    if not (s.ami_user and s.ami_secret):
        log.info("AMI_USER/AMI_SECRET not set: pressing # does not return to the menu")
        return
    _task = asyncio.create_task(_run())


def stop() -> None:
    if _task:
        _task.cancel()
