"""How numbers are shown to the model (how they're said is each language file's rule)."""

from app.phones import display_phone


def test_sri_lankan_numbers_are_grouped_like_they_are_written():
    assert display_phone("0771234567") == "077 123 4567"
    assert display_phone("+94771234567") == "077 123 4567"
    assert display_phone("94771234567") == "077 123 4567"
    assert display_phone("0094771234567") == "077 123 4567"
    assert display_phone("771234567") == "077 123 4567"
    assert display_phone("0112345678") == "011 234 5678"


def test_other_numbers_are_left_as_they_were():
    assert display_phone("1234") == "1234"  # an extension
    assert display_phone("+1 415 555 0100") == "+1 415 555 0100"
    assert display_phone("") == ""


# ---- which numbers staff can call back, and how numbers are said

import pytest  # noqa: E402

from app import languages  # noqa: E402
from app.phones import say_phone, sri_lankan  # noqa: E402


@pytest.mark.parametrize("raw, want", [
    ("0771234567", "0771234567"), ("+94 77 123 4567", "0771234567"), ("0094771234567", "0771234567"),
    ("94771234567", "0771234567"), ("771234567", "0771234567"), ("011-234-5678", "0112345678"),
])
def test_a_sri_lankan_number_is_its_ten_digits(raw, want):
    assert sri_lankan(raw) == want


@pytest.mark.parametrize("raw", ["071320420", "0450350", "076", "1234", "1919", "ucmtrunk2", "", "+1 415 555 0100",
                                 "0012345678"])
def test_anything_else_is_not_a_number_staff_can_call_back(raw):
    assert sri_lankan(raw) == ""


@pytest.mark.parametrize("raw, said", [
    # the caller IDs the agent read back wrong on calls 30, 34, 39 and 53
    ("0760772704", "බිංදුවයි හැත්තෑ හයයි, බිංදුවයි හැත්තෑ හතයි, විසි හතයි, බිංදුවයි හතර"),
    ("0770166152", "බිංදුවයි හැත්තෑ හතයි, බිංදුවයි දාසයයි, හැට එකයි, පනස් දෙක"),
    ("0721510764", "බිංදුවයි හැත්තෑ දෙකයි, එකසිය පනස් එකයි, බිංදුවයි හතයි, හැට හතර"),
    ("+94771234567", "බිංදුවයි හැත්තෑ හතයි, එකසිය විසි තුනයි, හතළිස් පහයි, හැට හත"),  # si.yaml's own example
    ("0112005006", "බිංදුවයි එකොළහයි, දෙසියයයි, පනහයි, බිංදුවයි හය"),
])
def test_sinhala_phone_numbers_are_written_out_in_words(raw, said):
    assert say_phone(raw, languages.get("si")) == said


def test_english_and_tamil_keep_their_digits():
    for code in ("en", "ta"):
        lang = languages.get(code)
        assert say_phone("0771234567", lang) == "077 123 4567"
    assert say_phone("1234", languages.get("si")) == "1234"  # not a Sri Lankan number: as it was
