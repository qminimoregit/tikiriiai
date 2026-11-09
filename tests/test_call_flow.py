"""Whole calls through handle_call(): language menu, then the agent against a scripted fake Gemini Live (no network)."""

import asyncio

import pytest
from google.genai import errors, types

import app.ai.live_session as ls
import app.call_flow as cf
from app import languages, notify, sms
from app.models import Call, SmsMessage
from tests.fakes import (
    AUDIO, TURN_DONE, FakeClient, FakeSession, FakeTransport, audio_msg, caller_said, go_away, handle, tool,
)

MENU = b"\x00\x00" * 1200  # a 50 ms language-menu recording


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ls, "GOODBYE_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(ls, "GOODBYE_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(cf, "MENU_WAIT_SECONDS", 0.3)
    real_start = cf._start_call

    def start(channel, caller):  # a short menu recording, so the menu takes milliseconds
        call_id, snap, _ = real_start(channel, caller)
        return call_id, snap, MENU

    monkeypatch.setattr(cf, "_start_call", start)


def use(monkeypatch, client):
    monkeypatch.setattr(ls, "get_client", lambda: client)


async def run(transport, **kw):
    return await asyncio.wait_for(cf.handle_call(transport, channel="browser_admin", **kw), 10)


def saved(db, call_id) -> Call:
    call = db.get(Call, call_id)
    db.refresh(call)
    return call


async def test_menu_key_picks_the_language_and_the_agent_speaks_only_that(db, monkeypatch):
    s1 = FakeSession([audio_msg("வணக்கம்! நான் Nethmi."), TURN_DONE, caller_said("பத்தாம் தரம் வகுப்பு எப்போது?"),
                      audio_msg("சனிக்கிழமை காலை எட்டு மணிக்கு"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1])
    use(monkeypatch, client)
    t = FakeTransport(keys="2")  # Tamil
    call_id = await run(t)

    assert "Greet them now in Tamil" in s1.notes[0] and "டிகிரி AI Agent-ஐ அழைத்ததற்கு நன்றி" in s1.notes[0]
    assert languages.get("ta").recording_notice not in s1.notes[0]  # the menu recording already said it is recorded
    cfg = client.aio.live.configs[0]
    assert list(cfg.input_audio_transcription.language_codes) == languages.get("ta").transcription_codes
    text = cfg.system_instruction
    assert "This call is in Tamil" in text and "speak only Tamil for the whole call" in text
    assert "Speak natural, colloquial spoken Sinhala" not in text  # no other language's speaking rules
    assert "Speak polite, natural spoken Sri Lankan Tamil" in text
    call = saved(db, call_id)
    assert call.status == "completed" and call.language == "ta" and call.end_reason == "completed"
    assert call.menu_seconds is not None and call.transcript[0]["role"] == "menu" and "pressed 2" in call.transcript[0]["text"]
    assert ("caller", "பத்தாம் தரம் வகுப்பு எப்போது?") in [(x["role"], x["text"]) for x in call.transcript]
    kinds = [e["type"] for e in t.events]
    assert kinds[0] == "started" and "agent" in kinds and kinds[-1] == "ended" and t.closed


async def test_no_key_uses_the_main_language_after_two_plays(db, monkeypatch):
    s1 = FakeSession([audio_msg("ආයුබෝවන්!"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    t = FakeTransport()
    call_id = await run(t)
    assert "Greet them now in Sinhala" in s1.notes[0]
    assert t.sent >= 2 * len(MENU)  # the menu was played twice
    assert saved(db, call_id).language == "si"
    assert "no key pressed" in saved(db, call_id).transcript[0]["text"]


async def test_a_wrong_key_plays_the_menu_again_and_a_right_one_wins(db, monkeypatch):
    s1 = FakeSession([audio_msg("Hello!"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    t = FakeTransport(keys="93")  # 9 is not a language key, 3 is English
    call_id = await run(t)
    assert "Greet them now in English" in s1.notes[0] and saved(db, call_id).language == "en"


async def test_a_menu_played_by_the_dialplan_is_not_played_again(db, monkeypatch):
    s1 = FakeSession([audio_msg("வணக்கம்!"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    t = FakeTransport()
    call_id = await run(t, menu_lang="ta", pbx_menu=True)
    assert "Greet them now in Tamil" in s1.notes[0] and languages.get("ta").recording_notice not in s1.notes[0]
    assert saved(db, call_id).language == "ta"


async def test_the_agent_never_says_the_call_is_recorded_itself(db, monkeypatch):
    s1 = FakeSession([audio_msg("ආයුබෝවන්!"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    monkeypatch.setattr(cf, "_start_call", lambda ch, caller: (*_real(ch, caller)[:2], b""))
    t = FakeTransport()
    call_id = await run(t)
    assert languages.get("si").recording_notice not in s1.notes[0] and saved(db, call_id).language == "si"
    assert not t.notes()  # no menu step


def _real(channel, caller):
    from app.db import SessionLocal
    from app.clock import now_utc
    from app.snapshot import load_snapshot
    with SessionLocal() as db:
        snap = load_snapshot(db)
        call = Call(channel=channel, caller_number=caller, status="in_progress", started_at=now_utc())
        db.add(call)
        db.commit()
        return call.id, snap


# ---------- the call must not drop ----------

async def test_go_away_resumes_with_handle_and_greets_once(db, monkeypatch):
    s1 = FakeSession([handle("h1"), audio_msg("Hello, how can I help?"), TURN_DONE, go_away("5s")])
    s2 = FakeSession([caller_said("When is the Grade 10 class?"), audio_msg("Saturday. Goodbye!"),
                      tool("end_call", reason="caller_goodbye"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    call_id = await run(FakeTransport(keys="3"))
    configs = client.aio.live.configs
    assert configs[0].session_resumption.handle is None and configs[1].session_resumption.handle == "h1"
    assert [n for s in client.aio.live.used for n in s.notes if "A caller has just connected" in n].__len__() == 1
    call = saved(db, call_id)
    assert call.status == "completed" and call.end_reason == "caller_goodbye" and call.reconnects == 1


@pytest.mark.parametrize("code", [1006, 1011, 1013, 503])
async def test_a_dropped_connection_reconnects(db, monkeypatch, code):
    s1 = FakeSession([handle("h9"), audio_msg("Hi"), TURN_DONE, errors.APIError(code, {"message": "dropped"})])
    s2 = FakeSession([audio_msg("Bye now"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    call_id = await run(FakeTransport(keys="1"))
    assert client.aio.live.configs[1].session_resumption.handle == "h9"
    assert saved(db, call_id).reconnects == 1 and saved(db, call_id).status == "completed"


async def test_a_bad_request_after_the_greeting_is_not_retried(db, monkeypatch):
    s1 = FakeSession([audio_msg("Hi"), TURN_DONE, errors.APIError(1008, {"message": "policy"})])
    client = FakeClient([s1, FakeSession([])])
    use(monkeypatch, client)
    call_id = await run(FakeTransport(keys="1"))
    assert len(client.aio.live.configs) == 1  # no second attempt
    call = saved(db, call_id)
    assert call.status == "failed" and call.reconnects == 0


async def test_gives_up_after_five_reconnects(db, monkeypatch):
    monkeypatch.setattr(ls.asyncio, "sleep", _fast_sleep(ls.asyncio.sleep))
    drops = [FakeSession([audio_msg("Hi"), TURN_DONE, errors.APIError(1011, {"message": "x"})])] + [
        FakeSession([errors.APIError(1011, {"message": "x"})]) for _ in range(6)]
    client = FakeClient(drops)
    use(monkeypatch, client)
    call_id = await run(FakeTransport(keys="1"))
    assert len(client.aio.live.configs) == 1 + ls.MAX_RECONNECTS and saved(db, call_id).status == "failed"


def _fast_sleep(real):
    async def sleep(seconds, *a):
        return await real(min(seconds, 0.01), *a)
    return sleep


async def test_reconnect_without_handle_replays_context(db, monkeypatch):
    s1 = FakeSession([audio_msg("Hello"), TURN_DONE, caller_said("Is the Vinuda class on Tuesday?"),
                      audio_msg("Let me check."), TURN_DONE, go_away("1s")])
    s2 = FakeSession([audio_msg("Bye"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    await run(FakeTransport(keys="3"))
    replay = [n for n in s2.notes if "connection was renewed" in n]
    configs = client.aio.live.configs
    assert replay and "So far in this call" in replay[0] + configs[1].system_instruction
    assert "Caller: Is the Vinuda class on Tuesday?" in configs[1].system_instruction
    assert "So far in this call" not in configs[0].system_instruction


async def test_resumed_reconnect_also_gets_the_whole_call_so_far(db, monkeypatch):
    s1 = FakeSession([handle("h1"), audio_msg("Hello"), TURN_DONE, caller_said("Grade 10 theory in Sahitha"),
                      audio_msg("Saturday at eight"), TURN_DONE, go_away("1s")])
    s2 = FakeSession([audio_msg("Bye"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    await run(FakeTransport(keys="3"))
    configs = client.aio.live.configs
    assert configs[1].session_resumption.handle == "h1"
    assert "Caller: Grade 10 theory in Sahitha" in configs[1].system_instruction
    assert "You: Saturday at eight" in configs[1].system_instruction


async def test_gemini_down_plays_the_unavailable_recording_and_fails_the_call(db, monkeypatch):
    use(monkeypatch, FakeClient([errors.APIError(1008, {"message": "API key not valid"})]))
    sorry = b"\x00\x00" * 4800
    from app.models import AgentProfile
    profile = db.get(AgentProfile, 1)
    profile.unavailable_audio = {"si": "sorry.wav"}
    db.commit()
    monkeypatch.setattr(cf, "load_menu_pcm", lambda name: sorry if name == "sorry.wav" else b"")
    t = FakeTransport(keys="1")
    call_id = await run(t)
    call = saved(db, call_id)
    assert call.status == "failed" and "API key" in call.error and call.end_reason == "error"
    assert t.sent >= len(sorry)  # the caller is told, not left in silence
    assert [e["type"] for e in t.events][-1] == "ended" and t.closed


async def test_caller_hangs_up_during_the_agent(db, monkeypatch):
    use(monkeypatch, FakeClient([FakeSession([audio_msg("Hello"), TURN_DONE])]))
    call_id = await run(FakeTransport(keys="1", hang_up_after=0.6))
    call = saved(db, call_id)
    assert call.status == "completed" and call.end_reason == "caller_hung_up"


async def test_caller_hangs_up_in_the_menu(db, monkeypatch):
    client = FakeClient([])
    use(monkeypatch, client)
    call_id = await run(FakeTransport(hang_up_after=0.1))
    call = saved(db, call_id)
    assert call.end_reason == "hung_up_in_menu" and not client.aio.live.configs  # the agent never started


async def test_interruption_clears_playback(db, monkeypatch):
    interrupted = types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))
    s1 = FakeSession([audio_msg("The class is on"), interrupted, caller_said("Just Saturday?"),
                      audio_msg("Yes. Bye"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    t = FakeTransport(keys="3")
    call_id = await run(t)
    assert t.cleared >= 1
    agent_turns = [x for x in saved(db, call_id).transcript if x["role"] == "agent"]
    assert agent_turns[0].get("interrupted") is True


async def test_time_limit(db, monkeypatch):
    use(monkeypatch, FakeClient([FakeSession([audio_msg("Hello"), TURN_DONE])]))
    call_id = await run(FakeTransport(keys="1"), max_seconds=1)
    assert saved(db, call_id).end_reason == "time_limit"


async def test_end_call_without_a_goodbye_returns_the_goodbye_to_say(db, monkeypatch):
    s1 = FakeSession([handle("e1"), audio_msg("Hello"), TURN_DONE, tool("end_call", reason="caller_goodbye"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    call_id = await run(FakeTransport(keys="1"))
    assert saved(db, call_id).end_reason == "caller_goodbye"
    assert s1.tool_responses[0].response["say"] == languages.get("si").common.goodbye


async def test_end_call_after_the_goodbye_says_nothing_more(db, monkeypatch):
    s1 = FakeSession([handle("e2"), audio_msg("Hello"), TURN_DONE,
                      audio_msg("ස්තූතියි. සුබ දවසක්!"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    await run(FakeTransport(keys="1"))
    assert "say" not in s1.tool_responses[0].response


async def test_recording_is_saved_on_the_call(db, monkeypatch):
    s1 = FakeSession([audio_msg("Hello"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    call_id = await run(FakeTransport(keys="1"))
    call = saved(db, call_id)
    assert call.recording_path.startswith(str(call_id)) and call.has_recording


# ---------- sending the details ----------

@pytest.fixture
def sms_works(monkeypatch):
    sent = []
    monkeypatch.setattr(sms, "setup_problem", lambda: "")
    monkeypatch.setattr(sms, "_deliver", lambda phone, text: (sms.sms_number(phone), "sent", "") if not sent.append((phone, text)) else None)
    return sent


async def test_the_agent_sends_the_summary_to_the_caller_id_number(db, monkeypatch, sms_works):
    s1 = FakeSession([audio_msg("Hello"), TURN_DONE,
                      tool("send_details", topics=["industry"], industry="salon"),  # asks if the caller ID is right
                      tool("send_details", topics=["industry"], industry="salon"),  # the caller said yes
                      audio_msg("Sent. Goodbye!"), tool("end_call", reason="details_sent"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    call_id = await run(FakeTransport(keys="3"), caller_number="0771234567")
    assert "077 123 4567" in s1.tool_responses[0].response["say"]
    assert s1.tool_responses[1].response["say"] == languages.get("en").send.sent
    assert len(sms_works) == 1
    phone, text = sms_works[0]
    assert phone == "0771234567" and "Salons & beauty" in text and "Hair and beauty appointments" in text
    assert "Table reservation requests" not in text  # only the business the caller asked about
    row = db.query(SmsMessage).one()
    assert row.call_id == call_id and row.status == "sent" and row.channel == "sms" and row.language == "en"


async def test_a_landline_is_refused_and_nothing_is_sent(db, monkeypatch, sms_works):
    s1 = FakeSession([audio_msg("Hello"), TURN_DONE, tool("send_details", topics=["contact"], phone="0112345678"),
                      tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    await run(FakeTransport(keys="3"))
    assert s1.tool_responses[0].response["say"] == languages.get("en").send.not_mobile
    assert not sms_works and db.query(SmsMessage).count() == 0


async def test_with_no_provider_the_agent_says_so_and_nothing_breaks(db, monkeypatch):
    s1 = FakeSession([audio_msg("Hello"), TURN_DONE, tool("send_details", topics=["all"]),
                      tool("send_details", topics=["all"]), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    call_id = await run(FakeTransport(keys="3"), caller_number="0771234567")
    assert s1.tool_responses[1].response["say"] == languages.get("en").send.not_set_up
    assert saved(db, call_id).status == "completed"


# ---------- what is saved when something goes wrong in the middle ----------

def voiced_24k(f0: float, seconds: float = 2.0) -> bytes:
    import numpy as np
    t = np.arange(int(24000 * seconds)) / 24000
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
    return (4000 * x).astype("<i2").tobytes()


def voice_msg(f0: float, text: str = "", seconds: float = 2.0):
    m = audio_msg(text)
    m.server_content.model_turn.parts[0].inline_data.data = voiced_24k(f0, seconds)
    return m


async def test_the_call_is_saved_while_it_goes_on(db, monkeypatch):
    monkeypatch.setattr(cf, "CHECKPOINT_SECONDS", 0.05)
    use(monkeypatch, FakeClient([FakeSession([audio_msg("Hello there"), TURN_DONE])]))
    task = asyncio.create_task(cf.handle_call(FakeTransport(keys="3"), channel="browser_admin"))
    await asyncio.sleep(1.2)
    row = db.query(Call).order_by(Call.id.desc()).first()
    db.refresh(row)
    assert row.status == "in_progress" and row.language == "en"
    assert ("agent", "Hello there") in [(x["role"], x["text"]) for x in row.transcript]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_stopped_server_leaves_the_call_interrupted_with_what_was_said(db, monkeypatch):
    monkeypatch.setattr(cf, "CHECKPOINT_SECONDS", 0.05)
    use(monkeypatch, FakeClient([FakeSession([audio_msg("Hello there"), TURN_DONE])]))
    task = asyncio.create_task(cf.handle_call(FakeTransport(keys="3"), channel="browser_admin"))
    await asyncio.sleep(1.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = db.query(Call).order_by(Call.id.desc()).first()
    db.refresh(row)
    assert row.status == "interrupted" and row.end_reason == "server_stopped" and row.ended_at is not None
    assert row.language == "en" and ("agent", "Hello there") in [(x["role"], x["text"]) for x in row.transcript]
    assert row.recording_path.startswith(str(row.id))


def test_startup_keeps_what_an_unfinished_call_saved(db):
    from datetime import timedelta
    from app import main
    from app.clock import now_utc
    from app.config import get_settings
    start = now_utc()
    call = Call(channel="pbx", status="in_progress", started_at=start, language="si", duration_seconds=42,
                transcript=[{"role": "agent", "text": "ආයුබෝවන්!", "t": 9.0}], transcript_check="checking")
    db.add(call)
    db.commit()
    folder = get_settings().recordings_path
    folder.mkdir(parents=True, exist_ok=True)
    from app.telephony.recording import delete_recordings
    delete_recordings(call.id)  # a leftover from an earlier test with the same id
    (folder / f"{call.id}.wav").write_bytes(b"RIFF")
    main._startup_db()
    db.refresh(call)
    assert call.status == "interrupted" and call.end_reason == "server_restart"
    assert call.transcript[0]["text"] == "ආයුබෝවන්!" and call.language == "si"
    assert call.ended_at == start + timedelta(seconds=42) and call.recording_path == f"{call.id}.wav"
    assert call.transcript_check == "failed"


# ---------- a voice of the wrong gender ----------

MENU_CLEARS = 1  # the language menu stops its recording once the key is pressed


def agent_lines(call: Call) -> list[str]:
    return [x["text"] for x in call.transcript if x["role"] == "agent"]


def notes(call: Call) -> list[str]:
    return [x["text"] for x in call.transcript if x["role"] == "note"]


async def test_a_male_sounding_greeting_is_cut_off_and_said_again(db, monkeypatch):
    s1 = FakeSession([voice_msg(115, "Hello"), TURN_DONE])  # the profile's voice is female (Aoede)
    s2 = FakeSession([voice_msg(190, "Hello"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    transport = FakeTransport(keys="3")
    call_id = await run(transport)
    assert len(client.aio.live.configs) == 2 and client.aio.live.configs[1].session_resumption.handle is None
    assert transport.cleared == MENU_CLEARS + 1  # the male-sounding greeting was dropped from the line
    assert s2.notes == s1.notes  # the new session is cued to greet, not to "carry on"
    call = saved(db, call_id)
    assert agent_lines(call) == ["Hello"] and any("reply cut off and said again" in n for n in notes(call))
    assert call.status == "completed"


async def test_a_reply_in_the_wrong_voice_is_cut_off_and_said_again_in_full(db, monkeypatch):
    s1 = FakeSession([voice_msg(190, "Hello"), TURN_DONE, caller_said("When are the grade 10 classes?"),
                      audio_msg("Theory or"), voice_msg(115, " paper?"), TURN_DONE])
    s2 = FakeSession([voice_msg(190, "Theory or paper?"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    transport = FakeTransport(keys="3")
    call_id = await run(transport)
    from app.ai.prompt_builder import redo_note
    assert len(client.aio.live.configs) == 2 and client.aio.live.configs[1].session_resumption.handle is None
    assert transport.cleared == MENU_CLEARS + 1 and s2.notes == [redo_note("Theory or")]
    recap = client.aio.live.configs[1].system_instruction
    assert "Caller: When are the grade 10 classes?" in recap and "Theory or" not in recap  # the cut part wasn't heard
    call = saved(db, call_id)
    assert agent_lines(call) == ["Hello", "Theory or paper?"]
    assert any(n.endswith("reply cut off and said again: Theory or") for n in notes(call))


async def test_a_short_wrong_voice_reply_is_held_back_and_never_played(db, monkeypatch):
    s1 = FakeSession([voice_msg(190, "Hello"), TURN_DONE, caller_said("Hi"),
                      voice_msg(115, "Which grade?", seconds=1.8), TURN_DONE])
    s2 = FakeSession([voice_msg(190, "Which grade?"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    transport = FakeTransport(keys="3")
    call_id = await run(transport)
    from app.ai.prompt_builder import redo_note
    assert transport.cleared == MENU_CLEARS + 1 and s2.notes == [redo_note("")]  # cut before its text arrived
    assert agent_lines(saved(db, call_id)) == ["Hello", "Which grade?"]


async def test_a_wrong_voice_is_never_sent_to_the_caller(db, monkeypatch):
    low, good = voiced_24k(115), voiced_24k(190)

    class Recording(FakeTransport):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.chunks = []

        async def send(self, pcm):
            self.chunks.append(bytes(pcm))
            await super().send(pcm)

    s1 = FakeSession([voice_msg(115, "Hello"), TURN_DONE])
    s2 = FakeSession([voice_msg(190, "Hello"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1, s2]))
    transport = Recording(keys="3")
    await run(transport)
    assert low not in transport.chunks and good in transport.chunks


async def test_a_held_reply_that_is_interrupted_is_dropped(db, monkeypatch):
    interrupted = types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))
    s1 = FakeSession([audio_msg("The class is on"), interrupted, caller_said("Just Saturday?"),
                      audio_msg("Yes. Bye"), tool("end_call", reason="completed"), TURN_DONE])
    use(monkeypatch, FakeClient([s1]))
    t = FakeTransport(keys="3")
    await run(t)
    assert t.cleared >= 2  # the menu, then the interruption: the first 100 ms was never played


async def test_a_turn_too_short_to_judge_while_playing_is_checked_when_it_ends(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "voice_hold_seconds", 0)  # replies play straight away
    s1 = FakeSession([voice_msg(190, "Hello"), TURN_DONE, caller_said("Hi"),
                      voice_msg(115, "Which grade?", seconds=1.8), TURN_DONE])
    s2 = FakeSession([voice_msg(190, "Grade 10?"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1, s2])
    use(monkeypatch, client)
    transport = FakeTransport(keys="3")
    call_id = await run(transport)
    from app.ai.prompt_builder import resume_note
    assert len(client.aio.live.configs) == 2 and client.aio.live.configs[1].session_resumption.handle is None
    assert transport.cleared == MENU_CLEARS and s2.notes == [resume_note()]  # it was heard in full: carry on, no repeat
    call = saved(db, call_id)
    assert agent_lines(call) == ["Hello", "Which grade?", "Grade 10?"]
    assert any("connection renewed" in n for n in notes(call))


async def test_the_live_temperature_is_capped(db, monkeypatch):
    from app.models import AgentProfile
    client = FakeClient([FakeSession([audio_msg("Hi"), tool("end_call", reason="completed"), TURN_DONE]),
                         FakeSession([audio_msg("Hi"), tool("end_call", reason="completed"), TURN_DONE])])
    use(monkeypatch, client)
    await run(FakeTransport(keys="3"))
    db.get(AgentProfile, 1).temperature = 0.5
    db.commit()
    await run(FakeTransport(keys="3"))
    assert [c.temperature for c in client.aio.live.configs] == [1.0, 0.5]


async def test_a_normal_female_voice_is_left_alone(db, monkeypatch):
    s1 = FakeSession([voice_msg(190, "Hello"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([s1])
    use(monkeypatch, client)
    transport = FakeTransport(keys="3")
    await run(transport)
    assert len(client.aio.live.configs) == 1 and transport.cleared == MENU_CLEARS


async def test_a_wrong_voice_is_replaced_a_limited_number_of_times(db, monkeypatch):
    low = lambda: FakeSession([voice_msg(115, "Hello"), TURN_DONE])
    last = FakeSession([voice_msg(115, "Bye"), tool("end_call", reason="completed"), TURN_DONE])
    client = FakeClient([*[low() for _ in range(ls.MAX_VOICE_RENEWALS)], last])
    use(monkeypatch, client)
    await run(FakeTransport(keys="3"))
    assert len(client.aio.live.configs) == 1 + ls.MAX_VOICE_RENEWALS


async def test_the_transcript_check_tries_again_when_gemini_is_busy(db, monkeypatch):
    from app.ai import transcript_check as tc
    monkeypatch.setattr(tc, "RETRY_WAITS", (0, 0))
    monkeypatch.setattr(tc, "get_settings", lambda: type("S", (), {"gemini_transcribe_model": "m"})())
    calls = []

    class Models:
        async def generate_content(self, **kw):
            calls.append(1)
            if len(calls) < 3:
                raise errors.ServerError(503, {"error": {"message": "busy"}})
            return type("R", (), {"parsed": []})()

    monkeypatch.setattr(tc, "get_client", lambda: type("C", (), {"aio": type("A", (), {"models": Models()})()})())
    assert await tc._hear([], "p") == [] and len(calls) == 3
    calls.clear()

    class Bad(Models):
        async def generate_content(self, **kw):
            calls.append(1)
            raise errors.ClientError(400, {"error": {"message": "bad"}})

    monkeypatch.setattr(tc, "get_client", lambda: type("C", (), {"aio": type("A", (), {"models": Bad()})()})())
    with pytest.raises(errors.ClientError):
        await tc._hear([], "p")
    assert len(calls) == 1
