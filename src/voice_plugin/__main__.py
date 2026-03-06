"""Standalone dev server for the voice plugin.

Usage:
    cd amplifier-voice
    uv run --extra dev python -m voice_plugin

The UI loads at http://127.0.0.1:8410/voice/
Session creation and execution require amplifierd, but the UI, history,
and signaling endpoints work standalone.
"""

from __future__ import annotations

import argparse
from pathlib import Path


class _MockSettings:
    sessions_dir: Path | None = None


class _MockState:
    session_manager = None
    event_bus = None
    bundle_registry = None
    settings = _MockSettings()


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice plugin dev server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8410)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn
    from fastapi import FastAPI

    from voice_plugin import create_router

    state = _MockState()
    app = FastAPI(title="amplifierd-plugin-voice (dev)")
    app.include_router(create_router(state))

    print(f"Voice plugin dev server → http://{args.host}:{args.port}/voice/")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
