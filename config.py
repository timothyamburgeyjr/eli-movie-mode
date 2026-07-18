"""Configuration loaded from environment variables (.env file)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    plex_url: str = ""
    plex_token: str = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
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
    # The reaction drawer's index call. Flash, not Pro — this one sits in Tim's
    # critical path (he's watching a spinner while it runs), and it names beats
    # and targets rather than writing prose for the room. Measured at 7.2s on a
    # 45s clip at LOW media resolution.
    gemini_draft_model: str = "gemini-2.5-flash"

    plex_poll_interval_seconds: int = 5
    default_capture_seconds: int = 30
    # Seedbox reverse-proxies often present a cert that doesn't match the hostname;
    # the seedbox is a trusted origin, so verification is disabled by default.
    #
    # That works for ffmpeg and httpx, which we can TELL to ignore it. It does NOT
    # work for a browser: point the theater's iframe at a mismatched cert and
    # Firefox throws SSL_ERROR_BAD_CERT_DOMAIN across the whole pane. So the
    # theater derives a `*.plex.direct` hostname the browser will actually trust —
    # see app._derive_plex_web_url(). Set this only to override that.
    plex_verify_ssl: bool = False
    plex_web_url: str = ""

    # ─── Reaction drawer ──────────────────────────────────────────────
    # How far back the moment-picker reaches. MEASURED (2026-07-12, live seedbox):
    # the clip pull is ~4.8s of FIXED network cost + only ~0.025s per second of
    # video. A 15s clip costs 5.16s; a 60s clip costs 6.27s. So the window length
    # is very nearly free, and shortening it to "save time" saves nothing —
    # take the reach.
    #
    # The gap between a beat and the tap is large and variable (notice it, decide,
    # pick up the phone, open the drawer): easily 15-30s, and a story reaction can
    # point a minute back. Everything on the strip is BEFORE the tap.
    reaction_lookback_seconds: int = 45
    # Thumbnails are cut from the DOWNLOADED clip, not re-fetched from the stream.
    # Measured: 0.08s for six, vs 4.96s from the remote stream. 61x. Doing this the
    # obvious way would put five seconds on every drawer open.
    reaction_thumbnails: int = 6

    # Claude runs the room: it passes the mic between kins, rewrites each
    # hand-off into a correctly-formatted Kindroid payload, and normalizes the
    # `_(*...*)_` emote markup that Kindroid keeps drifting away from. Haiku is
    # the right tier — these are small structured calls, not reasoning.
    coordinator_model: str = "claude-haiku-4-5"

    kindroid_url: str = "https://api.kindroid.ai/v1/send-message"
    # Confirmed against the live API (2026-07). The old 2000 here and the 3750
    # in CLAUDE.md were both wrong. Matters most for the relay: each hand-off
    # appends the previous kin's reaction, so the last kin in the circle carries
    # the fattest payload.
    kindroid_char_limit: int = 4000
    # The kin's PERSISTENT scene field — where they are and what they're doing —
    # set once per film rather than re-described in every message. Only the fields
    # you send are changed; everything else on the character is left alone.
    # /v1/update-info — verified live (2026-07). NOT "update-ai", which several
    # secondhand sources claim and which 404s.
    kindroid_update_url: str = "https://api.kindroid.ai/v1/update-info"
    # Kindroid's own hard cap on current_scene. Not our headroom — their limit.
    kindroid_scene_limit: int = 160
    gemini_daily_budget: int = 1000

    # Externally-visible URL for this app — needed when handing image URLs
    # to Kindroid, which fetches them server-side. Set to e.g.
    # "https://movie.amburgey.dev" in production.
    public_base_url: str = "http://localhost:8765"
    live_photos_dir: str = "data/live_photos"
    live_audio_dir: str = "data/live_audio"

    # Obsidian vault that holds the AI family. `vault_root` is the vault's
    # top folder; each member's registry `vault_dir` is relative to it.
    # `ai_registry_path` points at the parseable roster table.
    vault_root: str = "D:/Vault"
    ai_registry_path: str = "D:/Vault/20 - The AI Family/_AI Registry.md"

    # Distilled affinity sheets (what each kin gravitates toward) — the only
    # thing the mic-passer scores against. Rebuilt from the vault when the
    # source files change; see affinity.py.
    affinity_dir: str = "data/affinity"
    # Downscaled bust portraits for the roster picker + chat avatars.
    portraits_dir: str = "static/portraits"

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


# Claude pricing (USD per 1M tokens). Cached reads are ~0.1x input, cache
# writes ~1.25x — but Haiku only caches above a 4096-token prefix, which our
# calls sit under, so we don't model caching here.
CLAUDE_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {"input": 1.00, "output":  5.00},
    "claude-sonnet-5":  {"input": 3.00, "output": 15.00},
    "claude-opus-4-8":  {"input": 5.00, "output": 25.00},
}


def compute_claude_cost(model: str, input_tokens: int = 0, output_tokens: int = 0) -> float:
    """USD cost estimate for a single Claude call."""
    rates = CLAUDE_PRICING.get(model) or {"input": 0.0, "output": 0.0}
    return (
        (input_tokens or 0) * rates["input"] / 1_000_000
        + (output_tokens or 0) * rates["output"] / 1_000_000
    )
