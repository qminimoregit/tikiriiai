import shutil
from pathlib import Path

import pytest

from app import languages
from app.languages import LANG_DIR, LanguageFileError, load_languages


def test_all_three_languages_load_with_the_same_keys():
    langs = load_languages()
    assert list(langs) == ["si", "ta", "en"]
    for lang in langs.values():
        assert len(lang.sms.weekdays) == 7 and lang.whatsapp_language
        assert set(lang.language.names) == {"si", "ta", "en"}


def test_the_agent_says_a_phone_number_in_words_in_sinhala():
    si = languages.get("si")
    assert si.spoken_numbers.say("0771234567", si.spoken_numbers.phone_groups).startswith("බිංදුවයි හැත්තෑ හතයි")


@pytest.fixture
def copy(tmp_path):
    for f in LANG_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    return tmp_path


def test_a_missing_key_names_the_file_and_the_key(copy):
    p = copy / "ta.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("other_language:", "other_langage:"), encoding="utf-8")
    with pytest.raises(LanguageFileError, match=r"ta\.yaml.*other_language"):
        load_languages(copy)


def test_an_unknown_placeholder_is_refused(copy):
    p = copy / "en.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("We, the Tikirii AI Agent,", "We, the {nope} AI Agent,"),
                 encoding="utf-8")
    with pytest.raises(LanguageFileError, match="nope"):
        load_languages(copy)


def test_the_goodbye_regex_knows_every_language():
    rx = languages.goodbye_regex()
    assert rx.search("Thank you. Have a good day!") and rx.search("සුබ දවසක්!") and rx.search("இனிய நாள்!")
    assert not rx.search("ආයුබෝවන්")  # the greeting is never a goodbye


def test_sinhala_weekdays_are_spelled_correctly():
    assert languages.get("si").sms.weekdays == ["සඳුදා", "අඟහරුවාදා", "බදාදා", "බ්‍රහස්පතින්දා", "සිකුරාදා", "සෙනසුරාදා", "ඉරිදා"]
