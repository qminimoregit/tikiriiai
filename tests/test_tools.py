import asyncio

import pytest

from app import languages, notify, sms
from app.ai import tools
from app.models import AgentProfile, Lead, SmsMessage
from app.snapshot import load_snapshot


def ctx(db, language="en", caller="", asked=True):
    """asked=True: the caller-ID question is treated as already put to the caller (the tests of the guard pass False)."""
    return tools.CallContext(call_id=1, channel="pbx", caller_number=caller, snap=load_snapshot(db), language=language,
                             caller_id_asked=asked)


def call(name, args, c):
    return asyncio.run(tools.dispatch(name, args, c))


@pytest.fixture
def provider(monkeypatch):
    sent = []
    monkeypatch.setattr(sms, "setup_problem", lambda: "")

    def deliver(phone, text):
        sent.append((phone, text))
        return sms.sms_number(phone), "sent", ""
    monkeypatch.setattr(sms, "_deliver", deliver)
    return sent


def test_every_tool_but_end_call_is_blocking():
    for d in tools.DECLARATIONS:
        assert (d.get("behavior") == "BLOCKING") == (d["name"] != "end_call")


def test_check_phone_gives_the_words_to_read_back(db):
    r = call("check_phone", {"phone": "0771234567"}, c := ctx(db, "si"))
    assert r["phone"] == "077 123 4567" and r["phone_say"].startswith("බිංදුවයි හැත්තෑ හතයි") and c.phone == "0771234567"
    assert r["say"] == languages.get("si").common.read_back_phone.format(phone=r["phone_say"])


def test_check_phone_asks_again_for_a_short_number(db):
    r = call("check_phone", {"phone": "077123"}, ctx(db, "en"))
    assert "error" in r and r["say"] == languages.get("en").common.phone_ten_digits


def test_check_phone_warns_about_a_landline(db):
    assert "landline" in call("check_phone", {"phone": "0112345678"}, ctx(db))["note"]


def test_send_details_uses_the_caller_id_and_records_the_message(db, provider):
    r = call("send_details", {"topics": ["industry"], "industry": "restaurant"}, ctx(db, "en", "+94771234567"))
    assert r["say"] == languages.get("en").send.sent and r["phone"] == "077 123 4567"
    assert provider[0][0] == "0771234567" and "Table reservation requests" in provider[0][1]
    assert db.query(SmsMessage).one().status == "sent"


def test_send_details_prefers_the_number_check_phone_accepted(db, provider):
    c = ctx(db, "en", "0771234567")
    call("check_phone", {"phone": "0712345678"}, c)
    call("send_details", {"topics": ["capabilities"]}, c)
    assert provider[0][0] == "0712345678"


def test_the_same_message_is_not_sent_twice_in_one_call(db, provider):
    c = ctx(db, "en", "0771234567")
    for _ in range(2):
        call("send_details", {"topics": ["all"]}, c)
    assert len(provider) == 1


def test_a_landline_cannot_receive_the_details(db, provider):
    r = call("send_details", {"topics": ["all"], "phone": "0112345678"}, ctx(db, "en"))
    assert r["say"] == languages.get("en").send.not_mobile and not provider


def test_no_number_at_all_asks_for_one(db, provider):
    r = call("send_details", {"topics": ["all"]}, ctx(db, "ta"))
    assert r["say"] == languages.get("ta").send.ask_mobile and not provider


def test_without_a_provider_the_caller_is_told_and_nothing_is_saved(db):
    r = call("send_details", {"topics": ["all"]}, ctx(db, "en", "0771234567"))
    assert r["say"] == languages.get("en").send.not_set_up and db.query(SmsMessage).count() == 0


def test_with_the_channel_off_nothing_is_sent(db, provider):
    db.get(AgentProfile, 1).message_channel = "off"
    db.commit()
    assert call("send_details", {"topics": ["all"]}, ctx(db, "en", "0771234567"))["say"] == languages.get("en").send.not_set_up
    assert not provider


def test_whatsapp_first_falls_back_to_sms_when_whatsapp_is_not_set_up(db, provider):
    db.get(AgentProfile, 1).message_channel = "whatsapp_first"
    db.commit()
    call("send_details", {"topics": ["all"]}, ctx(db, "en", "0771234567"))
    assert [r.channel for r in db.query(SmsMessage)] == ["sms"] and len(provider) == 1


def test_both_sends_on_each_channel(db, provider, monkeypatch):
    from app import whatsapp
    db.get(AgentProfile, 1).message_channel = "both"
    db.commit()
    monkeypatch.setattr(whatsapp, "setup_problem", lambda: "")
    monkeypatch.setattr(whatsapp, "send_template", lambda n, t, lang, params: ("sent", "", "wamid.1"))
    call("send_details", {"topics": ["all"]}, ctx(db, "si", "0771234567"))
    rows = {r.channel: r for r in db.query(SmsMessage)}
    assert set(rows) == {"sms", "whatsapp"} and rows["whatsapp"].provider_id == "wamid.1"


def test_a_failing_send_never_breaks_the_tool(db, monkeypatch):
    monkeypatch.setattr(sms, "setup_problem", lambda: "")
    monkeypatch.setattr(sms, "_deliver", lambda phone, text: 1 / 0)
    r = call("send_details", {"topics": ["all"]}, ctx(db, "en", "0771234567"))
    assert "say" in r  # the model is told it went; the failure is logged, never raised


def test_end_call_hands_back_the_goodbye_unless_it_was_said(db):
    c = ctx(db, "ta")
    r = call("end_call", {"reason": "completed"}, c)
    assert c.ended and r["say"] == languages.get("ta").common.goodbye
    c2 = ctx(db)
    c2.said_goodbye = True
    assert "say" not in call("end_call", {"reason": "details_sent"}, c2) and c2.end_reason == "details_sent"


def test_a_broken_tool_returns_an_error_instead_of_raising(db, monkeypatch):
    async def boom(args, c):
        raise RuntimeError("db down")
    monkeypatch.setitem(tools.HANDLERS, "check_phone", boom)
    assert "db down" in call("check_phone", {"phone": "0771234567"}, ctx(db))["error"]
    assert "error" in call("nonsense", {}, ctx(db))


# ---------- book_demo ----------

DEMO = {"name": "Nimal Perera", "business_name": "Glow Salon", "industry": "salon", "languages": ["si", "en", "xx"],
        "preferred_time": "tomorrow morning", "notes": "Bookings after hours"}


def book(args, c):
    """book_demo as the agent uses it: called again with the same details after each yes (the caller-ID question,
    then the read-back), until it stops asking. The last result."""
    for _ in range(3):
        r = call("book_demo", args, c)
        if not r.get("error", "").startswith(("Not saved yet", "The caller's number is not confirmed")):
            break
    return r


def test_book_demo_saves_the_lead_with_the_caller_id(db):
    c = ctx(db, "si", "+94771234567")
    r = book(DEMO, c)
    assert r["say"] == languages.get("si").lead.booked and r["phone"] == "077 123 4567"
    lead = db.query(Lead).one()
    assert (lead.name, lead.business_name, lead.industry, lead.phone) == ("Nimal Perera", "Glow Salon", "salon", "0771234567")
    assert lead.languages == ["si", "en"] and lead.language == "si" and lead.status == "new" and lead.call_id == 1
    assert c.lead_id == lead.id


def test_book_demo_prefers_the_number_check_phone_accepted(db):
    c = ctx(db, "en", "0771234567")
    call("check_phone", {"phone": "0712345678"}, c)
    book(DEMO, c)
    assert db.query(Lead).one().phone == "0712345678"


def test_book_demo_needs_a_name_business_time_and_number(db):
    en = languages.get("en").lead
    r = call("book_demo", {**DEMO, "name": " "}, ctx(db, "en", "0771234567"))
    assert "error" in r and r["say"] == en.ask_name
    r = call("book_demo", {**DEMO, "business_name": ""}, ctx(db, "en", "0771234567"))
    assert "error" in r and r["say"] == en.ask_business
    r = call("book_demo", {**DEMO, "preferred_time": " "}, ctx(db, "en", "0771234567"))
    assert "error" in r and r["say"] == en.ask_time
    r = call("book_demo", DEMO, ctx(db, "ta"))
    assert "error" in r and r["say"] == languages.get("ta").common.ask_phone
    r = call("book_demo", {**DEMO, "phone": "0771"}, ctx(db, "en"))
    assert r["say"] == languages.get("en").common.phone_ten_digits
    assert db.query(Lead).count() == 0


def test_book_demo_takes_a_landline(db):
    book({**DEMO, "phone": "0112345678"}, ctx(db, "en"))
    assert db.query(Lead).one().phone == "0112345678"  # the team can call a landline back


def test_the_team_gets_an_sms_for_a_new_lead(db, provider, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "lead_notify_phone", "0719998888")
    book(DEMO, ctx(db, "si", "0771234567"))
    alerts = [p for p in provider if p[0] == "0719998888"]
    assert len(alerts) == 1
    text = alerts[0][1]
    assert text.startswith("New TIKIRII demo request: Nimal Perera")
    assert "Glow Salon" in text and "Phone: 0771234567" in text and "tomorrow morning" in text and "si, en" in text
    row = db.query(SmsMessage).filter_by(kind="lead_alert").one()
    assert row.status == "sent"


def test_without_an_alert_number_only_the_lead_is_saved(db):
    book(DEMO, ctx(db, "en", "0771234567"))
    assert db.query(SmsMessage).count() == 0 and db.query(Lead).count() == 1


# ---------- reading the details back before saving ----------

def test_the_details_are_read_back_before_anything_is_saved(db):
    c = ctx(db, "en", "0771234567")
    r = call("book_demo", DEMO, c)
    assert "error" in r and db.query(Lead).count() == 0
    assert r["say"].startswith("Let me check the details. Your name is Nimal Perera, your business is Glow Salon, "
                               "and our team will call you tomorrow morning on 077 123 4567.")
    assert r["say"].endswith("Is that correct?")
    r = call("book_demo", DEMO, c)  # the caller said yes
    assert r["say"] == languages.get("en").lead.booked and db.query(Lead).count() == 1


def test_the_same_details_with_other_spacing_or_case_are_the_same(db):
    c = ctx(db, "en", "0771234567")
    call("book_demo", DEMO, c)
    r = call("book_demo", {**DEMO, "name": "nimal  PERERA ", "business_name": "glow salon"}, c)
    assert "error" not in r and db.query(Lead).count() == 1


def test_a_correction_is_read_back_again(db):
    c = ctx(db, "en", "0771234567")
    call("book_demo", DEMO, c)
    r = call("book_demo", {**DEMO, "business_name": "Glow Beauty"}, c)  # "no, it is Glow Beauty"
    assert "error" in r and "Glow Beauty" in r["say"] and db.query(Lead).count() == 0
    call("book_demo", {**DEMO, "business_name": "Glow Beauty"}, c)
    assert db.query(Lead).one().business_name == "Glow Beauty"


def test_the_read_back_is_in_the_call_language(db):
    r = call("book_demo", DEMO, ctx(db, "si", "0771234567"))
    assert r["say"].startswith("විස්තර එක පාරක් බලමු.") and "Nimal Perera" in r["say"] and "Glow Salon" in r["say"]
    assert "බිංදුවයි" in r["say"]  # the number in Sinhala words, not digits


# ---------- correcting a saved booking ----------

def test_a_saved_booking_is_corrected_not_duplicated(db, provider, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "lead_notify_phone", "0719998888")
    c = ctx(db, "en", "0771234567")
    book(DEMO, c)
    first = c.lead_id
    fixed = {**DEMO, "preferred_time": "Friday afternoon"}
    r = call("book_demo", fixed, c)  # "sorry, the time is wrong"
    assert "error" in r and "Friday afternoon" in r["say"]
    r = call("book_demo", fixed, c)
    assert r["say"] == languages.get("en").lead.updated_sms
    lead = db.query(Lead).one()
    assert lead.id == first and lead.preferred_time == "Friday afternoon" and lead.business_name == "Glow Salon"
    to_caller = [t for p, t in provider if p == "0771234567"]
    assert len(to_caller) == 2 and to_caller[1].startswith("TIKIRII: Nimal Perera, your demo request has been updated.")
    assert "Friday afternoon" in to_caller[1]
    alerts = [t for p, t in provider if p == "0719998888"]
    assert alerts[0].startswith("New TIKIRII") and alerts[1].startswith("Updated TIKIRII demo request")


def test_the_same_details_again_are_already_booked(db):
    c = ctx(db, "en", "0771234567")
    book(DEMO, c)
    r = call("book_demo", DEMO, c)
    assert r["say"] == languages.get("en").lead.already_booked and db.query(Lead).count() == 1


# ---------- the confirmation SMS to the caller ----------

def test_the_caller_gets_a_confirmation_sms_in_the_call_language(db, provider):
    r = book(DEMO, ctx(db, "si", "0771234567"))
    assert r["say"] == languages.get("si").lead.booked_sms
    assert len(provider) == 1
    phone, text = provider[0]
    si = languages.get("si").sms
    assert phone == "0771234567"
    assert text.split("\n") == ["TIKIRII: ස්තූතියි Nimal Perera. ඔයාගේ නොමිලේ demo ඉල්ලීම book කළා.",
                                 "ව්‍යාපාරය: Glow Salon", "අපි කතා කරන්නේ: tomorrow morning", si.demo_footer]
    row = db.query(SmsMessage).one()
    assert (row.kind, row.topics, row.status, row.language, row.call_id) == ("demo_confirm", "demo", "sent", "si", 1)


def test_no_confirmation_sms_to_a_landline(db, provider):
    r = book({**DEMO, "phone": "0112345678"}, ctx(db, "en"))
    assert r["say"] == languages.get("en").lead.booked and not provider


def test_no_confirmation_sms_with_the_channel_off_or_whatsapp_only(db, provider):
    for mode in ("off", "whatsapp"):
        db.query(Lead).delete()
        db.get(AgentProfile, 1).message_channel = mode
        db.commit()
        r = book(DEMO, ctx(db, "en", "0771234567"))
        assert r["say"] == languages.get("en").lead.booked
    assert not provider


def test_no_confirmation_sms_when_sms_is_not_set_up(db):
    r = book(DEMO, ctx(db, "en", "0771234567"))
    assert r["say"] == languages.get("en").lead.booked and db.query(SmsMessage).count() == 0


# ---------- the caller-ID question ----------

def test_book_demo_first_asks_whether_the_caller_id_is_the_number(db):
    c = ctx(db, "en", "+94771234567", asked=False)
    r = call("book_demo", DEMO, c)
    assert "error" in r and db.query(Lead).count() == 0 and c.lead_id is None
    assert r["say"] == "Can our team call you back on the number you are calling from, 077 123 4567?"
    r = call("book_demo", DEMO, c)  # the caller said yes: now the details are read back
    assert r["say"].startswith("Let me check the details.")
    r = call("book_demo", DEMO, c)
    assert r["say"] == languages.get("en").lead.booked and db.query(Lead).one().phone == "0771234567"


def test_the_question_is_said_in_the_call_language(db):
    r = call("book_demo", DEMO, ctx(db, "si", "0771234567", asked=False))
    assert r["say"].startswith("ඔයා දැන් කතා කරන")


def test_no_caller_id_question_when_a_number_is_given_or_checked(db):
    c = ctx(db, "en", "0771234567", asked=False)
    r = book({**DEMO, "phone": "0712345678"}, c)
    assert r["say"] == languages.get("en").lead.booked and db.query(Lead).one().phone == "0712345678"
    assert c.caller_id_asked is False
    c = ctx(db, "en", "0771234567", asked=False)
    call("check_phone", {"phone": "0765555555"}, c)
    call("send_details", {"topics": ["all"]}, c)
    assert c.caller_id_asked is False


def test_a_bad_number_gets_the_ten_digits_line_not_the_caller_id_question(db):
    r = call("book_demo", {**DEMO, "phone": "0771"}, ctx(db, "en", "0771234567", asked=False))
    assert r["say"] == languages.get("en").common.phone_ten_digits


def test_no_caller_id_question_without_a_usable_caller_id(db):
    for caller in ("", "ucmtrunk2", "1527831296"):
        r = call("book_demo", DEMO, ctx(db, "en", caller, asked=False))
        assert r["say"] == languages.get("en").common.ask_phone


def test_the_caller_is_asked_once_for_the_demo_and_the_summary(db, provider):
    c = ctx(db, "en", "0771234567", asked=False)
    assert book(DEMO, c)["say"] == languages.get("en").lead.booked_sms
    r = call("send_details", {"topics": ["all"]}, c)
    assert "error" not in r  # already confirmed for the demo


def test_send_details_asks_with_its_own_line_first(db, provider):
    c = ctx(db, "en", "0771234567", asked=False)
    r = call("send_details", {"topics": ["all"]}, c)
    assert "error" in r and r["say"] == "Shall I send it to the number you are calling from, 077 123 4567?"
    assert not provider
    assert "error" not in call("send_details", {"topics": ["all"]}, c)
    r = call("book_demo", DEMO, c)  # no second number question: straight to the read-back
    assert r["say"].startswith("Let me check the details.")
