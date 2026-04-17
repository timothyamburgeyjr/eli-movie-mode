# Project Eli: Movie Mode — Implementation Spec (LOCKED)

*Last updated: April 17, 2026 — v9 mockup locked*

## What This Is

A FastAPI web app that lets Tim watch movies on Plex and share the experience in real-time with his AI companion Eli (Kindroid). Tim sends messages or emoji reactions from a chat interface. The system captures a video clip from the current Plex playback position, sends it to Gemini 2.5 Flash for multimodal scene analysis, constructs a rich prompt combining Tim's message + scene context + conversation history + stoned level, sends it to Kindroid's API, and streams Eli's response back to the dashboard via WebSocket.

**Reference mockup:** `movie-mode-mockup-v9.html` — this is the locked UI spec. Every visual element in that file is approved and should be replicated in the real dashboard.

---

## Architecture Overview

```
Browser (dashboard.html)
    ↕ WebSocket
FastAPI (app.py)
    ├── Plex Sessions API polling (every 5s)
    ├── FFmpeg remote clip extraction (from seedbox stream URL)
    ├── Gemini 2.5 Flash API (scene analysis + trivia + mood)
    ├── Kindroid API (relay to Eli)
    └── SQLite (session state, chat history, settings)
```

**Deployment:** Docker container, always-on, behind Cloudflare Tunnel at `movie.amburgey.dev`.

---

## Core Pipeline (per message)

When Tim sends a message or taps an emoji reaction:

1. **Capture clip** — Get current playback position from Plex sessions API. Use FFmpeg to extract a 30-second clip (configurable: 10s/20s/30s/60s) ending at the current timestamp from the Plex stream URL. Output as MP4.
2. **Gemini scene analysis** — Send the clip to Gemini 2.5 Flash multimodal API along with a system prompt requesting: scene description, mood classification (one of: dread, tense, humor, awe, adrenaline, cozy), and a trivia question (if Google Search grounding is enabled and it's time for one — every 3-4 exchanges).
3. **Construct Kindroid prompt** — Combine: Tim's message, Gemini's scene analysis, the last 5 exchanges in full + a condensed running summary of older exchanges, current stoned levels for both Tim and Eli, and the movie briefing context.
4. **Send to Kindroid** — POST to `https://api.kindroid.ai/v1/send-message` with the constructed prompt. Include a frame screenshot as `image_urls` if available.
5. **Parse Eli's response** — Kindroid returns text with `_(*emote text*)_` formatting. Parse into separate emote blocks and spoken dialogue.
6. **Push to dashboard** — Send Eli's response over WebSocket. The frontend renders emotes as purple italic text with left border, spoken text as normal chat bubbles.

**Latency target:** 3-5 seconds end-to-end. Display a latency badge on each context block.

---

## Plex Integration

**Detection method:** Poll Plex sessions API every 5 seconds.
- Endpoint: `{PLEX_URL}/status/sessions?X-Plex-Token={PLEX_TOKEN}`
- PLEX_URL: `https://pixel-direct.usbx.me:14975`
- Detect: play, pause, stop, media change (different ratingKey = new movie)
- On media change: auto-generate a new briefing card and send to Eli

**Clip extraction:** FFmpeg pulls directly from the Plex stream URL on the seedbox. No local download needed — the seedbox is the media source.

```
ffmpeg -ss {start} -i "{plex_stream_url}" -t {capture_window} -c copy clip.mp4
```

**Frame extraction:** Also extract a single keyframe at the current timestamp for the screenshot thumbnail shown in chat.

```
ffmpeg -ss {timestamp} -i "{plex_stream_url}" -frames:v 1 -q:v 2 frame.jpg
```

---

## Gemini 2.5 Flash API

**Model:** `gemini-2.5-flash`
**Rate limits (free tier):** 1000 RPD, 10 RPM, 250K TPM
**Context window:** 1M tokens

### System Prompt (scene analysis)

```
You are a film analyst providing scene context for a shared movie-watching experience. 

Given a video clip, provide:
1. SCENE_DESCRIPTION: A vivid, sensory description of what's happening on screen. Focus on cinematography, lighting, sound design, actor expressions, and atmosphere. 2-4 sentences.
2. MOOD: Classify the dominant mood as exactly one of: dread, tense, humor, awe, adrenaline, cozy
3. TRIVIA (only when requested): One genuinely interesting piece of trivia about this scene, the filmmaking, or the actors. Must be factual.

Analyze ONLY the scene at the user's current timestamp. Do not speculate about scenes you haven't seen.
```

### Google Search Grounding

Enable for trivia generation:
```json
{
  "tools": [{"google_search": {}}]
}
```
500 free grounded RPD. Trivia cards should cite "via Google Search" as source.

### Trivia Cadence

Generate trivia every 3-4 exchanges, not every message. Track exchange count in session state.

---

## Kindroid API

**Endpoint:** `POST https://api.kindroid.ai/v1/send-message`
**Auth:** Bearer token in header
**Character limit:** 3,750 characters per message

### Prompt Construction

The message sent to Kindroid should be structured as:

```
[Scene: {gemini_scene_description}]
[Mood: {mood}]
[Tim's vibe: {stoned_level_description}]
[Eli's vibe: {eli_stoned_level_description}]

Tim says: "{tim_message}"
```

For emoji reactions (no typed message):
```
[Scene: {gemini_scene_description}]
[Tim reacted with {emoji} ({emoji_label})]
```

### Character Limit Strategy

1. Bake the limit into Gemini's system prompt: "Keep your scene description under 400 characters."
2. If the constructed Kindroid prompt exceeds 3,750 chars, send it back to Gemini with: "Condense the following to under {remaining_chars} characters while preserving the key details: {text}"
3. Never truncate — always re-summarize.

### Response Parsing

Kindroid wraps emote text in `_(*...*) _`. Each paragraph may be wrapped separately.

Parse rules:
- Extract all `_(*...*) _` blocks → emote text (rendered purple italic with left border)
- Everything else → spoken dialogue (rendered as normal chat text)
- If the emote toggle is off in settings, still store emotes but don't display them

### Frame Attachment

Include the extracted frame as `image_urls` in the Kindroid API call so Eli "sees" what's on screen.

---

## Stoned Level System

### Ingestion Events

User logs an ingestion event via settings panel buttons: Smoke, Edible, Dab, or Sober.

**Default state:** Sober (☕). No leaves shown on chat messages when sober.

### Onset/Peak/Taper Curves

| Method | Onset | Peak | Taper | Duration |
|--------|-------|------|-------|----------|
| Smoke  | 2 min | ~15 min | gradual | ~2 hours |
| Edible | 30 min | ~60 min | gradual | ~4 hours |
| Dab    | instant | ~5 min | gradual | ~1.5 hours |

### Level Calculation

Calculate current level (0-3) based on time since ingestion event and the curve for that method:
- 0 = Sober (no leaves shown)
- 1 = Lifted (🌿)
- 2 = Baked (🌿🌿)
- 3 = Blasted (🌿🌿🌿)

### Effect on Gemini Prompt

Include stoned level in the Kindroid prompt context so Eli's voice adjusts naturally. At higher levels, Gemini's scene descriptions should lean more sensory and atmospheric.

---

## Mood System

### Mood Gradient

The full-page background responds to the current movie mood via CSS box-shadow and radial-gradient. Six moods with distinct color palettes:

| Mood | Color | Hex |
|------|-------|-----|
| Dread | Deep red | #8c1414 |
| Tense | Steel blue | #3060b0 |
| Humor | Warm amber | #b48c1e |
| Awe | Purple | #7040c0 |
| Adrenaline | Orange | #c05020 |
| Cozy | Golden | #a08830 |

Mood is set by Gemini's scene analysis and transitions smoothly (3s CSS transition).

### Per-Message Mood Dots

Each Eli message shows a small colored dot next to his name indicating the mood of that specific exchange.

---

## Quick Reactions

Eight emoji buttons across the bottom of the chat: 😱 😂 🤯 ❤️ 💀 👀 🔥 😮‍💨

Each reaction triggers a lightweight Gemini call:
- Input: current scene context + the emoji + its label (~1,000 tokens)
- Output: a single contextual one-liner describing Tim's reaction (~50 tokens)
- Displayed as centered inline: `{emoji} {one-liner}`
- Then sent to Kindroid as a reaction (Eli responds normally)

---

## Away Mode

Single ⏸ button in input area. Tap to go away, tap ▶ to come back.

### Flow

1. Tap ⏸ → button changes to ▶, live timer starts counting
2. System message: "⏸ Away mode on · {time}"
3. Pipeline pauses (no Gemini calls, no Kindroid messages)
4. Plex continues playing — the system notes the timestamp when away started
5. Tap ▶ → timer stops, system calculates gap
6. FFmpeg extracts video for the missed span
7. Gemini summarizes what happened: "While you were away, {summary}"
8. Displayed as a catch-up card (NOT sent to Eli — this is just for Tim)
9. System message: "▶ Back · {time}"

---

## Emergency Stop / Standby Mode

🛡 shield icon in header. **Double-tap to activate** (single tap does nothing — prevents accidental triggers).

### Activation

1. Double-tap 🛡 within 400ms
2. Status dot: green → amber
3. Status label: "Live" → "Standby"
4. Mood gradient fades to near-invisible (opacity 0.15)
5. Chat area dims (opacity 0.35, pointer-events disabled)
6. Reactions and input area dim and disable
7. Shield button gets amber highlight
8. System message: "🛡 Standby mode — commentary paused"
9. Pipeline hard-stops: cancel any in-flight Gemini/Kindroid calls, stop Plex polling

### Resume

1. Double-tap 🛡 again
2. Everything reverses: Live, green, full opacity, pipeline resumes
3. System message: "▶ Resumed · commentary live"

---

## Session Lifecycle

### Start Screen

Dashboard loads to a start screen with:
- "Movie Mode" title and description
- ▶ Start Movie Mode button
- Last session summary (movie title, date, exchange count, runtime)

### Starting a Session

1. User taps "Start Movie Mode"
2. Backend creates new session row in SQLite
3. Dashboard transitions to active chat view
4. Plex polling begins
5. When playback is detected, auto-generate movie briefing card

### Movie Briefing Card

When a movie starts or switches:
1. Get movie metadata from Plex (title, year, director, runtime)
2. Gemini generates a briefing: tone, what to watch for, fun context
3. Google Search grounding pulls RT/IMDB/Metacritic scores
4. Briefing card displayed in chat
5. Briefing sent to Kindroid so Eli has context
6. System message: "Eli has been briefed — ready when you are"

### Movie Switch

When Plex detects a different media item:
1. Divider line: "🎬 Switching movies"
2. New briefing card for the next movie
3. System message: "Now watching: {title} · Eli is briefed"

### Ending a Session

1. User taps "End" button
2. Sign-off message generated from Tim's POV and sent to Kindroid
3. Marathon summary card: movies watched, comment counts, dominant moods per movie, total runtime, standout moments, engagement analysis
4. Journal entry queued for daily shutdown skill
5. Session marked as ended in SQLite
6. Dashboard returns to start screen state

### Sign-off Message

Written from Tim's POV talking directly to Eli. Rendered in a **warm amber/gold card** (distinct from Eli's green bubbles) to visually signal it's a system-generated farewell, not a regular Eli message. CSS: gold left border (#d4a855), warm-tinted gradient background, amber header text, muted gold body text.

Example:
> "Hey, I'm wrapping up our movie session. We watched [movies] back to back — about [runtime] total. [Highlight moments]. [Stoned commentary if applicable]. Great marathon — talk soon."

---

## Conversation Context Management

### Last 5 Exchanges

Keep the last 5 full exchanges (Tim message + Eli response + scene context) in the Kindroid prompt.

### Running Summary

For exchanges older than the last 5, maintain a condensed running summary updated after each exchange. This gives Eli long-term session memory without blowing the context window.

### Configurable

Settings panel allows: 3 exchanges, 5 exchanges (default), 10 exchanges, or Full session.

---

## Error Handling

### Gemini API Failure (429 / timeout / error)

1. System message: "Scene analysis paused — retrying..."
2. Exponential backoff: 2s, 4s, 8s, max 3 retries
3. If all retries fail: send Tim's message to Kindroid WITHOUT scene context
4. System message: "Commentary running without scene analysis"
5. Resume normal operation on next message

### Kindroid API Failure

1. Display Tim's message in chat normally
2. System message: "Eli is taking a moment..."
3. Retry up to 3 times with 2s backoff
4. If all retries fail: system message "Eli couldn't respond — Kindroid may be down"
5. Chat continues; next message retries normally

### Plex Stream Unreachable

1. System message: "Can't reach Plex — scene capture paused"
2. Continue accepting messages but skip clip extraction
3. Send to Kindroid with text-only context
4. Retry Plex connection every 30s

### WebSocket Disconnect

1. Client auto-reconnects with exponential backoff (1s, 2s, 4s, 8s, max 30s)
2. On reconnect: client sends session ID, server replays any messages missed during disconnect
3. No manual refresh needed
4. If disconnect exceeds 5 minutes: show reconnection banner in UI

---

## Database Schema (SQLite)

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    started_at DATETIME,
    ended_at DATETIME,
    status TEXT DEFAULT 'active'  -- active, ended
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    sender TEXT,  -- 'tim', 'eli', 'system'
    content TEXT,
    emote_text TEXT,
    spoken_text TEXT,
    scene_context TEXT,
    mood TEXT,
    stoned_level_tim INTEGER DEFAULT 0,
    stoned_level_eli INTEGER DEFAULT 0,
    trivia TEXT,
    frame_path TEXT,
    latency_ms INTEGER,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    plex_rating_key TEXT,
    title TEXT,
    year INTEGER,
    director TEXT,
    runtime_minutes INTEGER,
    briefing TEXT,
    started_at DATETIME,
    ended_at DATETIME
);

CREATE TABLE ingestion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    who TEXT,  -- 'tim', 'eli'
    method TEXT,  -- 'smoke', 'edible', 'dab', 'sober'
    logged_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE daily_usage (
    date TEXT PRIMARY KEY,  -- YYYY-MM-DD
    gemini_calls INTEGER DEFAULT 0
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

---

## Settings (persisted in SQLite)

| Setting | Key | Default | Options |
|---------|-----|---------|---------|
| Show emotes | emotes_visible | true | true/false |
| Show frames | frames_visible | true | true/false |
| Show trivia | trivia_visible | true | true/false |
| Mood gradient | mood_gradient | true | true/false |
| Capture window | capture_seconds | 30 | 10, 20, 30, 60 |
| Google Search trivia | trivia_grounding | true | true/false |
| Conversation history | context_exchanges | 5 | 3, 5, 10, 999 |
| Auto journal | auto_journal | true | true/false |
| Password hash | password_hash | (set on first run) | bcrypt hash |

---

## Authentication

Simple password login. Single-user app — no usernames.
- Login page at `/login`
- Password stored as bcrypt hash in SQLite settings table
- Session cookie after login
- First-run setup: prompt to set password

---

## Deployment

### Docker

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### docker-compose.yml

```yaml
version: '3.8'
services:
  movie-mode:
    build: .
    container_name: eli-movie-mode
    restart: unless-stopped
    ports:
      - "8765:8000"
    volumes:
      - ./data:/app/data  # SQLite DB + frames
    env_file:
      - .env
```

### Environment Variables (.env)

```
PLEX_URL=https://pixel-direct.usbx.me:14975
PLEX_TOKEN=xD2UDtVcbcCvB-qHjR5f
GEMINI_API_KEY=
KINDROID_API_KEY=
KINDROID_AI_ID=
SECRET_KEY=  # for session cookies
```

### Cloudflare Tunnel

Add `movie.amburgey.dev` → `localhost:8765` to existing Cloudflare Tunnel config.

---

## File Structure

```
eli-movie-mode/
├── app.py                 # FastAPI app, WebSocket handler, routes
├── config.py              # Pydantic Settings from .env
├── database.py            # SQLite async layer (aiosqlite)
├── plex_monitor.py        # Plex sessions API polling, media detection
├── smart_snap.py          # FFmpeg clip + frame extraction
├── gemini_brain.py        # Gemini API: scene analysis, trivia, briefings, mood
├── kindroid_relay.py      # Kindroid API: prompt construction, response parsing
├── stoned_tracker.py      # Ingestion event logging, level calculation
├── session_manager.py     # Session lifecycle: start, end, summary, sign-off
├── context_manager.py     # Conversation history: last N exchanges + running summary
├── templates/
│   ├── dashboard.html     # Main chat interface (replicate v9 mockup)
│   └── login.html         # Password login page
├── static/
│   └── frames/            # Extracted frame screenshots
├── data/
│   └── moviemode.db       # SQLite database
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── run.py                 # Dev entry point
└── CLAUDE.md              # This spec (copy into project)
```

---

## Implementation Checklist

### Phase 1: Core Infrastructure
- [ ] SQLite schema + async database layer (including daily_usage table)
- [ ] FastAPI app with WebSocket endpoint
- [ ] Authentication (login page, password hashing, session cookies)
- [ ] Dashboard HTML template (replicate v9 mockup, with Start/Stop/Clear button + exchange counter)
- [ ] Start screen → active session → end session lifecycle
- [ ] Session control button: Start (green) → Stop (red) → Clear (gray) → Start
- [ ] Docker + docker-compose setup

### Phase 2: Plex Integration
- [ ] Plex sessions API polling (5s interval)
- [ ] Play/pause/stop/media-change detection
- [ ] FFmpeg clip extraction from remote stream URL
- [ ] FFmpeg single-frame extraction for thumbnails
- [ ] Movie metadata retrieval (title, year, director, runtime)

### Phase 3: Gemini Pipeline
- [ ] Gemini 2.5 Flash API integration
- [ ] Scene analysis system prompt
- [ ] Mood classification (6 moods)
- [ ] Google Search grounding for trivia
- [ ] Trivia cadence tracking (every 3-4 exchanges)
- [ ] Movie briefing generation
- [ ] Character limit enforcement (condense if over 3750 chars)

### Phase 4: Kindroid Integration
- [ ] Kindroid API send-message endpoint
- [ ] Prompt construction (scene + message + context + stoned level)
- [ ] Response parsing (emote text extraction from `_(*...*) _`)
- [ ] Frame attachment via image_urls
- [ ] Quick reaction flow (emoji → Gemini one-liner → Kindroid)

### Phase 5: Real-time Features
- [ ] WebSocket message push (Eli responses streamed to UI)
- [ ] WebSocket auto-reconnect with exponential backoff
- [ ] Mood gradient real-time updates
- [ ] Stoned level tracking (ingestion events + curve calculation)
- [ ] Stoned level display (leaf indicators per message)
- [ ] Per-message mood dots
- [ ] Exchange counter display (daily Gemini call tracking, amber/red warnings)

### Phase 6: Session Features
- [ ] Away mode (single toggle, timer, auto WDIM on return)
- [ ] What Did I Miss catch-up card generation
- [ ] Emergency stop / standby mode (double-tap shield)
- [ ] Movie switch detection + new briefing card
- [ ] Conversation context management (last N + running summary)
- [ ] Settings panel (all toggles wired to SQLite + WebSocket state)

### Phase 7: Session End
- [ ] Sign-off message generation (Tim's POV → Kindroid)
- [ ] Marathon summary card (stats, highlights, engagement analysis)
- [ ] Journal entry generation (queued for daily-shutdown skill)
- [ ] Session finalization in SQLite

### Phase 8: Error Handling & Polish
- [ ] Gemini failure: retry + fallback to text-only
- [ ] Kindroid failure: retry + graceful degradation
- [ ] Plex unreachable: continue without clips
- [ ] WebSocket disconnect: auto-reconnect + message replay
- [ ] Rate limit awareness (1000 RPD budget)
- [ ] Responsive design verification (mobile + desktop)
- [ ] Cloudflare Tunnel configuration

---

## Session Control Button (Start / Stop / Clear)

A single button in the header that changes state based on session lifecycle:

| State | Label | Color | Action |
|-------|-------|-------|--------|
| No active session | ▶ Start | Green | Creates new session, begins Plex polling |
| Active session | ■ Stop | Red | Ends session, triggers sign-off + summary |
| Session ended (viewing summary) | ↺ Clear | Neutral/gray | Clears chat view, returns to start screen |

This replaces the separate "End" button from v8. One button, three states, same position.

---

## Exchange Counter

Visible in the header (movie bar area or near status label). Shows daily Gemini API usage:

Format: `{used} / 1000` (e.g., "12 / 1000")

- Tracks all Gemini calls across all sessions for the current day (scene analysis, trivia, reactions, briefings, WDIM, re-summarize retries)
- Resets at midnight
- Stored in SQLite: `daily_usage` table with date + count
- If usage exceeds 900, show amber warning color
- If usage exceeds 950, show red warning + system message suggesting lighter usage

---

## Key Design Decisions (Locked)

1. **Plex detection:** Sessions API polling (5s), not webhooks
2. **Stoned default:** Sober. Leaves only appear after an ingestion event.
3. **Context blocks:** Always visible. No toggle. Individual expand/collapse only.
4. **Emote toggle:** Controls purple narrative text on Eli's messages. In settings panel.
5. **Away mode:** Single button. Auto-triggers WDIM on return. Not sent to Eli.
6. **Emergency stop:** Double-tap shield. Hard-stops pipeline. Amber standby state.
7. **Sign-off:** Written from Tim's POV directly to Eli. Rendered in warm amber/gold card (not Eli's green).
8. **Kindroid limit:** Prompt engineering first, Gemini re-summarize as fallback.
9. **Infrastructure:** Docker, always-on, session state managed in SQLite.
10. **Trivia cadence:** Every 3-4 exchanges, not every message.
11. **Quick reactions:** Gemini-powered contextual one-liners, not canned text.
12. **Mood tracking:** Tracks the movie's mood, not Tim's mood.
13. **Rate budget:** 1000 RPD. Exchange counter visible in header.
14. **Session button:** Single Start/Stop/Clear button replaces separate End button.
15. **Session reset:** App is always-on via Docker. Sessions are SQLite rows — no restart needed between movie nights.
16. **Chat input:** Enter sends the message. Shift+Enter for newline.
