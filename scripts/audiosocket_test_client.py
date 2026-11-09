"""Pretend to be Asterisk: call the running AudioSocket server, press keys, and record what the caller hears.

Opens a TCP connection like the AudioSocket() dialplan app: optionally registers a caller ID through the
PBX hook, sends the UUID packet, streams a mono PCM16 WAV in real time (20 ms frames), presses the keys given with
--digits at the given seconds (0x03 packets, as Asterisk 22.4+ sends them), keeps the line open a little longer
for the agent to finish, then hangs up and saves everything it heard (menus and agent) to a WAV.

Usage (server running with PBX_ENABLED=true):
    # Sinhala (1), then section 2, then topic 1; speak after 30 s of menus
    .venv/bin/python scripts/audiosocket_test_client.py tests/fixtures/sample_8k.wav out.wav --digits 1@4,2@10,1@16 --lead 30
    .venv/bin/python scripts/audiosocket_test_client.py in.wav out.wav --caller 0771234567 --digits 3@2,6@6
"""

import argparse
import asyncio
import struct
import urllib.parse
import urllib.request
import uuid
import wave

HEADER = struct.Struct(">BH")
T_HANGUP, T_UUID, T_DTMF = 0x00, 0x01, 0x03
RATE_TYPES = {8000: 0x10, 12000: 0x11, 16000: 0x12, 24000: 0x13}


def read_wav(path: str) -> tuple[int, bytes]:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("The input must be a mono 16-bit PCM WAV")
        if w.getframerate() not in RATE_TYPES:
            raise SystemExit(f"Sample rate must be one of {sorted(RATE_TYPES)}")
        return w.getframerate(), w.readframes(w.getnframes())


def write_wav(path: str, rate: int, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def parse_digits(spec: str) -> list[tuple[float, str]]:
    """"1@4,2@10" -> [(4.0, "1"), (10.0, "2")]: the key, and the second (from the start of the call) to press it."""
    presses = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        key, _, when = part.partition("@")
        if len(key) != 1 or key not in "0123456789*#":
            raise SystemExit(f"--digits: {key!r} is not a phone key")
        try:
            presses.append((float(when), key))
        except ValueError:
            raise SystemExit(f"--digits: give a time for {key!r}, e.g. {key}@5")
    return sorted(presses)


def register_caller(http: str, call_uuid: str, caller: str, token: str) -> None:
    query = urllib.parse.urlencode({"uuid": call_uuid, "caller": caller, "token": token})
    with urllib.request.urlopen(f"{http}/api/pbx/call-start?{query}", timeout=5) as r:
        r.read()


async def run(args) -> None:
    rate, pcm = read_wav(args.input)
    frame = int(rate * 0.02) * 2
    audio_type = RATE_TYPES[rate]
    call_uuid = uuid.uuid4()
    presses = parse_digits(args.digits)
    if args.caller:
        register_caller(args.http, str(call_uuid), args.caller, args.token)

    reader, writer = await asyncio.open_connection(args.host, args.port)
    writer.write(HEADER.pack(T_UUID, 16) + call_uuid.bytes)
    received = bytearray()
    agent_hung_up = asyncio.Event()

    async def receive() -> None:
        while True:
            try:
                typ, length = HEADER.unpack(await reader.readexactly(3))
                payload = await reader.readexactly(length) if length else b""
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            if typ == T_HANGUP:
                break
            if typ == audio_type:
                received.extend(payload)
        agent_hung_up.set()

    receiver = asyncio.create_task(receive())
    silence = bytes(frame)
    loop = asyncio.get_running_loop()
    t = loop.time()
    start = t

    async def press_keys() -> None:
        for when, key in presses:
            await asyncio.sleep(max(0.0, start + when - loop.time()))
            if agent_hung_up.is_set():
                return
            writer.write(HEADER.pack(T_DTMF, 1) + key.encode())
            print(f"  pressed {key} at {loop.time() - start:.1f} s")

    presser = asyncio.create_task(press_keys())

    async def send_frames(frames) -> bool:
        nonlocal t
        for chunk in frames:
            if agent_hung_up.is_set():
                return False
            writer.write(HEADER.pack(audio_type, len(chunk)) + chunk)
            await writer.drain()
            t += 0.02
            await asyncio.sleep(max(0.0, t - loop.time()))
        return True

    print(f"Calling {args.host}:{args.port} as {call_uuid} at {rate} Hz…")
    # Silence first so the greeting plays, then the caller's audio, then silence while the agent answers.
    lead = [silence] * int(args.lead / 0.02)
    speech = [pcm[i:i + frame].ljust(frame, b"\0") for i in range(0, len(pcm), frame)]
    tail = [silence] * int(args.linger / 0.02)
    if await send_frames(lead) and await send_frames(speech) and await send_frames(tail):
        writer.write(HEADER.pack(T_HANGUP, 0))
        await writer.drain()
        print("Hung up.")
    else:
        print("The agent hung up.")
    presser.cancel()
    await asyncio.wait_for(receiver, 5) if not receiver.done() else None
    writer.close()
    write_wav(args.output, rate, bytes(received))
    print(f"Saved {len(received) / 2 / rate:.1f} s of what the caller heard to {args.output}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="mono 16-bit WAV (8/12/16/24 kHz) with what the caller says")
    p.add_argument("output", help="where to save what the caller heard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9092)
    p.add_argument("--lead", type=float, default=30.0, help="seconds of silence before speaking (menus and greeting)")
    p.add_argument("--digits", default="", help="keys to press and when, e.g. 1@4,2@10,1@16 (key@seconds)")
    p.add_argument("--linger", type=float, default=15.0, help="seconds to stay on the line after speaking")
    p.add_argument("--caller", default="", help="caller ID to register through the PBX hook")
    p.add_argument("--http", default="http://127.0.0.1:8000", help="the app's HTTP address (for --caller)")
    p.add_argument("--token", default="", help="PBX_HOOK_TOKEN, if set")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
