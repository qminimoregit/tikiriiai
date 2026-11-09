from app import details, languages, sms
from app.models import AgentProfile, Capability
from app.snapshot import load_snapshot


def build(db, language="en", topics=("all",), **kw):
    return details.build(load_snapshot(db), language, topics, **kw)


def test_the_full_message_has_the_intro_capabilities_contact_and_footer(db):
    d = build(db, "en")
    lines = d.text.splitlines()
    assert lines[0] == languages.get("en").sms.intro.format(company="TIKIRII")
    assert "What TIKIRII handles" in d.text and "- Appointments, booked" in d.text
    assert "Email: tikiriiai@gmail.com" in d.text and d.text.rstrip().endswith(languages.get("en").sms.footer)
    assert "- Three languages" not in d.text  # an SMS keeps to the six call jobs
    assert not d.truncated and len(details.split_text(d.text)) == 1


def test_the_callers_business_is_matched_and_its_calls_listed(db):
    d = build(db, "en", ["industry"], industry="saloon")  # misspelt, as callers say it
    assert "For Salons & beauty" not in d.text  # "saloon" is not a word of the name, so no guess
    d = build(db, "en", ["industry"], industry="salon")
    assert "For Salons & beauty" in d.text and "- Hair and beauty appointments" in d.text
    assert "What TIKIRII handles" not in d.text  # only what was asked for, plus the contact


def test_the_industry_name_is_in_the_callers_language(db):
    assert "සැලෝන් සහ රූපලාවණ්‍ය සඳහා" in build(db, "si", ["industry"], industry="Salons & beauty").text
    assert "உணவகங்கள் இற்கு" in build(db, "ta", ["industry"], industry="restaurant").text


def test_an_unknown_business_falls_back_to_the_capabilities(db):
    d = build(db, "en", ["industry"], industry="space travel")
    assert "What TIKIRII handles" in d.text


def test_contact_lines_follow_what_is_entered(db):
    p = db.get(AgentProfile, 1)
    p.website, p.sales_phone = "https://tikirii.lk", "0771234567"
    db.commit()
    d = build(db, "si", ["contact"])
    assert "වෙබ් අඩවිය: https://tikirii.lk" in d.text and "දුරකථන: 077 123 4567" in d.text
    p.website = p.sales_phone = p.contact_email = ""
    db.commit()
    assert build(db, "en", ["contact"]).empty


def test_a_message_that_needs_too_many_sms_is_cut_and_says_so(db):
    db.query(Capability).update({"title": "x" * 600})
    db.commit()
    d = build(db, "en")
    parts = details.split_text(d.text)
    assert d.truncated and len(parts) <= details.MAX_PARTS and all(len(p) <= sms.MAX_SMS_CHARS for p in parts)


def test_the_whatsapp_parameters_have_no_line_breaks_in_the_text_we_pass(db):
    d = build(db, "en")
    assert d.params["company"] == "TIKIRII" and "\n" not in d.params["details"]
    assert d.params["contact"] == "Email: tikiriiai@gmail.com"
