from functools import lru_cache

from google import genai

from ..config import get_settings


@lru_cache
def get_client() -> genai.Client:
    """The Gemini client. The API key stays on the server; browsers only talk to our WebSocket."""
    key = get_settings().gemini_api_key
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
    return genai.Client(api_key=key)
