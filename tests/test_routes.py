"""Route-level tests for the voice plugin."""

from pathlib import Path

from voice_plugin.connection import VoiceConnection
from voice_plugin.routes import _VOICE_CAPABILITY_TOKEN


def test_health_endpoint(client):
    resp = client.get("/voice/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["plugin"] == "voice"


def test_static_index_returns_200(client):
    resp = client.get("/voice/")
    assert resp.status_code == 200


def test_status_endpoint_unconfigured(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resp = client.get("/voice/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unconfigured"
    assert data["api_key_set"] is False


def test_status_endpoint_configured(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    resp = client.get("/voice/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["api_key_set"] is True


def test_create_session_voice_only_mode(client):
    """POST /voice/sessions succeeds in voice-only mode (no session_manager).

    When session_manager is None the handler falls back to a plain UUID
    (voice-only mode).  The route must respond 200, not 404.
    """
    resp = client.post("/voice/sessions", json={})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "session_id" in data
    assert len(data["session_id"]) > 0


def test_create_session_rejects_client_workspace_root(client):
    """The working directory is server-controlled."""
    resp = client.post(
        "/voice/sessions",
        json={"workspace_root": "/tmp/test-workspace"},
    )
    assert resp.status_code == 400
    assert "configured by the server" in resp.json()["error"]


def test_create_session_uses_configured_workspace(
    client, monkeypatch, tmp_path
):
    configured_workspace = tmp_path / "trusted-workspace"
    configured_workspace.mkdir()
    monkeypatch.setenv("AMPLIFIER_WORKSPACE_ROOT", str(configured_workspace))
    received: dict[str, str] = {}

    async def capture_create(
        self: VoiceConnection,
        workspace_root: str,
        bundle_name: str | None = None,
    ) -> str:
        received["workspace_root"] = workspace_root
        return "trusted-session"

    monkeypatch.setattr(VoiceConnection, "create", capture_create)

    resp = client.post("/voice/sessions", json={})

    assert resp.status_code == 200
    assert Path(received["workspace_root"]) == configured_workspace.resolve()


def test_create_session_empty_body(client):
    """POST /voice/sessions works with no body (uses default workspace root)."""
    resp = client.post("/voice/sessions")
    assert resp.status_code == 200
    assert "session_id" in resp.json()


def test_create_session_returns_unique_ids(client):
    """Each POST /voice/sessions call returns a distinct session_id."""
    r1 = client.post("/voice/sessions", json={})
    r2 = client.post("/voice/sessions", json={})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["session_id"] != r2.json()["session_id"]


def test_sessions_list_empty(client):
    resp = client.get("/voice/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_sessions_stats_empty(client):
    resp = client.get("/voice/sessions/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0


def test_sdp_missing_body(client):
    resp = client.post("/voice/sdp", content=b"")
    assert resp.status_code == 400


def test_sdp_missing_auth(client):
    resp = client.post("/voice/sdp", content=b"v=0\r\n")
    assert resp.status_code == 401


def test_tool_execute_missing_name(client):
    resp = client.post("/voice/tools/execute", json={})
    assert resp.status_code == 400
    assert "name" in resp.json()["error"]


def test_tool_execute_unknown_tool(client):
    resp = client.post("/voice/tools/execute", json={"name": "unknown_tool"})
    assert resp.status_code == 400
    assert "Unknown tool" in resp.json()["error"]


def test_tool_delegate_no_session(client):
    resp = client.post(
        "/voice/tools/execute",
        json={"name": "delegate", "arguments": {"instruction": "test"}},
    )
    assert resp.status_code == 400
    assert "No active" in resp.json()["error"]


def test_session_and_tool_routes_require_authorization(unauthenticated_client):
    create_resp = unauthenticated_client.post("/voice/sessions", json={})
    tool_resp = unauthenticated_client.post(
        "/voice/tools/execute",
        json={
            "name": "delegate",
            "arguments": {"instruction": "test"},
            "session_id": "session-1",
        },
    )

    assert create_resp.status_code == 401
    assert tool_resp.status_code == 401


def test_tool_execute_rejects_cross_origin(client):
    resp = client.post(
        "/voice/tools/execute",
        headers={"Origin": "https://attacker.example"},
        json={
            "name": "delegate",
            "arguments": {"instruction": "test"},
            "session_id": "session-1",
        },
    )

    assert resp.status_code == 403


def test_tool_execute_rejects_text_plain(client):
    resp = client.post(
        "/voice/tools/execute",
        content='{"name":"delegate","arguments":{"instruction":"test"}}',
        headers={"Content-Type": "text/plain"},
    )

    assert resp.status_code == 415


def test_tool_execute_rejects_other_session(client, monkeypatch):
    class FakeConnection:
        session_id = "active-session"

        async def execute(self, instruction):
            raise AssertionError("execute must not be called")

    import voice_plugin.routes as routes

    monkeypatch.setattr(routes, "_active_connection", FakeConnection())
    resp = client.post(
        "/voice/tools/execute",
        json={
            "name": "delegate",
            "arguments": {"instruction": "test"},
            "session_id": "other-session",
        },
    )

    assert resp.status_code == 403


def test_tool_execute_allows_active_session(client, monkeypatch):
    class FakeConnection:
        session_id = "active-session"

        async def execute(self, instruction):
            return f"executed: {instruction}"

    import voice_plugin.routes as routes

    monkeypatch.setattr(routes, "_active_connection", FakeConnection())
    resp = client.post(
        "/voice/tools/execute",
        json={
            "name": "delegate",
            "arguments": {"instruction": "test"},
            "session_id": "active-session",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"result": "executed: test"}


def test_index_injects_voice_capability(unauthenticated_client):
    resp = unauthenticated_client.get("/voice/")

    assert resp.status_code == 200
    assert (
        f'<meta name="voice-token" content="{_VOICE_CAPABILITY_TOKEN}">'
        in resp.text
    )
    assert resp.headers["cache-control"] == "no-store"
    assert "HttpOnly" in resp.headers["set-cookie"]
    assert "SameSite=strict" in resp.headers["set-cookie"]


def test_cancel_invalid_level(client):
    resp = client.post(
        "/voice/cancel",
        json={"session_id": "test", "level": "invalid"},
    )
    assert resp.status_code == 400


def test_end_session_invalid_id(client):
    resp = client.post("/voice/sessions/!!invalid!!/end", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Static file catch-all route
# ---------------------------------------------------------------------------


def test_static_vendor_js(client):
    """vendor.js is served with JS content-type."""
    resp = client.get("/voice/static/vendor.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_static_connection_health_mjs(client):
    """connection-health.mjs is served with JS content-type (ES module)."""
    resp = client.get("/voice/static/connection-health.mjs")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_static_amplifier_theme_css(client):
    """amplifier-theme.css is served with CSS content-type."""
    resp = client.get("/voice/static/amplifier-theme.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


def test_static_theme_init_js(client):
    """theme-init.js stub is served successfully."""
    resp = client.get("/voice/static/theme-init.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_static_feedback_widget_js(client):
    """feedback-widget.js stub is served and exposes AmplifierFeedback."""
    resp = client.get("/voice/static/feedback-widget.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "AmplifierFeedback" in resp.text


def test_static_favicon_svg(client):
    """favicon.svg stub is served with SVG content-type."""
    resp = client.get("/voice/static/favicon.svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]


def test_static_missing_file_returns_404(client):
    """Non-existent static files return 404."""
    resp = client.get("/voice/static/does-not-exist.js")
    assert resp.status_code == 404


def test_static_directory_traversal_blocked(client):
    """Path traversal attempts outside static/ are blocked."""
    resp = client.get("/voice/static/../routes.py")
    # FastAPI normalises the path before it reaches the handler, but the
    # handler's resolve() + relative_to() check catches any that slip through.
    assert resp.status_code in (404, 400)


def test_index_html_uses_relative_asset_paths(client):
    """index.html must reference assets with relative paths, not /apps/voice/."""
    resp = client.get("/voice/")
    assert resp.status_code == 200
    # Relative paths should be present
    assert 'src="static/vendor.js"' in resp.text
    assert 'src="static/connection-health.mjs"' in resp.text
    assert 'href="static/amplifier-theme.css"' in resp.text
    # Old absolute /apps/voice/ paths must be gone
    assert "/apps/voice/static/" not in resp.text
    # Global /static/ parent-app paths must be gone
    assert 'href="/static/' not in resp.text
    assert 'src="/static/' not in resp.text
