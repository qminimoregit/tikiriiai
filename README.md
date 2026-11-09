# tikiriiai: the TIKIRII product line

A phone line answered by **TIKIRII itself**. Business owners call the number to find out what TIKIRII is and what it
can do for their kind of business. The agent explains, books a **free demo** with the team, and can **send a short
summary by SMS or WhatsApp**. The caller is hearing the product working while they ask about it.

The caller presses **1 (Sinhala), 2 (Tamil) or 3 (English)**, and the agent then speaks **only that language** for the
whole call. It answers from what is entered in the admin panel, which is seeded on first start from the marketing site
(`../callagentnew/src/i18n.js` and `content.js`):

- what TIKIRII is, the package prices (it recommends a package from the caller's call volume) and how to get started
- what it does: incoming and outgoing calls, appointments, orders, reminders, follow-ups, three languages, always on
- the 15 industries from the site (restaurants, salons, healthcare, banking, real estate and so on), each with its typical calls
- integrations (Custom CRM, Google Calendar, Google Sheets, Slack, WhatsApp, n8n, REST API and webhooks) and the FAQ

The call engine is `premilaproject`'s, unchanged: the language menu, Gemini Live speech-to-speech, the "no dropping"
behaviour (keepalive, session resumption, reconnects, watchdogs), recordings and transcripts. Only the domain changed.

```
caller ─ phone ─ Asterisk ─ AudioSocket (TCP 9094) ─┐
admin "Test call" ─ browser ─ WebSocket /ws/... ────┴─ FastAPI ─ Gemini Live (speech-to-speech)
                                                      │
                                   admin panel ───────┘  SQLite/MySQL: company, capabilities, industries,
                                                         integrations, FAQ, demo requests, calls, messages
```

## Run it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # set GEMINI_API_KEY, ADMIN_PASSWORD and SESSION_SECRET
.venv/bin/uvicorn app.main:app --port 8000 --workers 1
```

Open http://localhost:8000, sign in, and use **Test call** (Chrome on localhost or https). Tests: `.venv/bin/python -m pytest`
(no network or key needed). Run **one worker** only: login lockouts are in memory.

## Admin panel

| Page | What it controls |
|---|---|
| Dashboard | New demo requests, calls, setup problems |
| Demo requests | Every `book_demo` from a call: name, business, kind of business, phone, when to call back, languages, what they want. Set the status (new, contacted, demo booked, closed) and add team notes |
| Company & agent | What TIKIRII is, pricing, getting started, website and email (and how the agent says them), sales phone, agent name and voice, languages, greetings, **the language-menu recording**, "unavailable" recordings, how summaries are sent, extra info, instructions, and a **preview of exactly what the agent knows** |
| What it does / Industries / Integrations / FAQ | The lists the agent explains from. Hide an item to stop the agent mentioning it |
| Calls, Messages sent | Every call with recording, transcript and the demo request it produced; every message sent |

Changes apply from the **next call**: each call builds the agent's instructions fresh from the database.

## How a call works

1. The call connects. The **language menu recording** plays (upload it on *Company & agent*; say in it that the call is
   recorded). The caller's key picks the language. No key after two plays: the main language (Sinhala by default).
2. The agent greets in that language, says it is an AI voice agent, and asks what kind of business the caller has.
3. It explains what TIKIRII would do for that business from the matching industry, one short answer at a time. It
   never invents a feature, a customer or a price (it gives only the prices written in the profile).
4. When the caller is interested it offers a free demo, asks their name, business and a good time to call, confirms the
   number (caller ID, or one they say, checked and read back), and calls `book_demo`. The request appears on *Demo
   requests* and, with `LEAD_NOTIFY_PHONE` set, is texted to the sales team. The agent never promises a demo time.
5. Tools: `book_demo`, `send_details`, `check_phone`, `end_call`.

## Phone calls with Asterisk

1. `PBX_ENABLED=true` in `.env`, restart. AudioSocket listens on `127.0.0.1:9094`.
2. Load `res_audiosocket` / `app_audiosocket` (Asterisk 18+), set `ASTERISK_SOUNDS_DIR` to a folder Asterisk can read, and include
   `asterisk/extensions_tikirii.conf`. Route the TIKIRII number to `tikirii,s,1`. Set the same `PBX_HOOK_TOKEN`.
3. For "press # to go back to the language menu", create an AMI user (read=dtmf, write=call) and set `AMI_USER` and `AMI_SECRET`.
4. Test without Asterisk: `.venv/bin/python scripts/audiosocket_test_client.py tests/fixtures/sample_8k.wav out.wav --caller 0771234567`.

## Messages: summaries to callers, alerts to the team

- **Summary to the caller** (`send_details`): set `message_channel` on *Company & agent* (SMS, WhatsApp then SMS, both,
  WhatsApp, or off). The text is built by the app from the admin data (the matching industry's calls, the six call jobs,
  the contact details), never written by the model. SMS: Notify.lk or Getmore (`SMS_PROVIDER` and keys in `.env`).
  WhatsApp (Meta Cloud API) needs an approved template: run `scripts/whatsapp_templates.py` for the text to submit (one
  per language), then set the `WHATSAPP_*` variables and point Meta's webhook at `/api/whatsapp/webhook`.
- **Demo request alert** (`LEAD_NOTIFY_PHONE`): an SMS in English to the sales team for each demo request, through the
  same SMS provider. It is saved on *Messages sent* too.

Only Sri Lankan mobile numbers are messaged. Without a provider nothing is sent: messages are saved as "not sent" and the
agent gives the contact details by voice.

## Checking the agent's answers

`scripts/qa_check.py` puts the 40 prospect questions in `tests/data/tikirii_qa.yaml` (English, Sinhala, Tamil) through the
same instructions and has a judge model compare each answer with the expected one (`GEMINI_API_KEY` needed, costs a few
cents). It also flags any amount that is not in the profile's price list. Read the report; it is not a pass/fail gate.

## Deploying

nginx in front with WebSocket upgrade headers and `proxy_read_timeout 3600s`; block `/api/pbx/` from outside; run uvicorn with
`--proxy-headers --forwarded-allow-ips=127.0.0.1 --workers 1`. Keep `.env`, `recordings/` and `media/` in backups; recordings
are kept until you delete the call. Schema changes: `.venv/bin/alembic revision --autogenerate -m "what changed"` (applied on start).

## Before going live

- Enter the **website** (and how the agent should say it) and a **sales phone** if callers may be given one. Without a
  phone number the agent offers a demo to anyone who wants a person.
- Read the seeded copy on every page: it comes from the marketing site and may promise more than you deliver today
  (for example the integrations). Hide what isn't ready.
- Set `LEAD_NOTIFY_PHONE` and an SMS provider so demo requests reach the team at once.
- Record the language menu and the "unavailable" clips. Have native speakers review `languages/si.yaml` and `ta.yaml`
  (greeting, demo lines, summary text): the agent says those lines word for word. Test on real phone audio in Sinhala and Tamil.
- Set a strong `ADMIN_PASSWORD` and `SESSION_SECRET`; keep `.env` out of git.
