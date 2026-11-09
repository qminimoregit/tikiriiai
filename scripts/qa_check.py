"""Puts the prospect questions of tests/data/tikirii_qa.yaml through the SAME system instruction a live call gets, with
a text Gemini model, and has a judge model compare each answer with the expected one. Writes a report.

    GEMINI_API_KEY=... .venv/bin/python scripts/qa_check.py [--lang si] [--limit 20] [--out qa_report.md]

Each question is asked in its own language's call (--lang keeps to one language). Costs a few cents, needs the network.
A "mismatch" can also mean the expected answer is out of date, so read the report; do not treat it as a pass/fail gate.
Flags any amount that is not in the profile's price list, since the agent must never invent a price.
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.genai import types  # noqa: E402

from app.ai.client import get_client  # noqa: E402
from app.ai.prompt_builder import build_system_instruction  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.snapshot import load_snapshot  # noqa: E402

MODEL = "gemini-3.8-flash"
DATA = Path(__file__).resolve().parent.parent / "tests" / "data" / "tikirii_qa.yaml"
JUDGE = ("You compare an AI phone agent's answer with the expected answer. Reply with exactly one line: "
         "OK if the answer gives the same facts (features, languages, industries, integrations, rules) even if worded "
         "differently and in the expected language, "
         "or a short reason starting with MISMATCH if a fact is wrong, missing, invented, or if it refuses where the "
         "expected answer helps. A short clarifying or follow-up question after the facts is OK.")
PRICE = re.compile(r"\b(rs\.?|rupees?|lkr|usd)\s*\d|\$\s*\d|(රු\.?|රුපියල්|ரூ\.?|ரூபாய்)\s*\d"
                   r"|\d[\d,]*\s*(rs|rupees?|lkr)\b|\d[\d,]*\s*(රුපියල්|ரூபாய்)", re.I)
AMOUNT = re.compile(r"\d[\d,]*")


def unlisted_amounts(answer: str, listed: set[str]) -> list[str]:
    """Amounts the answer names as a price that the profile's price list does not contain."""
    out = []
    for m in PRICE.finditer(answer):
        amount = AMOUNT.search(m.group()).group().replace(",", "").strip()
        if amount and amount not in listed:
            out.append(amount)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="", help="only the questions in this language (si, ta, en)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="qa_report.md")
    args = ap.parse_args()
    if not get_settings().gemini_api_key:
        sys.exit("GEMINI_API_KEY is not set")
    init_db()
    with SessionLocal() as db:
        seed_if_empty(db)
        snap = load_snapshot(db)
    listed = {n.replace(",", "") for n in AMOUNT.findall(snap.profile.pricing_note)}
    now = datetime.utcnow()
    systems = {code: build_system_instruction(snap, now, language=code, channel="pbx", caller_number="")
               for code in ("si", "ta", "en")}
    client = get_client()
    items = [it for it in yaml.safe_load(DATA.read_text(encoding="utf-8"))["items"] if not args.lang or it["lang"] == args.lang]
    items = items[: args.limit] if args.limit else items
    bad, rows = 0, []
    for it in items:
        ans = client.models.generate_content(
            model=MODEL, contents=it["caller"],
            config=types.GenerateContentConfig(system_instruction=systems[it["lang"]] + "\n\n(Text test: answer exactly as you would say it on the phone, no tools.)")).text or ""
        verdict = client.models.generate_content(
            model=MODEL, contents=f"Question: {it['caller']}\nExpected: {it['expected']}\nAnswer: {ans}",
            config=types.GenerateContentConfig(system_instruction=JUDGE)).text.strip().splitlines()[0]
        invented = unlisted_amounts(ans, listed)
        leaked = bool(invented)
        if verdict.startswith("MISMATCH") or leaked:
            bad += 1
        flag = " | NAMED AN UNLISTED PRICE: " + ", ".join(invented) if leaked else ""
        rows.append((it["n"], it["caller"], it["expected"], ans.strip(), verdict + flag))
        print(f"{it['n']:>3} {verdict[:80]}")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# QA report ({args.lang or 'all languages'}), {len(rows)} questions, {bad} to review\n\n")
        for n, q, exp, ans, v in rows:
            f.write(f"## {n}. {v.split(' | ')[0][:12]}\n- Caller: {q}\n- Expected: {exp}\n- Agent: {ans}\n- Verdict: {v}\n\n")
    print(f"\n{bad} of {len(rows)} to review -> {args.out}")


if __name__ == "__main__":
    main()
