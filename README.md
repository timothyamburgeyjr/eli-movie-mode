# Project Eli: Movie Mode

FastAPI web app that lets you watch Plex movies with your AI companion Eli (via the Kindroid API). Captures video clips from Plex → analyzes them with Gemini 2.5 → relays rich context to Kindroid → renders Eli's responses with proper emote formatting.

See [CLAUDE.md](CLAUDE.md) for the full locked spec.

---

## Quick start (local dev)

```bash
cp .env.example .env
# Fill in: PLEX_TOKEN, GEMINI_API_KEY, KINDROID_API_KEY, KINDROID_AI_ID, SECRET_KEY

python -m venv .venv
source .venv/bin/activate   # or: .venv\Scripts\activate on Windows
pip install -r requirements.txt

python run.py
```

Open `http://localhost:8000`. First visit prompts for a dashboard password.

**Prerequisite:** `ffmpeg` on `$PATH`. On Windows: `winget install Gyan.FFmpeg`. On macOS: `brew install ffmpeg`. On Linux: `apt install ffmpeg`.

---

## Docker deployment

```bash
cp .env.example .env
# Fill in credentials

docker-compose up -d --build
```

The container runs on `localhost:8765` (host) → `8000` (container). Data and journals persist via bind-mount to `./data`.

```bash
# Tail logs
docker-compose logs -f movie-mode

# Stop
docker-compose down

# Rebuild after code changes
docker-compose up -d --build
```

---

## Phone access — two options

### Option A: Local WiFi (instant)
Find your computer's LAN IP (`ipconfig` / `ifconfig`), then hit `http://<lan-ip>:8000` (or `:8765` with Docker) from your phone's browser. Works as long as both devices are on the same network.

### Option B: Cloudflare Tunnel (anywhere)
Expose the container at `movie.amburgey.dev` (or your own hostname) without opening router ports.

1. **Install cloudflared** on the host machine and authenticate:
   ```bash
   cloudflared tunnel login
   ```

2. **Create a tunnel** (once):
   ```bash
   cloudflared tunnel create eli-movie-mode
   cloudflared tunnel route dns eli-movie-mode movie.amburgey.dev
   ```

3. **Config file** at `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: eli-movie-mode
   credentials-file: /home/YOU/.cloudflared/<tunnel-uuid>.json

   ingress:
     - hostname: movie.amburgey.dev
       service: http://localhost:8765
     - service: http_status:404
   ```

4. **Run as a service:**
   ```bash
   sudo cloudflared service install
   ```

   Or one-off for testing: `cloudflared tunnel run eli-movie-mode`.

5. Visit `https://movie.amburgey.dev` from any device, anywhere. HTTPS terminates at Cloudflare, so your password-based auth is safe even without a cert on the local side.

**WebSocket note:** Cloudflare Tunnel handles WebSocket upgrades automatically — no extra config needed for the `/ws` endpoint.

---

## Architecture summary

| Module | Responsibility |
|---|---|
| [app.py](app.py) | FastAPI + routes + WebSocket + pipeline orchestration |
| [config.py](config.py) | Pydantic settings + Gemini pricing + cost math |
| [database.py](database.py) | Async SQLite (aiosqlite), schema, migrations, queries |
| [plex_monitor.py](plex_monitor.py) | Sessions API poller, state machine, event emission |
| [smart_snap.py](smart_snap.py) | ffmpeg clip extraction (downscaled 480p for speed) |
| [gemini_brain.py](gemini_brain.py) | Scene / briefing / trivia / reaction / sign-off / catchup |
| [kindroid_relay.py](kindroid_relay.py) | Emote payload assembly + POST + reply parsing |
| [stoned_tracker.py](stoned_tracker.py) | Onset/peak/taper curves + narration variants |
| [context_manager.py](context_manager.py) | Session history for Gemini (Kindroid sees only narrative) |
| [session_manager.py](session_manager.py) | Stats, sign-off orchestration, journal entries |

---

## Models + cost

| Call | Model | Typical |
|---|---|---|
| Scene analysis (video) | `gemini-2.5-pro` | ~$0.03/call |
| Briefing (grounded) | `gemini-2.5-flash-lite` | ~$0.04/call (grounding fee) |
| Trivia (grounded) | `gemini-2.5-flash-lite` | ~$0.04/call |
| Reaction / condense | `gemini-2.5-flash-lite` | ~$0.0001/call |
| Sign-off / summary | `gemini-2.5-flash-lite` | ~$0.0005/call |
| WDIM catchup (video) | `gemini-2.5-pro` | ~$0.03/call |

Typical 3-movie night: **~$1.10** in Gemini costs. Max tier Kindroid covers unlimited replies.

Header counter shows live session cost (`N calls · ~$0.12`). Hover for per-call-type breakdown.

---

## Session lifecycle

1. **Start** — tap ▶ Start on the splash. Session row created in SQLite. If a movie is already playing on Plex, a briefing card generates + Eli greets.
2. **Chat** — type messages or tap emoji reactions. Each triggers: ffmpeg clip → Gemini scene analysis → Kindroid relay → Eli's reply rendered with interleaved emotes + dialogue.
3. **Ingestion** — 💨 Smoke / 🍬 Edible / 🔥 Dab buttons post a first-person action emote and trigger Eli's reaction. Level curves auto-compute; leaves render on subsequent messages; every 2-3 messages a reinforcement emote fires to keep Eli's altered state alive in context.
4. **Away / Standby** — ⏸ pauses pipeline, auto-generates WDIM catch-up card on return (Eli never sees it). 🛡 double-tap hard-stops everything.
5. **Movie switch** — when Plex detects a different rating key, a continuation briefing card fires.
6. **Stop** — sign-off generated (Tim → Eli direct speech, warm amber card), marathon summary card (stats + engagement analysis), journal entry written to `data/journals/`.

---

## Environment variables

| Var | Required | What |
|---|---|---|
| `PLEX_URL` | ✓ | Plex server URL (e.g. `https://pixel-direct.usbx.me:14975`) |
| `PLEX_TOKEN` | ✓ | Plex X-Plex-Token |
| `GEMINI_API_KEY` | ✓ | Google AI Studio key |
| `KINDROID_API_KEY` | ✓ | Kindroid API key |
| `KINDROID_AI_ID` | ✓ | Kindroid character ID |
| `SECRET_KEY` | ✓ | Random string for signed session cookies |
| `PLEX_VERIFY_SSL` | | `false` (default) — seedbox certs often mismatch |

---

## Data on disk

- `data/moviemode.db` — SQLite: sessions, messages, movies, ingestion, gemini_calls
- `data/journals/*.txt` — per-session plain-text journal entries
- `static/frames/*.jpg` — currently unused (frame feature was removed — see git history)

Bind-mounted in docker-compose so they survive container rebuilds.
