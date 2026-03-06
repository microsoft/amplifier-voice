# amplifier-voice

Voice plugin for the [amplifierd](https://github.com/microsoft/amplifierd) daemon — WebRTC voice interface using the OpenAI Realtime API.

Ported from the voice app in [amplifier-distro](https://github.com/microsoft/amplifier-distro) to run as a standalone amplifierd plugin. Audio flows directly between the browser and OpenAI via WebRTC; the plugin handles signaling, session lifecycle, transcript persistence, and event streaming.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An OpenAI API key with access to the Realtime API

## Quick Start

### Run as a standalone app

Install from GitHub with the `standalone` extra, which pulls in amplifierd and everything needed to run the server:

```bash
uv tool install amplifierd-plugin-voice \
  --from "git+https://github.com/microsoft/amplifier-voice[standalone]" \
  --force

amplifier-voice
```

Open http://127.0.0.1:8410/voice/ — the full voice UI with real session execution, delegation, and event streaming.

### Run as an amplifierd plugin

If you already have amplifierd installed and want to add voice as a plugin:

```bash
uv tool install amplifierd \
  --from git+https://github.com/microsoft/amplifierd \
  --with git+https://github.com/microsoft/amplifier-voice

amplifierd serve
```

The plugin is auto-discovered via Python entry points — no configuration needed. Both `amplifier-voice` and `amplifierd serve` boot the same amplifierd platform; the difference is the entry point.

### Editable install for development

Clone the repo and install as an editable tool. Source changes take effect on server restart — no reinstall needed.

```bash
git clone https://github.com/microsoft/amplifier-voice
cd amplifier-voice

uv tool install amplifierd-plugin-voice \
  --from ".[standalone]" \
  --editable \
  --force

amplifier-voice
```

To update dependencies while keeping the voice plugin editable, re-run the same command.

## Configuration

### Required: OpenAI API Key

Set `OPENAI_API_KEY` in your environment:

```bash
export OPENAI_API_KEY=sk-...
```

### Required: Amplifier Provider Config

amplifierd needs at least one LLM provider configured for session execution (tool calls, delegate, etc.). Create or edit `~/.amplifier/settings.yaml`:

```yaml
config:
  providers:
    - module: anthropic
      api_key: ${ANTHROPIC_API_KEY}
```

### Optional: Voice Settings

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OPENAI_API_KEY` | — | **Required.** OpenAI API key for Realtime API |
| `AMPLIFIER_VOICE_MODEL` | `gpt-4o-realtime-preview` | Realtime model to use |
| `AMPLIFIER_VOICE_VOICE` | `ash` | Voice persona (ash, ballad, coral, sage, verse) |
| `AMPLIFIER_VOICE_INSTRUCTIONS` | `""` | System instructions for the voice session |
| `AMPLIFIER_VOICE_ASSISTANT_NAME` | `Amplifier` | Display name in the UI |
| `VOICE_PLUGIN_HOME_DIR` | `~/.amplifier-voice` | Where voice session data is stored |

### Optional: Daemon Settings

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `AMPLIFIERD_HOST` | `127.0.0.1` | Bind address |
| `AMPLIFIERD_PORT` | `8410` | Bind port |
| `AMPLIFIERD_LOG_LEVEL` | `info` | Log verbosity |
| `AMPLIFIERD_DISABLED_PLUGINS` | `[]` | JSON array of plugin names to skip |

Or configure via `~/.amplifierd/settings.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8410,
  "log_level": "info"
}
```

## Verify It's Working

```bash
# Check daemon health
curl http://127.0.0.1:8410/health

# Check voice plugin status
curl http://127.0.0.1:8410/voice/api/status

# Open the voice UI
open http://127.0.0.1:8410/voice/
```

The daemon logs will show `Mounted plugin: voice` on startup if the plugin was discovered successfully.

## How It Works

```
Browser                          amplifierd + voice plugin              OpenAI
  │                                      │                                │
  │  GET /voice/session                  │                                │
  │ ──────────────────────────────────>  │  POST /v1/realtime/client_secrets
  │                                      │ ────────────────────────────>  │
  │  { value: "ek_..." }                │  ephemeral token               │
  │ <──────────────────────────────────  │ <────────────────────────────  │
  │                                      │                                │
  │  POST /voice/sdp (SDP offer)         │                                │
  │ ──────────────────────────────────>  │  POST /v1/realtime/calls       │
  │  SDP answer                          │ ────────────────────────────>  │
  │ <──────────────────────────────────  │ <────────────────────────────  │
  │                                      │                                │
  │  WebRTC audio (Opus) ══════════════════════════════════════════════>  │
  │  <════════════════════════════════════════════════════════════════════  │
  │                                      │                                │
  │  GET /voice/events (SSE)             │                                │
  │ <─────── streaming events ─────────  │                                │
  │                                      │                                │
  │  POST /voice/tools/execute           │                                │
  │ ──────────────────────────────────>  │  (amplifierd session execute)  │
  │  { result: "..." }                   │                                │
  │ <──────────────────────────────────  │                                │
```

Audio flows directly between the browser and OpenAI via WebRTC — the plugin never touches audio data. The plugin handles:

- **Signaling**: Ephemeral token creation and SDP offer/answer relay
- **Session lifecycle**: Create, resume, end sessions via amplifierd's SessionManager
- **Event streaming**: SSE stream of session events to the browser UI
- **Transcript persistence**: Disk-backed conversation history with cross-app visibility
- **Tool execution**: Delegate and cancel via amplifierd session handles

## Theming

The voice plugin ships with the [amplifier-distro](https://github.com/microsoft/amplifier-distro) brand theme — Syne/Epilogue typography, Signal Purple accent, light/dark mode with system preference detection. This means the voice UI looks identical whether running standalone or as a plugin inside amplifier-distro.

Fonts are bundled as WOFF2 files in `static/fonts/` — no CDN dependency.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/voice/` | Voice UI (HTML) |
| `GET` | `/voice/health` | Plugin health check |
| `GET` | `/voice/api/status` | Voice config status (model, voice, API key set) |
| `GET` | `/voice/session` | Create ephemeral client secret |
| `POST` | `/voice/sdp` | Exchange WebRTC SDP offer/answer |
| `POST` | `/voice/sessions` | Create a new voice session |
| `POST` | `/voice/sessions/{id}/resume` | Resume a disconnected session |
| `POST` | `/voice/sessions/{id}/transcript` | Sync transcript entries |
| `POST` | `/voice/sessions/{id}/end` | End a session |
| `GET` | `/voice/sessions` | List all voice sessions |
| `GET` | `/voice/sessions/stats` | Session statistics |
| `POST` | `/voice/tools/execute` | Execute a tool (delegate, cancel) |
| `POST` | `/voice/cancel` | Cancel a running session |
| `GET` | `/voice/events` | SSE event stream |

## Data Storage

Voice session data is stored at `~/.amplifier-voice/` (configurable via `VOICE_PLUGIN_HOME_DIR`):

```
~/.amplifier-voice/
├── index.json                          # Fast session listing
└── {session_id}/
    ├── conversation.json               # Session metadata (atomic write)
    └── transcript.jsonl                 # Append-only transcript
```

When running with amplifierd, transcripts are also mirrored to amplifierd's session directory for cross-app visibility (e.g., showing voice sessions in the chat UI's session list).

## Development

```bash
cd amplifier-voice
uv sync --all-extras

# Run tests
uv run pytest tests/ -v

# Run standalone dev server (UI and signaling work; session execution requires amplifierd)
uv run python -m voice_plugin

# Format and lint
uv run ruff format src/ tests/
uv run ruff check src/ tests/
```

## Plugin Contract

This plugin follows the [amplifierd plugin contract](https://github.com/microsoft/amplifierd/blob/main/docs/plugins.md):

1. Declares `[project.entry-points."amplifierd.plugins"]` in `pyproject.toml`
2. Exports `create_router(state) -> fastapi.APIRouter`
3. Uses `state.session_manager` and `state.event_bus` for daemon integration

To include this in a distribution like amplifier-distro, simply add it as a dependency — the entry-point system handles the rest.

## Building Standalone Apps on amplifierd

This project demonstrates the pattern for building standalone apps on the amplifierd platform:

1. **Write your plugin** — implement `create_router(state) -> APIRouter`
2. **Register the entry point** — `[project.entry-points."amplifierd.plugins"]` in `pyproject.toml`
3. **Add a CLI** — a thin `[project.scripts]` entry that boots amplifierd via `uvicorn.run("amplifierd.app:create_app", factory=True)`
4. **Ship it** — users install with `uv tool install` and get a self-contained command

The same code works as a plugin (discovered by amplifierd) and as a standalone app (the CLI boots amplifierd for you). No conditional logic, no separate code paths.
