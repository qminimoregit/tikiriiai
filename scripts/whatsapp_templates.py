"""Prints the WhatsApp message template to submit to Meta (Business Manager -> WhatsApp -> Message templates), once per
language. A business can only start a WhatsApp chat with an approved template, so the details message is a template
with three named parameters: {{company}}, {{details}} and {{contact}}.

    .venv/bin/python scripts/whatsapp_templates.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import languages, whatsapp  # noqa: E402BODY = {
    "si": "{{company}}: සිංහල, ඉංග්‍රීසි සහ දෙමළ භාෂාවලින් ඇමතුම්වලට පිළිතුරු දෙන AI voice agent. {{details}} අප අමතන්න: {{contact}} නොමිලේ demo එකක් book කරන්න ඕනෑම වෙලාවක අපට කතා කරන්න.",
    "ta": "{{company}}: சிங்களம், ஆங்கிலம், தமிழ் மொழிகளில் அழைப்புகளுக்குப் பதிலளிக்கும் AI voice agent. {{details}} எங்களைத் தொடர்புகொள்ள: {{contact}} இலவச demo ஒன்றை book செய்ய எப்போது வேண்டுமானாலும் எங்களை அழையுங்கள்.",
    "en": "{{company}}: the AI voice agent that answers calls in Sinhala, English and Tamil. {{details}} Contact us: {{contact}} Call us back any time to book a free demo.",
}
EXAMPLE = {"company": "TIKIRII", "details": "What TIKIRII handles; - Incoming calls, answered; - Appointments, booked",
           "contact": "Email: tikiriiai@gmail.com"}



for code, lang in languages.languages().items():
    print(f"--- {lang.name} ---")
    print(f"Name:      {whatsapp.template_name()}")
    print(f"Language:  {lang.whatsapp_language}")
    print("Category:  Utility")
    print(f"Body:      {BODY[code]}")
    print(f"Examples:  {EXAMPLE}")
    print()
print("Parameters are NAMED ({{company}}), not numbered. The app removes line breaks from them before sending.")
