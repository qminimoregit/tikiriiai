"""The Gemini prebuilt voices the admin panel offers."""

from ..config import get_settings

# name -> (gender, style). Google only publishes the style; the gender is how each voice sounds.
VOICES = {
    "Achernar": ("female", "Soft"),
    "Aoede": ("female", "Breezy"),
    "Autonoe": ("female", "Bright"),
    "Callirrhoe": ("female", "Easy-going"),
    "Despina": ("female", "Smooth"),
    "Erinome": ("female", "Clear"),
    "Gacrux": ("female", "Mature"),
    "Kore": ("female", "Firm"),
    "Laomedeia": ("female", "Upbeat"),
    "Leda": ("female", "Youthful"),
    "Pulcherrima": ("female", "Forward"),
    "Sulafat": ("female", "Warm"),
    "Vindemiatrix": ("female", "Gentle"),
    "Zephyr": ("female", "Bright"),
    "Achird": ("male", "Friendly"),
    "Algenib": ("male", "Gravelly"),
    "Algieba": ("male", "Smooth"),
    "Alnilam": ("male", "Firm"),
    "Charon": ("male", "Informative"),
    "Enceladus": ("male", "Breathy"),
    "Fenrir": ("male", "Excitable"),
    "Iapetus": ("male", "Clear"),
    "Orus": ("male", "Firm"),
    "Puck": ("male", "Upbeat"),
    "Rasalgethi": ("male", "Informative"),
    "Sadachbia": ("male", "Lively"),
    "Sadaltager": ("male", "Knowledgeable"),
    "Schedar": ("male", "Even"),
    "Umbriel": ("male", "Easy-going"),
    "Zubenelgenubi": ("male", "Casual"),
}


def call_voice(voice: str) -> str:
    """The company's picked voice, or the .env default when none (or an unknown one) is picked."""
    if voice in VOICES:
        return voice
    default = get_settings().default_voice
    return default if default in VOICES else "Aoede"


def voice_gender(voice: str) -> str:
    return VOICES.get(call_voice(voice), ("female", ""))[0]


FEMALE_MIN_HZ = 140.0  # a female voice measured 150-230 Hz on the phone; a male one that Gemini slipped into, 114 Hz
MALE_MAX_HZ = 200.0
MIN_VOICED_SECONDS = 1.5


def pitch_matches(voice: str, hz: float, voiced_seconds: float, min_voiced: float = MIN_VOICED_SECONDS) -> bool:
    """False only when there is enough speech to judge and its pitch belongs to the other gender."""
    if voiced_seconds < min_voiced:
        return True
    return hz >= FEMALE_MIN_HZ if voice_gender(voice) == "female" else hz <= MALE_MAX_HZ
