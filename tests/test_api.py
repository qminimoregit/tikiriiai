"""The admin API: login, the profile, the lists, leads, uploads, the prompt preview and the hooks."""

from tests.fakes import wav_bytes


def test_the_admin_panel_needs_a_login(client):
    assert client.get("/api/profile").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_wrong_password_is_refused_and_right_one_works(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code in (401, 403)
    assert client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200


def test_the_profile_is_seeded_and_can_be_updated(admin):
    p = admin.get("/api/profile").json()
    assert p["company_name"] == "TIKIRII" and p["contact_email"] == "tikiriiai@gmail.com"
    assert p["default_language"] == "si" and p["temperature"] == 1.0 and p["agent_name"] == "Tikirii"
    p["sales_phone"], p["message_channel"] = "0771234567", "both"
    assert admin.put("/api/profile", json=p).status_code == 200
    again = admin.get("/api/profile").json()
    assert again["sales_phone"] == "0771234567" and again["message_channel"] == "both"


def test_profile_validation(admin):
    p = admin.get("/api/profile").json()
    assert admin.put("/api/profile", json={**p, "default_language": "ta", "languages_enabled": ["si"]}).status_code == 422
    assert admin.put("/api/profile", json={**p, "menu_keys": {"si": "1", "ta": "1", "en": "3"}}).status_code == 422
    assert admin.put("/api/profile", json={**p, "menu_keys": {"si": "12", "ta": "2", "en": "3"}}).status_code == 422
    assert admin.put("/api/profile", json={**p, "timezone": "Mars/Base"}).status_code == 422


def test_capabilities_crud_and_order(admin):
    seeded = admin.get("/api/capabilities").json()
    assert len(seeded) == 9 and seeded[0]["title"] == "Incoming calls, answered"
    r = admin.post("/api/capabilities", json={"title": "Call summaries", "description": "A summary after every call."})
    assert r.status_code == 201 and r.json()["sort_order"] == 9
    ids = [c["id"] for c in admin.get("/api/capabilities").json()]
    assert admin.post("/api/capabilities/reorder", json={"ids": ids[::-1]}).status_code == 200
    assert admin.get("/api/capabilities").json()[0]["title"] == "Call summaries"
    assert admin.put(f"/api/capabilities/{ids[-1]}", json={"title": "Summaries"}).json()["title"] == "Summaries"
    assert admin.post("/api/capabilities", json={"title": " "}).status_code == 422
    assert admin.delete(f"/api/capabilities/{ids[-1]}").status_code == 200
    assert admin.post("/api/capabilities/reorder", json={"ids": [999]}).status_code == 400  # a stale list is refused


def test_an_industry_takes_its_typical_calls_one_per_line(admin):
    r = admin.post("/api/industries", json={"name": "Pharmacies", "pitch": "Answers stock questions.",
                                            "tasks": "Medicine availability\n\n  Opening hours  \n", "example": "Do you have Panadol?"})
    assert r.status_code == 201 and r.json()["tasks"] == ["Medicine availability", "Opening hours"]
    assert len(admin.get("/api/industries").json()) == 16
    assert admin.post("/api/industries", json={"name": ""}).status_code == 422


def test_integrations_and_faq_lists(admin):
    assert [i["name"] for i in admin.get("/api/integrations").json()][:2] == ["Custom CRM", "Google Calendar"]
    assert admin.post("/api/integrations", json={"name": "Zapier"}).status_code == 201
    assert admin.get("/api/faqs").json()[0]["question"] == "Which languages does TIKIRII speak?"


def test_a_hidden_item_is_not_told_to_callers(admin):
    item = admin.get("/api/integrations").json()[0]
    body = {k: v for k, v in item.items() if k not in ("id", "sort_order")}
    admin.put(f"/api/integrations/{item['id']}", json={**body, "is_active": False})
    assert "Custom CRM" not in admin.get("/api/profile/prompt-preview?language=en").json()["system_instruction"]


def test_leads_are_listed_updated_and_kept_when_the_call_is_deleted(admin, db):
    from app.models import Call, Lead
    call = Call(channel="pbx", caller_number="0771234567")
    db.add(call)
    db.commit()
    db.add(Lead(call_id=call.id, name="Nimal", business_name="Glow Salon", phone="0771234567", languages=["si"]))
    db.commit()
    items = admin.get("/api/leads").json()["items"]
    assert len(items) == 1 and items[0]["status"] == "new" and items[0]["business_name"] == "Glow Salon"
    lid = items[0]["id"]
    assert admin.get(f"/api/calls/{call.id}").json()["leads"][0]["name"] == "Nimal"
    r = admin.put(f"/api/leads/{lid}", json={"status": "demo_booked", "staff_notes": "Thursday 3pm"})
    assert r.status_code == 200 and r.json()["status"] == "demo_booked"
    assert admin.put(f"/api/leads/{lid}", json={"status": "maybe"}).status_code == 422
    assert admin.get("/api/leads?status=new").json()["total"] == 0
    assert admin.get("/api/dashboard").json()["leads_7d"] == 1
    assert admin.delete(f"/api/calls/{call.id}").status_code == 200
    assert admin.get("/api/leads").json()["items"][0]["call_id"] is None
    assert admin.delete(f"/api/leads/{lid}").status_code == 200 and admin.get("/api/leads").json()["total"] == 0


def test_the_prompt_preview_is_what_a_call_gets(admin):
    r = admin.get("/api/profile/prompt-preview?language=ta").json()
    assert r["language"] == "ta" and "This call is in Tamil" in r["system_instruction"] and r["warning"] == ""
    assert r["tools"] == ["book_demo", "send_details", "check_phone", "end_call"]
    assert "Salons & beauty" in r["system_instruction"]
    assert admin.get("/api/profile/prompt-preview?language=zz").json()["language"] == "si"  # the main language


def test_menu_recording_upload_play_and_delete(admin):
    r = admin.post("/api/profile/menu-audio", files={"file": ("menu.wav", wav_bytes(1.0), "audio/wav")})
    assert r.status_code == 200 and r.json()["menu_audio"].startswith("language-menu-")
    assert admin.get("/api/profile/menu-audio").headers["content-type"] == "audio/wav"
    assert admin.delete("/api/profile/menu-audio").json()["menu_audio"] == ""
    assert admin.get("/api/profile/menu-audio").status_code == 404
    assert admin.post("/api/profile/menu-audio", files={"file": ("x.wav", b"junk", "audio/wav")}).status_code == 400


def test_unavailable_recordings_are_per_language(admin):
    r = admin.post("/api/profile/unavailable-audio/ta", files={"file": ("sorry.wav", wav_bytes(1.0), "audio/wav")})
    assert r.status_code == 200 and set(r.json()["unavailable_audio"]) == {"ta"}
    assert admin.post("/api/profile/unavailable-audio/xx", files={"file": ("s.wav", wav_bytes(), "audio/wav")}).status_code == 404
    assert admin.delete("/api/profile/unavailable-audio/ta").json()["unavailable_audio"] == {}


def test_dashboard_counts_calls_and_demo_requests(admin):
    d = admin.get("/api/dashboard").json()
    assert d["company"] == "TIKIRII" and d["calls_today"] == 0 and d["api_key_set"] is True
    assert d["leads_new"] == 0 and d["new_leads"] == [] and d["lead_alerts"] is False


def test_calls_and_messages_lists_start_empty(admin):
    assert admin.get("/api/calls").json()["total"] == 0 and admin.get("/api/messages").json()["items"] == []
    assert admin.get("/api/calls/1").status_code == 404


def test_the_pbx_hook_checks_ip_and_token(client, monkeypatch):
    from app.config import get_settings
    assert client.get("/api/pbx/call-start?uuid=abc&caller=0771234567&lang=ta&menu=1").status_code in (200, 403)
    monkeypatch.setattr(get_settings(), "pbx_allowed_ips", "10.9.9.9")
    assert client.get("/api/pbx/call-start?uuid=abc").status_code == 403
    monkeypatch.setattr(get_settings(), "pbx_allowed_ips", "*")
    monkeypatch.setattr(get_settings(), "pbx_hook_token", "s3cret")
    assert client.get("/api/pbx/call-start?uuid=abc").status_code == 403
    assert client.get("/api/pbx/call-start?uuid=abc&token=s3cret&lang=ta&menu=1").status_code == 200
    from app.telephony.asterisk import pop_call
    assert pop_call("abc") == ("", "ta", True)


def test_the_whatsapp_webhook_refuses_unsigned_posts(client):
    assert client.post("/api/whatsapp/webhook", content=b"{}").status_code == 403
    assert client.get("/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=x&hub.challenge=1").status_code == 403


def test_me_says_who_is_signed_in(admin, client):
    me = admin.get("/api/auth/me").json()
    assert me["logged_in"] and me["username"] == "admin" and me["timezone"] == "Asia/Colombo" and me["company"] == "TIKIRII"
    client.cookies.clear()
    assert client.get("/api/auth/me").json() == {"logged_in": False}


def test_every_admin_page_is_served_after_login(admin):
    for page in ("index", "profile", "capabilities", "industries", "integrations", "faq", "leads", "calls", "call",
                 "messages", "test-call"):
        r = admin.get(f"/{page}.html")
        assert r.status_code == 200 and "TIKIRII" in r.text, page
    assert admin.get("/public/talk.html").status_code == 404  # no public widget
