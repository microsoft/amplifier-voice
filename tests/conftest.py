import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice_plugin import create_router


class MockSettings:
    sessions_dir = None


class MockState:
    session_manager = None
    event_bus = None
    bundle_registry = None
    settings = MockSettings()


@pytest.fixture
def state():
    return MockState()


@pytest.fixture
def app(state, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_PLUGIN_HOME_DIR", str(tmp_path))
    app = FastAPI()
    app.state = state
    router = create_router(state)
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_active_connection():
    """Reset the module-level _active_connection between tests.

    The voice plugin keeps a single global connection reference so that
    the SSE event stream and tool routes can access it.  Tests that call
    POST /voice/sessions set this global; without teardown, later tests
    see stale state and can fail unexpectedly.
    """
    import voice_plugin.routes as _routes

    _routes._active_connection = None
    yield
    _routes._active_connection = None
