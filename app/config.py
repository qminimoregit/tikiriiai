from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    # Live speech-to-speech model for the agent. gemini-3.8-live is the stable default.
    gemini_live_model: str = "gemini-3.8-live"
    # Text-to-speech model for the placeholder menu prompts (until real recordings are uploaded).
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    # After a call, this model listens to the recording and rewrites the caller's lines, which Gemini Live's own
    # live transcription often gets wrong in Sinhala and Tamil. Empty = keep the live transcript as it is.
    gemini_transcribe_model: str = "gemini-3.8-flash"
    # Voice used until one is picked on the Profile page (ai/voices.py lists them).
    default_voice: str = "Aoede"
    # How bracketed notes ("[the caller connected, greet them]") reach the model. Some Live models only accept
    # client_content as initial history; switch to realtime_text if mid-call notes are rejected.
    live_note_mode: str = "client_content"  # client_content | realtime_text
    # The live model drifts to another voice or accent more often at high temperature, so a call never uses more than
    # this, whatever the Profile page says. 2.0 = no cap.
    live_max_temperature: float = 1.0
    # Each agent reply is held back this long (seconds) while its voice is checked, so a reply in the wrong voice is
    # never played. 0 = play replies straight away.
    voice_hold_seconds: float = 1.5

    db_url: str = "sqlite:///./tikirii.db"
    timezone: str = "Asia/Colombo"  # only used to seed the profile on first start

    # Admin panel login. ADMIN_PASSWORD is required (8+ characters): the app refuses to start without it.
    admin_username: str = "admin"
    admin_password: str = ""
    session_secret: str = ""  # random string used to sign login cookies; a random one is made if empty
    session_days: int = 7

    # Language-menu recording (uploaded on the Profile page) and other uploaded audio live in media_dir.
    media_dir: str = "media"
    ffmpeg_bin: str = ""  # empty = find ffmpeg on PATH; without ffmpeg only WAV uploads are accepted
    # Folder Asterisk reads sounds from. The app keeps lang-menu.wav (8 kHz) here so the dialplan can play the menu
    # with Read() when the PBX does not pass key presses to AudioSocket. Empty = not used.
    asterisk_sounds_dir: str = ""

    # Call recordings: the whole call (menus, caller and agent) is saved as <recordings_dir>/<call id>.mp3 so staff can
    # play it on the call page. MP3 needs ffmpeg; without it the file stays a (much larger) WAV.
    record_calls: bool = True
    recordings_dir: str = "recordings"

    max_call_seconds: int = 900
    # If the caller stays silent this long after the agent stops talking, the agent asks "are you still
    # there?". After that many unanswered prompts the call is ended.
    silence_prompt_seconds: int = 10
    silence_max_prompts: int = 2

    # Gemini's own speech detection. "low" start sensitivity and a longer prefix make short background sounds
    # less likely to count as the caller talking (and cut the agent off). Empty / 0 keeps Gemini's default.
    vad_start_sensitivity: str = "low"  # low | high | ""
    vad_end_sensitivity: str = ""  # low | high | ""
    vad_prefix_padding_ms: int = 300
    vad_silence_ms: int = 1000
    vad_turn_coverage_all: bool = True  # TURN_INCLUDES_ALL_INPUT
    # Noise gate on caller audio: silences background sound (traffic, TV, other people) that is quieter than the
    # caller's own voice, before it reaches Gemini. on | log (measure only) | off. Default log: Gemini Live copes
    # with noise itself, and cleaning the audio first usually makes Sinhala recognition worse.
    voice_gate: str = "log"
    # RNNoise noise cancellation on caller audio: removes noise mixed into the caller's speech (traffic, fans,
    # hum, keyboard) before the gate. on | off (off by default, see voice_gate)
    noise_suppression: str = "off"

    # Asterisk PBX (AudioSocket). The dialplan connects here for the TIKIRII line.
    pbx_enabled: bool = False
    pbx_audiosocket_host: str = "127.0.0.1"
    pbx_audiosocket_port: int = 9094
    pbx_allowed_ips: str = "127.0.0.1"  # comma-separated; AudioSocket and the caller-ID hook accept only these
    pbx_hook_token: str = ""  # if set, /api/pbx/call-start must carry ?token=<this>
    # Asterisk manager (AMI): lets a caller press # to go back to the language menu. Empty user/secret = off.
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_user: str = ""
    ami_secret: str = ""

    # SMS to callers with the details they asked for. Empty provider = nothing is sent; messages are saved as "not sent".
    sms_provider: str = ""  # "" | notifylk | getmore
    sms_user_id: str = ""
    sms_api_key: str = ""
    sms_sender_id: str = "NotifyDEMO"  # NotifyDEMO is Notify.lk's test sender; use your approved sender ID
    sms_timeout_seconds: float = 10

    # WhatsApp (Meta Cloud API) to callers. Empty token or phone number id = WhatsApp is off.
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""  # only the template script uses it
    whatsapp_app_secret: str = ""  # checks the signature on webhook calls from Meta
    whatsapp_verify_token: str = ""  # Meta's webhook handshake
    whatsapp_api_version: str = "v23.0"
    whatsapp_timeout_seconds: float = 10

    # New demo requests (book_demo) are texted to this number (the sales team). Empty = they only show on the Leads page.
    lead_notify_phone: str = ""

    @property
    def pbx_allowed_ip_list(self) -> list[str]:
        return [ip.strip() for ip in self.pbx_allowed_ips.split(",") if ip.strip()]

    @property
    def media_path(self) -> Path:
        p = Path(self.media_dir)
        return p if p.is_absolute() else ROOT / p

    @property
    def recordings_path(self) -> Path:
        p = Path(self.recordings_dir)
        return p if p.is_absolute() else ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()


WEAK_PASSWORDS = {"admin", "password", "12345678", "changeme", "admin123"}


def check_settings(s: Settings) -> None:
    """Refuse to start with an unusable admin login: the panel controls what callers are told."""
    if not s.admin_password:
        raise RuntimeError("ADMIN_PASSWORD is not set. Copy .env.example to .env and choose a password.")
    if len(s.admin_password) < 8 or s.admin_password.lower() in WEAK_PASSWORDS:
        raise RuntimeError("ADMIN_PASSWORD is too weak. Use at least 8 characters, not a common password.")
