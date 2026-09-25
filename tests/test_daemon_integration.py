"""Optional amplifierd loader/entry-point integration; no live daemon or models.

Run with the standalone/dev extra or an inspected amplifierd source on PYTHONPATH.
Only the real discovery/settings modules are used, never app startup/prewarm.
"""

import importlib.metadata
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

plugins = pytest.importorskip("amplifierd.plugins")


@pytest.fixture
def entry_point(monkeypatch, tmp_path):
    metadata = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )
    target = metadata["project"]["entry-points"]["amplifierd.plugins"]["voice"]
    ep = importlib.metadata.EntryPoint(
        name="voice", value=target, group="amplifierd.plugins"
    )
    monkeypatch.setattr(plugins, "_get_entry_points", lambda: [ep])
    monkeypatch.setenv("VOICE_PLUGIN_HOME_DIR", str(tmp_path / "voice"))
    return ep


def test_real_discovery_mounts_protected_router(entry_point, state):
    loaded = plugins.discover_plugins(disabled=[], state=state)
    assert [name for name, _ in loaded] == ["voice"]
    app = FastAPI()
    for _, router in loaded:
        app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8410") as client:
        assert client.get("/voice/").status_code == 200
        assert client.get("/voice/", headers={"Host": "attacker.example:8410"}).status_code == 403
        assert client.get("/voice/sessions").status_code == 401
    assert plugins.discover_plugins(disabled=["voice"], state=state) == []


def test_real_discovery_fails_closed_on_invalid_config(entry_point, state, monkeypatch):
    monkeypatch.setenv("VOICE_PLUGIN_PUBLIC_ORIGIN", "*")
    assert plugins.discover_plugins(disabled=[], state=state) == []


def test_standalone_cli_targets_real_daemon_factory(entry_point, monkeypatch, tmp_path):
    from amplifierd import daemon_session
    from click.testing import CliRunner

    from voice_plugin.cli import main

    runs = []
    # Intercept the server launch; no listener, startup/prewarm or logging changes.
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(
        run=lambda *args, **kwargs: runs.append((args, kwargs))
    ))
    monkeypatch.setattr(daemon_session, "create_session_dir", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(daemon_session, "setup_session_log", lambda *a: None)
    monkeypatch.setenv("AMPLIFIERD_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_PLUGIN_PUBLIC_ORIGIN", "http://127.0.0.1:9000")
    result = CliRunner().invoke(main, ["--port", "9000"])
    assert result.exit_code == 0, result.output
    assert runs == [(("amplifierd.app:create_app",), {
        "host": "127.0.0.1", "port": 9000, "reload": False,
        "log_level": "info", "factory": True,
    })]


def test_dev_entry_point_mounts_boundary(entry_point, monkeypatch):
    from voice_plugin.__main__ import main

    runs = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(
        run=lambda app, **kwargs: runs.append(app)
    ))
    monkeypatch.setattr(sys, "argv", ["voice_plugin"])
    main()
    assert len(runs) == 1
    with TestClient(runs[0], base_url="http://127.0.0.1:8410") as client:
        assert client.get("/voice/").status_code == 200
        assert client.get("/voice/", headers={"Host": "attacker.example:8410"}).status_code == 403