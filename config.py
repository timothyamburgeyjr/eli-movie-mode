"""Configuration loaded from environment variables (.env file)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    plex_url: str = ""
    plex_token: str = ""
    gemini_api_key: str = ""
    kindroid_api_key: str = ""
    kindroid_ai_id: str = ""
    secret_key: str = "change-me-in-production"

    db_path: str = "data/moviemode.db"
    frames_dir: str = "static/frames"
    # Where session journals get written. Defaults to a local path for dev;
    # override via JOURNAL_DIR env var (e.g. "D:\\Vault" on Windows, or a
    # bind-mounted path inside Docker).
    journal_dir: str = "data/journals"

    # Scene analysis uses Pro (deep video understanding, richer prose).
    # Everything else (briefing, trivia, reaction, condense) uses Lite
    # (cheap + fast, Google Search handles the factual lift on the grounded calls).
    gemini_scene_model: str = "gemini-2.5-pro"
    gemini_text_model: str = "gemini-2.5-flash-lite"
    # Legacy alias — some call sites still read `gemini_model`. Keep in sync.
    gemini_model: str = "gemini-2.5-pro"
    kindroid_url: str = "https://api.kindroid.ai/v1/send-message"
    kindroid_char_limit: int = 2000
    gemini_daily_budget: int = 1000

    plex_poll_interval_seconds: int = 5
    default_capture_seconds: int = 30
    # Seedbox reverse-proxies often present a cert that doesn't match the hostname;
    # the seedbox is a trusted origin, so verification is disabled by default.
    plex_verify_ssl: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()


# Gemini pricing (paid tier, USD per 1M tokens). Rates pulled from the
# Google AI pricing page in early 2026; update if Google changes them.
# `thinking` tokens are billed at the same rate as output tokens.
GEMINI_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-pro":        {"input": 1.25, "output": 10.00, "thinking": 10.00},
    "gemini-2.5-flash":      {"input": 0.30, "output":  2.50, "thinking":  2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output":  0.40, "thinking":  0.40},
}

# Google Search grounding — $35 per 1,000 grounded requests after the
# free tier allowance.
GROUNDING_COST_PER_CALL: float = 0.035


def compute_gemini_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    grounded: bool = False,
) -> float:
    """USD cost estimate for a single Gemini call."""
    rates = GEMINI_PRICING.get(model) or {"input": 0.0, "output": 0.0, "thinking": 0.0}
    cost = (
        (input_tokens or 0) * rates["input"] / 1_000_000
        + (output_tokens or 0) * rates["output"] / 1_000_000
        + (thinking_tokens or 0) * rates["thinking"] / 1_000_000
    )
    if grounded:
        cost += GROUNDING_COST_PER_CALL
    return cost
