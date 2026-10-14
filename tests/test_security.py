"""Credential-free in-process boundary tests, not browser/daemon/audio proof."""

import asyncio
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice_plugin import create_router, routes
from voice_plugin.config import VoicePluginSettings
from voice_plugin.connection import VoiceConnection
from voice_plugin.security import canonical_origin

ORIGIN = "http://127.0.0.1:8410"
TOKEN = {"X-Voice-Token": routes._VOICE_CAPABILITY_TOKEN}
ROUTES = [
    ("GET", "/voice/"),
    ("GET", "/voice/health"),
    ("GET", "/voice/static/vendor.js"),
    ("GET", "/voice/api/status"),
    ("GET", "/voice/session"),
    ("POST", "/voice/sdp"),
    ("GET", "/voice/events"),
    ("POST", "/voice/sessions"),
    ("POST", "/voice/sessions/example/resume"),
    ("POST", "/voice/sessions/example/transcript"),
    ("POST", "/voice/sessions/example/end"),
    ("GET", "/voice/sessions"),
    ("GET", "/voice/sessions/stats"),
    ("POST", "/voice/tools/execute"),
    ("POST", "/voice/cancel"),
]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("host,origin", [
    ("attacker.example:8410", "http://attacker.example:8410"),
    ("127.0.0.1:9000", "http://127.0.0.1:9000"),
    ("127.0.0.1:8410", "https://127.0.0.1:8410"),
])
def test_every_route_rejects_untrusted_authority_before_work(
    unauthenticated_client, method, path, host, origin
):
    response = unauthenticated_client.request(
        method, path, headers={**TOKEN, "Host": host, "Origin": origin}
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert routes._VOICE_CAPABILITY_TOKEN not in response.text
    assert routes._active_connection is None


@pytest.mark.parametrize("name,value,status", [
    ("Host", "127.0.0.1:8410", 400),
    ("Origin", ORIGIN, 400),
    ("X-Voice-Token", routes._VOICE_CAPABILITY_TOKEN, 400),
    ("Cookie", "voice_token=" + routes._VOICE_CAPABILITY_TOKEN, 400),
])
def test_duplicate_security_headers(unauthenticated_client, name, value, status):
    path = "/voice/events" if name == "Cookie" else "/voice/sessions"
    headers = [(name, value), (name, value)]
    if name != "X-Voice-Token":
        headers += list(TOKEN.items())
    response = unauthenticated_client.get(path, headers=headers)
    assert response.status_code == status


@pytest.mark.parametrize("name,value,path", [
    ("Content-Type", "application/json", "/voice/cancel"),
    ("Content-Length", "2", "/voice/cancel"),
    ("Authorization", "Bearer synthetic", "/voice/sdp"),
])
def test_duplicate_body_and_bearer_headers(client, name, value, path):
    response = client.post(path, content="{}", headers=[(name, value), (name, value)])
    assert response.status_code == 400


@pytest.mark.parametrize("name,value", [
    (b"x-voice-token", b"\xff"),
    (b"x-voice-token", b"bad\x00token"),
    (b"origin", b"http://127.0.0.1:8410\n"),
    (b"host", b"127.0.0.1:8410\r"),
])
def test_non_ascii_or_control_header_values_fail_closed(client, name, value):
    assert client.get("/voice/sessions", headers=[(name, value)]).status_code == 400


@pytest.mark.parametrize("path,body", [
    ("/voice/cancel", []),
    ("/voice/cancel", None),
    ("/voice/tools/execute", []),
    ("/voice/tools/execute", {"name": "cancel_current_task", "arguments": []}),
])
def test_invalid_control_shapes_are_rejected(client, path, body):
    assert client.post(path, json=body).status_code == 400


@pytest.mark.parametrize("origin", [
    "", "null", "file://127.0.0.1:8410", "ftp://127.0.0.1:8410",
    ORIGIN + "/", ORIGIN + "/path", ORIGIN + "?", ORIGIN + "#",
    "http://user@127.0.0.1:8410", "http://127.0.0.1:",
    "http://127.0.0.1:0", "http://127.0.0.1:65536",
    "http://127.0.0.1:abc", "http://[::1", "http://%31%32%37.0.0.1:8410",
    ORIGIN + ", " + ORIGIN, ORIGIN + " " + ORIGIN,
    " " + ORIGIN, ORIGIN + "\t", "http://127.0.0.1:8410\\",
])
def test_malformed_origins_fail_closed(unauthenticated_client, origin):
    response = unauthenticated_client.get("/voice/", headers={"Origin": origin})
    assert response.status_code in (400, 403)
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("host", [
    "", "127.0.0.1:8410,attacker.example", "user@127.0.0.1:8410",
    "127.0.0.1:8410/path", "127.0.0.1:", "127.0.0.1:8410?",
    "127.0.0.1:8410#", "127.0.0.1:8410 ", "[::1", "127.0.0.1:99999",
])
def test_malformed_hosts_fail_closed(unauthenticated_client, host):
    response = unauthenticated_client.get("/voice/", headers={"Host": host})
    assert response.status_code == 403


def test_missing_host_rejected(app):
    # TestClient itself synthesizes Host. Remove it at the ASGI boundary.
    async def without_host(scope, receive, send):
        if scope["type"] == "http":
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"host"]
        await app(scope, receive, send)

    with TestClient(without_host, base_url=ORIGIN) as client:
        assert client.get("/voice/").status_code == 403


@pytest.mark.parametrize("value", [
    "", "*", "https://*.example", "https://example/", "https://example?",
    "https://example#", "https://user@example", "https://example:",
    "https://example:0", "https://example:65536", "null",
])
def test_invalid_server_config_cannot_build_router(monkeypatch, state, value):
    monkeypatch.setenv("VOICE_PLUGIN_PUBLIC_ORIGIN", value)
    with pytest.raises(ValueError):
        create_router(state)


@pytest.mark.parametrize("configured,host,origin", [
    ("https://voice.example", "VOICE.EXAMPLE:443", "https://VOICE.EXAMPLE:443"),
    ("http://voice.example:80", "voice.example", "http://voice.example"),
    ("http://[::1]:8410", "[0:0:0:0:0:0:0:1]:8410", "http://[::1]:8410"),
])
def test_normalized_origins_and_proxy_transport(
    state, tmp_path, monkeypatch, configured, host, origin
):
    monkeypatch.setenv("VOICE_PLUGIN_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_PLUGIN_PUBLIC_ORIGIN", configured)
    app = FastAPI()
    app.include_router(create_router(state))
    # Backend transport intentionally HTTP even for HTTPS public configuration.
    with TestClient(app, base_url="http://backend") as client:
        response = client.get("/voice/", headers={"Host": host, "Origin": origin})
        assert response.status_code == 200
        assert ("Secure" in response.headers["set-cookie"]) == configured.startswith("https:")
        assert client.get("/voice/sessions", headers={
            **TOKEN, "Host": host, "Origin": origin,
        }).status_code == 200
        wrong_scheme = origin.replace("https:", "http:") if origin.startswith("https:") else origin.replace("http:", "https:")
        assert client.get("/voice/", headers={"Host": host, "Origin": wrong_scheme}).status_code == 403


def test_origin_config_is_snapshotted_and_forwarded_headers_do_not_authorize(
    unauthenticated_client, monkeypatch
):
    monkeypatch.setenv("VOICE_PLUGIN_PUBLIC_ORIGIN", "https://attacker.example")
    response = unauthenticated_client.get("/voice/", headers={
        "Host": "attacker.example",
        "Forwarded": 'host="127.0.0.1:8410";proto=http',
        "X-Forwarded-Host": "127.0.0.1:8410",
        "X-Forwarded-Proto": "http",
    })
    assert response.status_code == 403
    response = unauthenticated_client.get("/voice/", headers={
        "Forwarded": "host=attacker.example;proto=https",
        "X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "https",
    })
    assert response.status_code == 200
    assert "Secure" not in response.headers["set-cookie"]


def test_canonical_default_ports():
    assert canonical_origin("https://EXAMPLE.com:443") == "https://example.com"
    assert canonical_origin("http://EXAMPLE.com:80") == "http://example.com"


@pytest.mark.parametrize("group,path", [
    ("static", "/voice/"),
    ("signaling", "/voice/session"),
    ("session", "/voice/sessions"),
    ("tool", "/voice/cancel"),
])
def test_individually_mounted_route_groups_keep_boundary(state, tmp_path, group, path):
    settings = VoicePluginSettings(home_dir=tmp_path)
    router = {
        "static": lambda: routes.create_static_routes(settings),
        "signaling": lambda: routes.create_signaling_routes(settings),
        "session": lambda: routes.create_session_routes(state, settings, None),
        "tool": lambda: routes.create_tool_routes(state, settings),
    }[group]()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://attacker.example:8410") as client:
        method = "POST" if group == "tool" else "GET"
        assert client.request(method, path, headers=TOKEN).status_code == 403


def test_cookie_cannot_authorize_control_and_wrong_header_cannot_fallback(
    unauthenticated_client
):
    page = unauthenticated_client.get("/voice/")
    assert page.status_code == 200
    assert unauthenticated_client.cookies["voice_token"] == routes._VOICE_CAPABILITY_TOKEN
    assert unauthenticated_client.post("/voice/sessions", json={}).status_code == 401
    assert unauthenticated_client.get("/voice/events", headers={"X-Voice-Token": "wrong"}).status_code == 401
    assert unauthenticated_client.get("/voice/events", headers={"X-Voice-Token": ""}).status_code == 401
    cookie = "voice_token=" + routes._VOICE_CAPABILITY_TOKEN
    assert unauthenticated_client.get("/voice/events", headers={"Cookie": cookie + "; " + cookie}).status_code == 400
    assert unauthenticated_client.get("/voice/events", headers={"Cookie": "voice_token=wrong"}).status_code == 401


def test_sse_cookie_lifecycle_and_restart(app, unauthenticated_client, monkeypatch):
    page = unauthenticated_client.get("/voice/")
    cookie = page.headers["set-cookie"].split(";", 1)[0]

    async def read_one_event():
        conn = SimpleNamespace(event_queue=asyncio.Queue())
        await conn.event_queue.put({"type": "test_event", "data": "synthetic"})
        monkeypatch.setattr(routes, "_active_connection", conn)
        messages = []
        disconnected = asyncio.Event()

        async def receive():
            # Yield while waiting; older Starlette has a concurrent disconnect
            # listener, newer versions rely on request.is_disconnected().
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body":
                disconnected.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/voice/events", "raw_path": b"/voice/events", "query_string": b"",
            "root_path": "", "server": ("127.0.0.1", 8410), "client": ("127.0.0.1", 1),
            "headers": [(b"host", b"127.0.0.1:8410"), (b"cookie", cookie.encode())],
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=2)
        assert messages[0]["status"] == 200
        assert b'"type": "test_event"' in messages[1]["body"]

    asyncio.run(read_one_event())
    # Simulates token rotation; does not claim a real process/browser restart.
    monkeypatch.setattr(routes, "_VOICE_CAPABILITY_TOKEN", "new-test-process-token")
    assert unauthenticated_client.get("/voice/events").status_code == 401
    assert unauthenticated_client.get("/voice/sessions", headers=TOKEN).status_code == 401
    fresh = unauthenticated_client.get("/voice/")
    assert "new-test-process-token" in fresh.text
    assert unauthenticated_client.get("/voice/sessions", headers={
        "X-Voice-Token": "new-test-process-token",
    }).status_code == 200


@pytest.mark.parametrize("path,name", [
    ("/voice/cancel", None),
    ("/voice/tools/execute", "cancel_current_task"),
    ("/voice/tools/execute", "delegate"),
])
@pytest.mark.parametrize("session_id", [None, "", "stale-voice", "unrelated-daemon", 7])
def test_control_cannot_target_other_sessions(
    client, state, monkeypatch, path, name, session_id
):
    class NeverManager:
        def get(self, session_id):
            raise AssertionError("Must not look up arbitrary daemon sessions")

    class Connection:
        session_id = "active-voice"

        async def cancel(self, **kwargs):
            raise AssertionError("Must not cancel another session")

        async def execute(self, instruction):
            raise AssertionError("Must not execute on another session")

    state.session_manager = NeverManager()
    monkeypatch.setattr(routes, "_active_connection", Connection())
    response = client.post(path, json={
        "session_id": session_id, "name": name, "level": "immediate",
        "arguments": {"instruction": "synthetic"},
    })
    assert response.status_code == 403


@pytest.mark.parametrize("level", ["graceful", "immediate"])
def test_cancel_uses_actual_active_handle(client, state, monkeypatch, level):
    calls = []

    class Handle:
        async def cancel(self, immediate=False):
            calls.append(immediate)

    class NeverManager:
        def get(self, session_id):
            raise AssertionError("Active connection owns the only authorized handle")

    state.session_manager = NeverManager()
    conn = VoiceConnection(None, state.session_manager, None)
    conn._session_id = "active"
    conn._handle = Handle()
    monkeypatch.setattr(routes, "_active_connection", conn)
    assert client.post("/voice/cancel", json={
        "session_id": "active", "level": level,
    }).status_code == 200
    assert calls == [level == "immediate"]
    assert client.post("/voice/tools/execute", json={
        "name": "cancel_current_task", "session_id": "active",
    }).status_code == 200
    assert calls == [level == "immediate", False]
    monkeypatch.setattr(routes, "_active_connection", None)
    assert client.post("/voice/cancel", json={"session_id": "active"}).status_code == 400
    assert client.post("/voice/tools/execute", json={
        "name": "cancel_current_task", "session_id": "active",
    }).status_code == 400
    assert len(calls) == 2


def test_resume_cannot_promote_unrelated_daemon_handle(client, state, monkeypatch):
    from voice_plugin import realtime

    async def never(*args):
        raise AssertionError("Unknown voice ID must be rejected before external work")

    state.session_manager = SimpleNamespace(resume=never)
    monkeypatch.setattr(realtime, "create_client_secret", never)
    assert client.post("/voice/sessions/unrelated-daemon/resume").status_code == 404
    assert routes._active_connection is None


def test_voice_only_resume_fails_without_daemon_before_provider(client, monkeypatch):
    from voice_plugin import realtime

    async def never(*args):
        raise AssertionError("No daemon available; must not request a provider token")

    monkeypatch.setattr(realtime, "create_client_secret", never)
    session_id = client.post("/voice/sessions", json={}).json()["session_id"]
    assert client.post(f"/voice/sessions/{session_id}/resume").status_code == 503


def test_browser_shaped_session_lifecycle(app, state, monkeypatch, tmp_path):
    """Real routes/VoiceConnection/repository, fake daemon handles and OpenAI."""
    from voice_plugin import realtime

    calls = []

    class Handle:
        session_id = "lifecycle-session"

        async def execute(self, prompt):
            calls.append(("execute", prompt))
            return "synthetic result"

        async def cancel(self, immediate=False):
            calls.append(("cancel", immediate))

        async def cleanup(self):
            calls.append(("cleanup",))

    class Manager:
        async def create(self, **kwargs):
            assert kwargs["working_dir"] == str(tmp_path / "workspace")
            return Handle()

        async def resume(self, session_id):
            assert session_id == Handle.session_id
            calls.append(("resume", session_id))
            return Handle()

        def get(self, session_id):
            raise AssertionError("No arbitrary handle lookup")

    class Bus:
        async def subscribe(self, session_id):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    async def secret(config):
        assert not config.openai_api_key
        return {"client_secret": {"value": "synthetic-ephemeral"}}

    async def sdp(offer, token, model):
        assert token == "synthetic-ephemeral"
        return {"sdp": "synthetic-answer"}

    monkeypatch.setenv("AMPLIFIER_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(realtime, "create_client_secret", secret)
    monkeypatch.setattr(realtime, "exchange_sdp", sdp)
    state.session_manager = Manager()
    state.event_bus = Bus()
    monkeypatch.setattr(state.settings, "default_bundle", "synthetic-bundle", raising=False)
    with TestClient(app, base_url=ORIGIN) as client:
        page = client.get("/voice/")
        token = re.search(r'<meta name="voice-token" content="([^"]+)">', page.text)[1]
        client.headers.update({"X-Voice-Token": token, "Origin": ORIGIN})
        assert client.get("/voice/session").status_code == 200
        assert client.post("/voice/sdp", content="synthetic-offer", headers={
            "Authorization": "Bearer synthetic-ephemeral",
        }).text == "synthetic-answer"
        session_id = client.post("/voice/sessions", json={}).json()["session_id"]
        assert session_id == Handle.session_id
        prefix = f"/voice/sessions/{session_id}"
        assert client.post(prefix + "/transcript", json={
            "entries": [{"role": "user", "content": "synthetic transcript"}],
        }).json() == {"synced": 1}
        assert client.get("/voice/sessions").json()[0]["id"] == session_id
        assert client.post("/voice/tools/execute", json={
            "name": "delegate", "session_id": session_id,
            "arguments": {"instruction": "synthetic instruction"},
        }).json() == {"result": "synthetic result"}
        assert client.post("/voice/cancel", json={
            "session_id": session_id, "level": "immediate",
        }).status_code == 200
        # Model a disconnect with the actual connection teardown, on its loop.
        # There is no real WebRTC/browser disconnect in this test.
        client.portal.call(routes._active_connection.teardown)
        resumed = client.post(prefix + "/resume")
        assert resumed.status_code == 200
        assert resumed.json()["client_secret"] == "synthetic-ephemeral"
        assert resumed.json()["context_to_inject"]
        assert client.post("/voice/tools/execute", json={
            "name": "cancel_current_task", "session_id": session_id,
        }).status_code == 200
        assert client.post(prefix + "/end", json={}).status_code == 200
        assert client.post("/voice/cancel", json={"session_id": session_id}).status_code == 400
    assert calls == [
        ("execute", "synthetic instruction"), ("cancel", True),
        ("resume", session_id), ("cancel", False), ("cleanup",),
    ]