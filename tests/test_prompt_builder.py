from datetime import datetime

import pytest

from app import languages
from app.ai import prompt_builder as pb
from app.models import AgentProfile
from app.snapshot import load_snapshot

NOW = datetime(2026, 10, 13, 9, 0)  # Tuesday 14:30 in Colombo (naive UTC)


def build(db, language="si", **kw):
    return pb.build_system_instruction(load_snapshot(db), NOW, language=language, **kw)


def test_the_product_knowledge_is_in_the_prompt(db):
    text = build(db, "en")
    snap = load_snapshot(db)
    assert len(snap.capabilities) == 9 and len(snap.industries) == 15 and len(snap.integrations) == 7
    for words in ("Appointments, booked", "Outgoing calls, automated", "Google Calendar", "n8n", "REST API",
                  "Which languages does TIKIRII speak?", "Lite: 5 hours", "LKR 14,990", "LKR 229,990",
                  "Extra 5 hours (300 minutes): LKR 9,500"):
        assert words in text, words
    assert "teacher" not in text.lower() and "grade" not in text.lower()


def test_every_industry_is_one_line_with_its_tasks_and_its_name_in_the_call_language(db):
    text = build(db, "si")
    assert "- Salons & beauty (Sinhala: සැලෝන් සහ රූපලාවණ්‍ය) — " in text
    assert "Hair and beauty appointments, Services and pricing enquiries" in text
    assert '"Can I book a haircut for Saturday?"' in text
    assert "- Restaurants (Tamil: உணவகங்கள்) — " in build(db, "ta")
    assert "(English: " not in build(db, "en")  # the English name is the name


def test_the_agent_says_it_is_the_product_and_gives_only_the_listed_prices(db):
    text = build(db, "en")
    assert "the caller is hearing the product working right now" in text
    assert "Give prices only exactly as written above" in text
    assert "calls a day times minutes per call times 30" in text
    assert "once you have answered a price question, offer a free demo" in text
    assert "say honestly that you are an AI voice agent" in text


def test_contact_details_are_said_the_way_written(db):
    text = build(db, "en")
    assert "- Email (say it as: tikirii a i at gmail dot com)" in text
    assert "There is no other phone number to give" in text  # no sales phone in the seed
    db.get(AgentProfile, 1).sales_phone = "0771234567"
    db.commit()
    assert "Sales phone, which you may give: 077 123 4567" in build(db, "en")


@pytest.mark.parametrize("code, name", [("si", "Sinhala"), ("ta", "Tamil"), ("en", "English")])
def test_the_call_is_locked_to_the_chosen_language(db, code, name):
    text = build(db, code)
    assert f"This call is in {name}" in text and f"speak only {name} for the whole call" in text
    assert languages.get(code).common.other_language in text
    assert "press the # key to go back to the main menu" in text
    others = [c for c in ("si", "ta", "en") if c != code]
    for other in others:  # none of the other languages' speaking rules or lines are in the prompt
        assert languages.get(other).common.other_language not in text
        assert languages.get(other).style_rules.strip().splitlines()[0] not in text


def test_the_language_rules_come_last_so_they_win(db):
    text = build(db)
    assert text.rindex("## Language (follow strictly)") > text.rindex("## Scope and honesty")
    assert text.rindex("## Scope and honesty") > text.rindex("## Kinds of business it works for")


def test_booking_a_demo_uses_the_language_lines(db):
    for code in ("si", "ta", "en"):
        lead = languages.get(code).lead
        text = build(db, code)
        assert f'"{lead.offer}"' in text and f'"{lead.ask_name}"' in text and f'"{lead.ask_time}"' in text
        assert "call book_demo" in text and "never read them back yourself" in text
        assert "updates the same request" in text  # a correction after saving, not a second request


def test_a_known_caller_id_is_offered_for_the_demo_and_the_message(db):
    assert "use_caller_id" not in build(db)  # a line key is never leaked
    with_id = build(db, "en", channel="pbx", caller_number="+94771234567")
    assert "The caller is phoning from 077 123 4567" in with_id and "without a phone" in with_id
    assert "with phone empty" in with_id and "its 'say' asks" in with_id
    without = build(db, "en", channel="pbx")
    assert "Ask for the mobile number" in without and "Ask for a mobile number" in without


def test_with_messages_off_the_agent_does_not_offer_to_send(db):
    db.get(AgentProfile, 1).message_channel = "off"
    db.commit()
    text = build(db)
    assert "Sending a summary to the caller's phone" not in text and "## Booking a demo" in text


def test_agent_instructions_from_the_team_are_included(db):
    text = build(db)
    assert "Instructions from the TIKIRII team" in text and "Find out early what kind of business" in text


def test_the_default_greeting_in_each_language(db):
    snap = load_snapshot(db)
    si, ta, en = (pb.greeting_text(snap, c) for c in ("si", "ta", "en"))
    assert si.startswith("ටිකිරි AI Agent වෙත ඇමතූ ඔබට ස්තුතියි.") and si.endswith("ඔබ කැමතිද?")
    assert ta.startswith("டிகிரி AI Agent-ஐ அழைத்ததற்கு நன்றி.") and ta.endswith("விரும்புகிறீர்களா?")
    assert en.startswith("Thank you for calling the Tikirii AI Agent.") and en.endswith("Would you like to know more about this?")


def test_a_greeting_the_admin_wrote_wins(db):
    db.get(AgentProfile, 1).greetings = {"en": "Hi from {company}! I'm {agent}."}
    db.commit()
    assert pb.greeting_text(load_snapshot(db), "en") == "Hi from TIKIRII! I'm Tikirii."


def test_the_recap_keeps_what_the_caller_gave():
    recap = pb.call_recap([{"role": "agent", "text": "What is your business?"}, {"role": "caller", "text": "A salon"}])
    assert "You: What is your business?" in recap and "Caller: A salon" in recap and "Never greet again" in recap
    assert pb.call_recap([]) == ""


def test_a_reply_cut_off_for_its_voice_is_said_again():
    note = pb.redo_note("TIKIRII can")
    assert '(it began: "TIKIRII can")' in note and "again" in note
    assert "it began" not in pb.redo_note("")


def test_a_wrong_answer_is_apologised_for_once(db):
    for code in ("si", "ta", "en"):
        text = build(db, code)
        sorry = languages.get(code).common.sorry_wrong
        assert f'"{sorry}"' in text and "your answer was wrong or not what they asked" in text
        assert text.index("your answer was wrong") > text.index("## Scope and honesty")


def test_the_prompt_is_a_sensible_size(db):
    assert len(build(db, "si")) < 30000 * 3  # under the admin preview's warning


def test_the_demo_asks_to_call_back_on_the_caller_id_not_to_send(db):
    text = build(db, "en", channel="pbx", caller_number="0771234567")
    demo = text[text.index("## Booking a demo"):text.index("## Sending a summary")]
    assert "Can our team call you back on the number you are calling from, 077 123 4567?" in demo
    assert "Shall I send it" not in demo


def test_sinhala_prices_come_with_their_words_and_english_keeps_digits(db):
    si = build(db, "si")
    assert "LKR 119,990 (say: එක් ලක්ෂ දහනව දහස් නවසිය අනූව)" in si
    assert "LKR 229,990 (say: දෙලක්ෂ විසිනව දහස් නවසිය අනූව)" in si
    assert "LKR 14,990 (say: දාහතර දහස් නවසිය අනූව)" in si
    assert "Starter" not in si and "8,150" not in si
    assert "Say exactly those words and never read the digits yourself" in si
    en = build(db, "en")
    assert "LKR 119,990" in en and "(say:" not in en.split("## Price")[1].split("## Getting started")[0]


def test_say_amount_in_sinhala_and_none_for_languages_without_words():
    from app import languages
    s = languages.get("si").spoken_numbers
    assert s.say_amount(5000) == "පන්දහස" and s.say_amount(9500) == "නවදහස් පන්සියය"
    assert s.say_amount(7777) == "හත දහස් හත්සිය හැත්තෑ හත"  # not in the table: the formula
    assert s.say_amount(0) == "" and s.say_amount(1_000_000) == ""
    assert languages.get("en").spoken_numbers.say_amount(119990) == ""
    assert languages.get("ta").spoken_numbers.say_amount(119990) == ""


def test_every_listed_price_has_its_exact_sinhala_words():
    import re
    import yaml
    from pathlib import Path
    from app import languages
    seed = yaml.safe_load((Path(__file__).resolve().parent.parent / "app" / "seed_data" / "tikirii.yaml").read_text(encoding="utf-8"))
    text = seed["profile"]["pricing_note"] + " ".join(f["answer"] for f in seed["faqs"])
    listed = {int(m.replace(",", "")) for m in re.findall(r"LKR\s*([\d,]+\d)", text)}
    missing = sorted(listed - set(languages.get("si").spoken_numbers.amounts))
    assert not missing, f"add these amounts to spoken_numbers.amounts in languages/si.yaml: {missing}"
