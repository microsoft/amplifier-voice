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
| `AMPLIFIER_WORKSPACE_ROOT` | User home directory | Trusted working directory for amplifierd sessions; browser requests cannot override it |
| `VOICE_PLUGIN_HOME_DIR` | `~/.amplifier-voice` | Where voice session data is stored |
| `VOICE_PLUGIN_PUBLIC_ORIGIN` | `http://127.0.0.1:8410` | Exact browser-facing HTTP(S) origin; no path, trailing slash, wildcard, userinfo, query, or fragment |

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

The voice UI receives a random capability when `/voice/` loads and sends it on
session-control requests. Restarting the daemon invalidates capabilities held by
open tabs, so reload the page after a restart. Keep the default loopback bind
unless the daemon is placed behind an authenticated, trusted reverse proxy.

### Security migration: origin, proxy and session controls

The plugin checks **Host and effective port before issuing the capability** and
on every voice route (including static files, status, signaling and SSE). When
an `Origin` header is present, its HTTP(S) scheme, normalized hostname and
effective port must match `VOICE_PLUGIN_PUBLIC_ORIGIN`. Missing Origin is allowed
for page navigation and non-browser clients; missing Host, malformed or duplicate
Host/Origin headers, opaque `null` origins and multi-origin lists are rejected.
Explicit default ports are equivalent (`https://voice.example:443` and
`https://voice.example`); HTTP and HTTPS are not.

The default works at **http://127.0.0.1:8410/voice/**. `localhost`, IPv6, a different
port, or TLS requires an explicit setting, loaded once when the router is built:

```bash
# Choose exactly the URL that this instance's browser clients will use.
VOICE_PLUGIN_PUBLIC_ORIGIN=http://localhost:9000 amplifier-voice --port 9000
# IPv6 example (also requires a matching daemon bind):
VOICE_PLUGIN_PUBLIC_ORIGIN='http://[::1]:8410' amplifier-voice --host ::1
```

This setting is separate from the listening address. Changing `--host`, `--port`,
`AMPLIFIERD_PORT` or daemon settings does not expand the origin policy
automatically. The same rule applies to `amplifierd serve` and the
`python -m voice_plugin` development server. Invalid configuration prevents the
voice router from loading (amplifierd may log and skip a failed plugin).

For an authenticated HTTPS reverse proxy, set for example
`VOICE_PLUGIN_PUBLIC_ORIGIN=https://voice.example` in the **server** environment.
The proxy must restrict incoming hosts, authenticate every `/voice/` route
(including HTML and SSE), and preserve the public `Host: voice.example` when
forwarding to the loopback backend. Leave the browser's Origin intact. The plugin
does not trust `Forwarded`, `X-Forwarded-Host`, or `X-Forwarded-Proto` to select an
origin; a forwarded header cannot authorize an otherwise rejected Host.
The configured HTTPS origin also makes the capability cookie Secure even when
the proxy's backend transport is HTTP. Isolate the backend from direct network
access. A Host policy/capability is **not remote-user authentication**: an HTTP
client that can reach the backend can choose the trusted Host.

Custom clients must load `/voice/` and send its `X-Voice-Token` on control requests.
Only `/voice/events` accepts the HttpOnly, SameSite=Strict `voice_token` cookie
instead of that header; the cookie alone cannot mutate a session. `/voice/sdp`
retains its separate ephemeral Bearer credential as well as the origin gate.
Reload after restart to renew both header and cookie credentials. Use one
process/worker and one browser-facing origin per instance: active voice state and
capabilities are process-local. Multiple daemons on the same hostname can overwrite
the same `/voice` cookie across ports and are not supported in one browser profile.

`delegate`, `cancel_current_task`, and `/voice/cancel` now require the exact active
voice `session_id`. A stale tab cannot cancel another session or fall back to an
arbitrary daemon handle. No-active-session requests return 400; a mismatched or
missing ID returns 403. Resume only accepts sessions recorded in the server's voice
repository (404 otherwise), and needs an amplifierd session manager (503 otherwise).
This prevents promoting an unrelated daemon session into the active voice scope.
`/voice/cancel` accepts `level: "graceful"` (default) or
`level: "immediate"`; the UI Stop button sends that field. This is a single-user,
single-active-session interface, not per-tab authorization or a sandbox for tools.

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

# Test the shipped Stop-button handler in-process (requires Node.js)
node --test tests/test_stop_button.mjs

# Run standalone dev server (UI and signaling work; session execution requires amplifierd)
uv run python -m voice_plugin

# Format and lint
uv run ruff format src/ tests/
uv run ruff check src/ tests/
```

The security tests use in-process HTTP/ASGI clients and synthetic provider/daemon
handles. `test_daemon_integration.py` additionally exercises real amplifierd
discovery when that package is installed (otherwise it skips), with server launch
intercepted. These do not establish live browser, proxy, daemon-prewarm or audio
compatibility.

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

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
