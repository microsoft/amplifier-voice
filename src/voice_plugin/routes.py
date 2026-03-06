"""Voice plugin routes for amplifierd daemon.

Adapted from amplifier-distro voice app to use amplifierd's SessionManager
and EventBus instead of FoundationBackend. The browser-side WebRTC flow
remains identical — audio goes directly browser <-> OpenAI.

Route groups:
  - Static: serves UI HTML and vendor JS
  - Signaling: ephemeral token + SDP exchange (OpenAI Realtime API)
  - Sessions: lifecycle management (create, resume, transcript sync, end, list)
  - Tools: delegate execution + cancel
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)

from voice_plugin.config import VoicePluginSettings, get_voice_config
from voice_plugin.connection import VoiceConnection
from voice_plugin.transcript.models import TranscriptEntry, VoiceConversation
from voice_plugin.transcript.repository import VoiceConversationRepository

logger = logging.getLogger(__name__)

_VALID_SESSION_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")

# Module-level state: single-user, no parallel voice sessions
_active_connection: VoiceConnection | None = None


def _validate_session_id(session_id: str) -> None:
    if not _VALID_SESSION_ID.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")


def _get_workspace_root() -> Path:
    workspace = os.environ.get("AMPLIFIER_WORKSPACE_ROOT", "")
    if workspace:
        return Path(workspace).expanduser().resolve()
    return Path.home()


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------


def create_static_routes() -> APIRouter:
    """Serve the voice UI static files."""
    router = APIRouter()

    @router.get("/voice/", response_class=HTMLResponse, tags=["voice"])
    async def index() -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        if html_path.exists():
            return HTMLResponse(content=html_path.read_text())
        return HTMLResponse(
            content=(
                "<!-- Voice UI not built yet -->"
                "<html><body><h1>Amplifier Voice</h1>"
                "<p>UI not built yet.</p></body></html>"
            ),
            status_code=200,
        )

    # Media-type map; .mjs must be served as JS so browsers accept ES modules
    _STATIC_MIME: dict[str, str] = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".svg": "image/svg+xml",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".ico": "image/x-icon",
        ".png": "image/png",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
    }

    @router.get("/voice/static/{path:path}", response_model=None, tags=["voice"])
    async def static_files(path: str) -> Response:
        """Serve any file from the plugin's static/ directory."""
        static_dir = (Path(__file__).parent / "static").resolve()
        # Resolve and guard against directory traversal
        try:
            file_path = (static_dir / path).resolve()
            file_path.relative_to(static_dir)  # raises ValueError if outside
        except (ValueError, OSError):
            return PlainTextResponse(content="Not found", status_code=404)

        if not file_path.exists() or not file_path.is_file():
            return PlainTextResponse(content="Not found", status_code=404)

        suffix = file_path.suffix.lower()
        media_type = _STATIC_MIME.get(suffix, "application/octet-stream")
        return Response(content=file_path.read_bytes(), media_type=media_type)

    return router


# ---------------------------------------------------------------------------
# Signaling routes (ephemeral token + SDP exchange)
# ---------------------------------------------------------------------------


def create_signaling_routes(settings: VoicePluginSettings) -> APIRouter:
    """OpenAI Realtime API signaling: client_secret + SDP relay."""
    router = APIRouter()

    @router.get("/voice/api/status", tags=["voice"])
    async def voice_status() -> JSONResponse:
        api_key = os.environ.get("OPENAI_API_KEY")
        vcfg = get_voice_config()
        return JSONResponse(
            content={
                "status": "ready" if api_key else "unconfigured",
                "api_key_set": bool(api_key),
                "model": vcfg["model"],
                "voice": vcfg["voice"],
                "assistant_name": vcfg["assistant_name"],
            }
        )

    @router.get("/voice/session", tags=["voice"])
    async def create_session_token() -> JSONResponse:
        """Return ephemeral client_secret for the GA Realtime API."""
        from voice_plugin import realtime as rt

        vcfg = get_voice_config()
        config = rt.VoiceConfig(
            model=vcfg["model"],
            voice=vcfg["voice"],
            instructions=vcfg["instructions"],
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        )
        token = await rt.create_client_secret(config)
        return JSONResponse(content={"value": token})

    @router.post("/voice/sdp", response_model=None, tags=["voice"])
    async def exchange_sdp(request: Request) -> PlainTextResponse | JSONResponse:
        """Exchange WebRTC SDP offer/answer."""
        offer_sdp = (await request.body()).decode(errors="replace")
        if not offer_sdp:
            return JSONResponse(
                status_code=400, content={"error": "SDP offer body required"}
            )

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "Bearer ephemeral token required"},
            )

        ephemeral_token = auth[len("Bearer ") :]
        vcfg = get_voice_config()

        from voice_plugin import realtime as rt

        sdp_answer = await rt.exchange_sdp(offer_sdp, ephemeral_token, vcfg["model"])
        return PlainTextResponse(content=sdp_answer, media_type="application/sdp")

    return router


# ---------------------------------------------------------------------------
# Session lifecycle routes
# ---------------------------------------------------------------------------


def create_session_routes(
    state: Any,
    settings: VoicePluginSettings,
    sessions_dir: Path | None,
) -> APIRouter:
    """Session management: create, resume, transcript sync, end, list."""
    router = APIRouter()

    def _get_repo() -> VoiceConversationRepository:
        return VoiceConversationRepository(
            base_dir=settings.home_dir,
            sessions_dir=sessions_dir,
        )

    @router.get("/voice/events", tags=["voice"])
    async def events_stream(request: Request) -> StreamingResponse:
        """SSE event stream from the active VoiceConnection."""

        async def _generate():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    conn = _active_connection
                    if conn is not None:
                        try:
                            event = await asyncio.wait_for(
                                conn.event_queue.get(), timeout=5.0
                            )
                            # Normalize tuples to dicts
                            if isinstance(event, tuple) and len(event) == 2:
                                raw_name, data = event
                                type_name = (
                                    str(raw_name)
                                    .replace(":", "_")
                                    .replace("_block", "")
                                )
                                event = {
                                    "type": type_name,
                                    "event": raw_name,
                                    **(data or {}),
                                }
                            yield f"data: {_json.dumps(event)}\n\n"
                        except TimeoutError:
                            yield ": heartbeat\n\n"
                    else:
                        yield ": heartbeat\n\n"
                        await asyncio.sleep(5.0)
            except (asyncio.CancelledError, GeneratorExit):
                pass

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/voice/sessions", tags=["voice"])
    async def create_session(request: Request) -> JSONResponse:
        """Create an amplifierd session and wire it to a VoiceConnection."""
        global _active_connection

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}

        workspace_root = body.get("workspace_root", str(_get_workspace_root()))
        repo = _get_repo()

        # Resolve the bundle to use for the backing amplifierd session (needed
        # by the delegate tool). Falls back to None, which triggers voice-only
        # mode (UUID session; delegate unavailable) when no bundle is configured.
        bundle_name: str | None = getattr(
            getattr(state, "settings", None), "default_bundle", None
        )

        conn = VoiceConnection(
            repository=repo,
            session_manager=state.session_manager,
            event_bus=state.event_bus,
            sessions_dir=sessions_dir,
        )
        session_id = await conn.create(workspace_root, bundle_name=bundle_name)

        conv = VoiceConversation(
            id=session_id,
            title=f"Voice session {session_id[:8]}",
            status="active",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        repo.create_conversation(conv)
        _active_connection = conn

        # Write cross-app visibility stubs
        repo.write_to_amplifierd_transcript(session_id, [])
        repo.write_amplifierd_metadata(session_id, conv)

        logger.info("Voice session created: %s", session_id)
        return JSONResponse(content={"session_id": session_id})

    @router.post("/voice/sessions/{session_id}/resume", tags=["voice"])
    async def resume_session(
        session_id: str,
        request: Request,
    ) -> JSONResponse:
        """Reconnect after a disconnect; returns fresh client_secret + context."""
        _validate_session_id(session_id)
        global _active_connection

        repo = _get_repo()
        context = repo.get_resumption_context(session_id)

        # Obtain a fresh ephemeral token
        from voice_plugin import realtime as rt

        vcfg = get_voice_config()
        config = rt.VoiceConfig(
            model=vcfg["model"],
            voice=vcfg["voice"],
            instructions=vcfg["instructions"],
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        )
        client_secret = await rt.create_client_secret(config)

        # Create a fresh VoiceConnection for resume
        conn = VoiceConnection(
            repository=repo,
            session_manager=state.session_manager,
            event_bus=state.event_bus,
            sessions_dir=sessions_dir,
        )
        conn._session_id = session_id

        # Resume via session_manager
        handle = await state.session_manager.resume(session_id)
        if handle is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Session {session_id} not found"},
            )
        conn._handle = handle

        # Start event forwarding for the resumed session
        conn._subscription_task = asyncio.create_task(conn._forward_events(session_id))
        _active_connection = conn

        logger.info("Voice session resumed: %s", session_id)
        return JSONResponse(
            content={
                "client_secret": client_secret,
                "context_to_inject": context,
            }
        )

    @router.post("/voice/sessions/{session_id}/transcript", tags=["voice"])
    async def sync_transcript(
        session_id: str,
        request: Request,
    ) -> JSONResponse:
        """Batch-add TranscriptEntry records for a session."""
        _validate_session_id(session_id)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "Body must be a JSON object with 'entries' key"},
            )

        entries_data: list[dict[str, Any]] = body.get("entries", [])
        repo = _get_repo()
        now = datetime.now(UTC)

        entries = [
            TranscriptEntry(
                id=str(uuid4()),
                conversation_id=session_id,
                role=e.get("role", "user"),
                content=e.get("content", ""),
                created_at=now,
                item_id=e.get("item_id"),
                tool_name=e.get("tool_name"),
                call_id=e.get("call_id"),
            )
            for e in entries_data
        ]
        repo.add_entries(session_id, entries)

        # Mirror to amplifierd transcript for cross-app visibility
        repo.write_to_amplifierd_transcript(session_id, entries)

        return JSONResponse(content={"synced": len(entries)})

    @router.post("/voice/sessions/{session_id}/end", tags=["voice"])
    async def end_session(
        session_id: str,
        request: Request,
    ) -> JSONResponse:
        """End a session permanently."""
        _validate_session_id(session_id)
        global _active_connection

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}

        _VALID_REASONS = {
            "session_limit",
            "network_error",
            "user_ended",
            "idle_timeout",
            "error",
        }
        raw_reason: str = body.get("reason", "user_ended")
        reason = raw_reason if raw_reason in _VALID_REASONS else "error"

        conn = _active_connection
        if conn is not None and conn.session_id == session_id:
            await conn.end(reason)
            _active_connection = None
        else:
            repo = _get_repo()
            repo.end_conversation(session_id, reason)  # type: ignore[arg-type]

        logger.info("Voice session ended: %s (reason=%s)", session_id, reason)
        return JSONResponse(content={"ended": True, "session_id": session_id})

    @router.get("/voice/sessions", tags=["voice"])
    async def list_sessions() -> JSONResponse:
        """Return the list of VoiceConversations from the repository index."""
        repo = _get_repo()
        return JSONResponse(content=repo.list_conversations())

    @router.get("/voice/sessions/stats", tags=["voice"])
    async def sessions_stats() -> JSONResponse:
        """Return aggregate statistics across all voice sessions."""
        repo = _get_repo()
        conversations = repo.list_conversations()
        by_status: dict[str, int] = {}
        for conv in conversations:
            status = conv.get("status", "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        return JSONResponse(
            content={
                "total": len(conversations),
                "by_status": by_status,
            }
        )

    return router


# ---------------------------------------------------------------------------
# Tool execution routes
# ---------------------------------------------------------------------------


def create_tool_routes(state: Any) -> APIRouter:
    """Voice tool execution: delegate and cancel."""
    router = APIRouter()

    @router.post("/voice/tools/execute", tags=["voice"])
    async def execute_tool(request: Request) -> JSONResponse:
        """Execute a voice tool on behalf of the active voice session."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

        name: str = body.get("name", "")
        arguments: dict[str, Any] = body.get("arguments", {})

        if not name:
            return JSONResponse(
                status_code=400, content={"error": "Missing 'name' field"}
            )

        conn = _active_connection

        if name == "delegate":
            instruction = arguments.get("instruction", "")
            if not instruction:
                return JSONResponse(
                    status_code=400,
                    content={"error": "instruction required for delegate"},
                )
            if conn is None or conn.session_id is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": "No active voice session"},
                )
            result = await conn.execute(instruction)
            return JSONResponse(content={"result": result})

        if name == "cancel_current_task":
            if conn is None:
                return JSONResponse(
                    status_code=400,
                    content={"error": "No active voice session"},
                )
            await conn.cancel()
            return JSONResponse(content={"result": "cancelled"})

        return JSONResponse(status_code=400, content={"error": f"Unknown tool: {name}"})

    @router.post("/voice/cancel", tags=["voice"])
    async def cancel_session(request: Request) -> JSONResponse:
        """Cancel the running session."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

        session_id: str = body.get("session_id", "")
        level: str = body.get("level", "graceful")
        if level not in ("graceful", "immediate"):
            return JSONResponse(
                status_code=400,
                content={"error": "level must be 'graceful' or 'immediate'"},
            )

        conn = _active_connection
        if conn is not None and conn.session_id == session_id:
            await conn.cancel(level=level)
        elif state.session_manager is not None:
            handle = state.session_manager.get(session_id)
            if handle is not None:
                handle.cancel(immediate=(level == "immediate"))

        return JSONResponse(content={"cancelled": True, "session_id": session_id})

    return router
