"""VoiceConnection - manages one voice session lifecycle.

Adapted for amplifierd daemon: uses SessionManager + EventBus instead of
FoundationBackend. The daemon's EventBus handles SSE streaming natively,
so this connection primarily manages session state and the voice-specific
event queue for the browser's custom SSE stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_EVENT_QUEUE_MAX_SIZE = 10000


class VoiceConnection:
    """Manages one voice session lifecycle: create, teardown, end, cancel."""

    def __init__(
        self,
        repository: Any,
        session_manager: Any,
        event_bus: Any,
        sessions_dir: Any = None,
    ) -> None:
        self._repository = repository
        self._session_manager = session_manager
        self._event_bus = event_bus
        self._sessions_dir = sessions_dir
        self._event_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_MAX_SIZE
        )
        self._session_id: str | None = None
        self._handle: Any = None
        self._subscription_task: asyncio.Task[None] | None = None

    @property
    def event_queue(self) -> asyncio.Queue[Any]:
        """The asyncio.Queue used as the event bus for this connection."""
        return self._event_queue

    @property
    def session_id(self) -> str | None:
        """The current session ID, or None if not yet created."""
        return self._session_id

    async def create(self, workspace_root: str, bundle_name: str | None = None) -> str:
        """Create a session for this voice connection via amplifierd SessionManager.

        1. Resolves bundle_name: uses the provided value, falls back to
           ``session_manager._settings.default_bundle``, or generates a plain
           UUID (voice-only mode) when no session manager / bundle is available.
        2. Creates session via session_manager.create()
        3. Subscribes to EventBus for this session's events
        4. Forwards events to the voice-specific event queue for SSE

        Voice-only mode (no amplifierd session): the WebRTC/audio path works
        normally but the delegate tool will report "No active session".
        """
        # Resolve bundle: explicit arg → session_manager default → None (UUID fallback)
        if bundle_name is None and self._session_manager is not None:
            bundle_name = getattr(
                getattr(self._session_manager, "_settings", None),
                "default_bundle",
                None,
            )

        # If no session manager or no bundle configured, run in voice-only mode.
        if self._session_manager is None or bundle_name is None:
            from uuid import uuid4

            self._session_id = str(uuid4())
            logger.info(
                "Voice session created in voice-only mode (no bundle configured): %s",
                self._session_id,
            )
            return self._session_id

        handle = await self._session_manager.create(
            bundle_name=bundle_name,
            working_dir=workspace_root,
        )
        self._handle = handle
        self._session_id = handle.session_id

        # Subscribe to EventBus and forward events to our queue
        self._subscription_task = asyncio.create_task(
            self._forward_events(handle.session_id)
        )

        return handle.session_id

    async def _forward_events(self, session_id: str) -> None:
        """Subscribe to EventBus and forward events to the voice event queue."""
        try:
            async for event in self._event_bus.subscribe(session_id=session_id):
                msg = event.to_sse_dict() if hasattr(event, "to_sse_dict") else event
                with contextlib.suppress(asyncio.QueueFull):
                    self._event_queue.put_nowait(msg)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("Event forwarding error for %s", session_id, exc_info=True)

    async def teardown(self) -> None:
        """Handle client disconnect: mark session disconnected, cleanup."""
        try:
            if self._session_id is not None:
                self._repository.update_status(self._session_id, "disconnected")
        finally:
            self._cancel_subscription()
            self._event_queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX_SIZE)

    async def end(self, reason: str = "user_ended") -> None:
        """End the session permanently."""
        try:
            if self._session_id is not None and self._handle is not None:
                await self._handle.cleanup()
                self._repository.end_conversation(self._session_id, reason)
        finally:
            self._cancel_subscription()

    async def cancel(self, level: str = "graceful") -> None:
        """Cancel the running session."""
        if self._handle is not None:
            immediate = level == "immediate"
            self._handle.cancel(immediate=immediate)

    async def execute(self, prompt: str) -> str:
        """Execute a prompt on the session (for delegate tool)."""
        if self._handle is None:
            return "No active session"
        result = await self._handle.execute(prompt)
        return str(result) if result is not None else ""

    def _cancel_subscription(self) -> None:
        """Cancel the EventBus subscription task."""
        if self._subscription_task is not None:
            self._subscription_task.cancel()
            self._subscription_task = None
