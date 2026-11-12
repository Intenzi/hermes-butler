"""Contract tests for the Butler dashboard API router."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import plugin_api  # noqa: E402  (import after sys.path setup)
from butler_core import engine, paths as bp_paths  # noqa: E402
from butler_core.summon import frame_look_text, frame_summon_text  # noqa: E402

router = plugin_api.router


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    assert body["ok"] is True


def test_state_without_state_file_is_empty_dict(client: TestClient) -> None:
    response = client.get("/state")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    assert body == {}


def test_state_reads_plugin_data_state_file(client: TestClient, tmp_path: Path) -> None:
    state = {
        "guide": {
            "id": "guide-1",
            "title": "Find the setting",
            "state": {"idx": 0, "status": "running", "say": "Open Settings"},
            "steps": [{"say": "Open Settings", "find": {"label": "Settings"}}],
            "verifications": [],
        },
        "overlay": "ringing",
    }
    path = tmp_path / "plugin-data" / "butler" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state), encoding="utf-8")

    response = client.get("/state")

    assert response.status_code == 200
    assert response.json() == state


def test_state_ignores_unreadable_state_file(client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "plugin-data" / "butler" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    response = client.get("/state")

    assert response.status_code == 200
    assert response.json() == {}


def test_history_shape_sorting_and_cap(client: TestClient, tmp_path: Path) -> None:
    guides = tmp_path / "plugin-data" / "butler" / "guides"
    guides.mkdir(parents=True)
    for i in range(55):
        guide = {
            "id": f"g{i:02d}",
            "title": f"Guide {i}",
            "state": {"status": "done" if i % 2 == 0 else "stopped"},
            "steps": [{"say": f"Step {i}.1"}, {"say": f"Step {i}.2"}],
            "verifications": [{"step": 0, "reason": "test"}],
            "started_at": 1000.0 + i,
        }
        (guides / f"g{i:02d}.json").write_text(json.dumps(guide), encoding="utf-8")
    # A malformed file must be skipped, not break the endpoint.
    (guides / "broken.json").write_text("{not json", encoding="utf-8")

    response = client.get("/history")

    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list)
    assert len(rows) == 50
    assert [row["id"] for row in rows[:3]] == ["g54", "g53", "g52"]
    assert rows[0]["started_at"] == 1054.0
    assert rows[0]["title"] == "Guide 54"
    assert rows[0]["status"] == "done"
    assert rows[0]["steps"] == [{"say": "Step 54.1"}, {"say": "Step 54.2"}]
    assert rows[0]["verifications"] == [{"step": 0, "reason": "test"}]
    assert set(rows[0]) == {
        "id", "title", "status", "steps", "verifications", "started_at"
    }


def test_history_ignores_the_legacy_install_dir_fallback(
        client: TestClient, tmp_path: Path) -> None:
    """History and config now share plugin-data; the install dir cannot arm it."""
    guides = tmp_path / "plugins" / "butler" / "data" / "guides"
    guides.mkdir(parents=True)
    guide = {
        "id": "legacy",
        "title": "Legacy guide",
        "state": {"status": "done"},
        "steps": [],
        "verifications": [],
        "started_at": 12.0,
    }
    (guides / "legacy.json").write_text(json.dumps(guide), encoding="utf-8")

    response = client.get("/history")

    assert response.status_code == 200
    assert response.json() == []


def test_config_defaults_pet_click_claim_on(client: TestClient) -> None:
    response = client.get("/config")

    assert response.status_code == 200
    assert response.json() == {"summon": {"pet_click_claim": True}}


def test_explicit_false_in_config_still_disarms_the_chip(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "plugin-data" / "butler" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"summon": {"pet_click_claim": False}}), encoding="utf-8")

    assert client.get("/config").json() == {"summon": {"pet_click_claim": False}}

    calls = []

    def fake_sync(enabled):
        calls.append(enabled)
        return {"enabled": bool(enabled), "running": False}

    monkeypatch.setattr(plugin_api, "_sync_claim_chip", fake_sync)
    assert client.post("/summon/claim", json={}).json()["enabled"] is False
    assert calls == [False]


def test_config_write_persists_and_hot_reloads(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/config", json={"summon": {"pet_click_claim": True}})

    assert response.status_code == 200
    assert response.json() == {"summon": {"pet_click_claim": True}}
    path = tmp_path / "plugin-data" / "butler" / "config.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["summon"]["pet_click_claim"] is True
    # The same GET reads the file fresh (no restart).
    assert client.get("/config").json() == {"summon": {"pet_click_claim": True}}

    assert client.post("/config", json={"pet_click_claim": False}).json() == {
        "summon": {"pet_click_claim": False}
    }
    assert json.loads(path.read_text(encoding="utf-8"))["summon"]["pet_click_claim"] is False


def test_dashboard_and_engine_share_one_config_file(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher's /config and the engine's _cfg must round-trip one file."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    canonical = tmp_path / "plugin-data" / "butler" / "config.json"

    assert plugin_api._config_candidates() == [bp_paths.config_path()]
    assert plugin_api._state_candidates() == [bp_paths.state_path()]
    assert plugin_api._config_path() == canonical

    assert client.post("/config", json={"summon": {"pet_click_claim": True}}).status_code == 200
    engine.init(canonical.parent)
    assert canonical.exists()
    assert engine.pet_click_claim() is True

    # A stale copy in the old install-dir fallback must not win (or arm the
    # receiver while the canonical file says off).
    legacy = tmp_path / "plugins" / "butler" / "data" / "config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"summon": {"pet_click_claim": True}}), encoding="utf-8")
    assert client.post("/config", json={"summon": {"pet_click_claim": False}}).status_code == 200
    assert engine.pet_click_claim() is False


def test_state_and_guides_read_the_same_plugin_data_dir(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = {"guide": {"id": "same-file", "state": {"status": "running"}}}
    path = bp_paths.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")

    assert plugin_api._state_candidates() == [path]
    assert plugin_api._guides_candidates() == [path.parent / "guides"]
    assert client.get("/state").json() == state


def test_watch_stop_endpoint_stops_the_watch_and_clears_the_row(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pane's Stop button posts here; it must reach engine.stop_watch()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    engine.init(bp_paths.plugin_data_dir("butler"))

    class FakeOverlay:
        sends = []

        def is_running(self):
            return False

    monkeypatch.setattr(engine, "_OVERLAY", FakeOverlay())
    engine._watch = {
        "app": "Slack", "until": None, "token": "tok-1",
        "owner_pid": os.getpid(), "started_at": time.time(),
        "events": 3, "hit": None, "stopped": None, "recent": [],
    }
    engine._write_state()
    assert client.get("/state").json()["watch"]["app"] == "Slack"

    response = client.post("/watch/stop")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "stopped"
    assert body["app"] == "Slack"
    assert body["events"] == 3
    # The row is cleared from the same state file the pane polls.
    assert "watch" not in client.get("/state").json()


def test_watch_stop_endpoint_is_honest_when_no_watch_exists(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    engine.init(bp_paths.plugin_data_dir("butler"))

    response = client.post("/watch/stop")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "idle"}


def test_watch_ack_endpoint_clears_the_hit_and_keeps_the_watch(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sticky watch's "thank you": clear the hit, keep observing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    engine.init(bp_paths.plugin_data_dir("butler"))
    engine._watch = None
    engine._watch_ring.clear()

    class FakeOverlay:
        sends = []

        def is_running(self):
            return False

    monkeypatch.setattr(engine, "_OVERLAY", FakeOverlay())
    engine._watch = {
        "app": "Slack", "until": "Export complete", "once": False,
        "token": "tok-1", "owner_pid": os.getpid(), "started_at": time.time(),
        "events": 5, "hit": {"label": "Export complete", "window": None,
                             "at": time.time(), "acknowledged": False},
        "stopped": None, "recent": [],
    }
    engine._write_state()
    assert client.get("/state").json()["watch"]["hit"]["label"] == "Export complete"

    response = client.post("/watch/ack")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "status": "acknowledged", "acknowledged": True,
        "app": "Slack", "events": 5,
    }
    state = client.get("/state").json()
    assert state["watch"]["hit"] is None, "the hit is cleared"
    assert state["watch"]["stopped"] is None, "the watch still runs"
    assert state["watch"]["app"] == "Slack"

    # A second ack is an honest false, not an error.
    again = client.post("/watch/ack")
    assert again.status_code == 200
    assert again.json()["acknowledged"] is False
    assert again.json()["status"] == "no-hit"

    # The pane's Stop still fully stops (and clears) the same row.
    stopped = client.post("/watch/stop")
    assert stopped.json()["status"] == "stopped"
    assert "watch" not in client.get("/state").json()


def test_watch_ack_endpoint_is_honest_when_no_watch_exists(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    engine.init(bp_paths.plugin_data_dir("butler"))
    engine._watch = None
    engine._watch_ring.clear()

    response = client.post("/watch/ack")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "idle",
                               "acknowledged": False}


def test_watch_ack_endpoint_clears_the_outliving_hit_with_no_watch(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-shot hit leaves no watch row; the announcement is the only row."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    engine.init(bp_paths.plugin_data_dir("butler"))
    engine._watch = None
    engine._watch_ring.clear()
    state_path = tmp_path / "plugin-data" / "butler" / "state.json"
    block = {
        "app": "Slack", "label": "Export complete",
        "window": "general — Slack", "at": time.time(),
        "acknowledged": False, "id": "a1b2c3d4",
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"watch_hit": block}), encoding="utf-8")

    # /state exposes the block the pet speaks.
    assert client.get("/state").json()["watch_hit"] == block

    response = client.post("/watch/ack")

    assert response.status_code == 200
    assert response.json()["acknowledged"] is True
    assert response.json()["status"] == "acknowledged"
    assert response.json()["app"] == "Slack"
    assert "watch_hit" not in client.get("/state").json(), "the block is removed"

    # A second ack is a no-op, never an exception.
    again = client.post("/watch/ack")
    assert again.status_code == 200
    assert again.json() == {"ok": True, "status": "idle", "acknowledged": False}


def test_the_hat_menu_watch_ack_route_reaches_the_engine(
        client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """overlay.py routes the hat's {"op":"watch_ack"} to engine.ack_watch()."""
    from butler_core import overlay as bp_overlay

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    engine.init(bp_paths.plugin_data_dir("butler"))
    engine._watch = None
    engine._watch_ring.clear()
    block = {"app": "Slack", "label": "Export complete", "window": None,
             "at": time.time(), "acknowledged": False, "id": "a1b2c3d4"}
    path = tmp_path / "plugin-data" / "butler" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"watch_hit": block}), encoding="utf-8")
    journaled = []
    monkeypatch.setattr(bp_overlay, "journal_click", journaled.append)

    controller = bp_overlay.Overlay()
    controller._handle_message({"op": "watch_ack"})

    assert journaled == ["watch-ack:menu"]
    assert "watch_hit" not in client.get("/state").json()


def test_summon_pet_is_a_noop_without_a_petdex_pet(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    (Path(os.environ["HERMES_HOME"]) / "pets").mkdir()

    response = client.post("/summon/pet")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "pet": False}


class FakePluginCtx:
    def __init__(self, accepted=True, raises=False):
        self.accepted = accepted
        self.raises = raises
        self.injected = []

    def inject_message(self, text, **kwargs):
        self.injected.append({"text": text, **kwargs})
        if self.raises:
            raise RuntimeError("injection host is down")
        return self.accepted


def test_summon_send_routes_by_the_desktop_chat_id(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The host owns the session-key rules: the desktop's durable chat id is
    # handed straight to the supported injection seam, never re-derived here.
    ctx = FakePluginCtx()
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "turn off wifi",
        "session_id": "local-ec39b9cb",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session": "local-ec39b9cb"}
    assert ctx.injected == [{
        "text": frame_summon_text("turn off wifi"),
        "role": "user",
        "session_key": "local-ec39b9cb",
    }]


def test_summon_send_reports_false_as_unroutable_without_raising(
        client: TestClient, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    ctx = FakePluginCtx(accepted=False)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "fix it",
        "session_id": "ses_123",
    })

    assert response.status_code == 200
    assert response.json() == {
        "error": "unroutable",
        "hint": "the desktop session could not be reached; the answer stays in chat",
    }
    assert len(ctx.injected) == 1
    # The host's consent flag is the actionable cause; the journal must name it.
    assert any("allow_gateway_injection" in record.message
               for record in caplog.records)


def test_summon_send_treats_a_raising_gateway_as_unroutable(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakePluginCtx(raises=True)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "fix it",
        "session_id": "ses_123",
    })

    assert response.status_code == 200
    assert response.json() == {
        "error": "unroutable",
        "hint": "the desktop session could not be reached; the answer stays in chat",
    }


def test_plugin_api_has_no_gateway_private_names() -> None:
    source = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")

    for private in ("_manager", "_published_gateway_message_injector",
                    "_generate_session_key"):
        assert private not in source, f"gateway internals leak: {private}"


def test_summon_send_injects_the_exact_frame_into_the_selected_session(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakePluginCtx()
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "turn off wifi",
        "session_id": "ses_123",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session": "ses_123"}
    assert ctx.injected == [{
        "text": frame_summon_text("turn off wifi"),
        "role": "user",
        "session_key": "ses_123",
    }]
    assert ctx.injected[0]["text"].startswith("[butler summon] turn off wifi\n\n")


def test_summon_send_fails_closed_without_a_live_plugin_context(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: None)

    response = client.post("/summon/send", json={"text": "fix it", "session_id": "ses_123"})

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "injection_denied"}


def test_summon_send_requires_text_and_a_session_id(client: TestClient) -> None:
    assert client.post("/summon/send", json={"text": "", "session_id": "ses_123"}).status_code == 400
    assert client.post("/summon/send", json={"text": "fix it", "session_id": ""}).status_code == 400


def test_summon_claim_endpoint_toggles_the_companion_chip(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_sync(enabled):
        calls.append(enabled)
        return {"enabled": bool(enabled), "running": bool(enabled)}

    monkeypatch.setattr(plugin_api, "_sync_claim_chip", fake_sync)

    assert client.post("/summon/claim", json={"enabled": True}).json() == {
        "ok": True, "enabled": True, "running": True
    }
    assert client.post("/summon/claim", json={"enabled": False}).json() == {
        "ok": True, "enabled": False, "running": False
    }
    assert calls == [True, False]


def test_summon_claim_defaults_to_armed_and_runs_the_engine_arm_path(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """No key anywhere means on: /summon/claim{} must reach set_claim_chip(True)."""
    armed: list[bool] = []

    def fake_set_claim_chip(enabled):
        armed.append(enabled)
        return {"enabled": bool(enabled), "running": True}

    monkeypatch.setattr(engine, "set_claim_chip", fake_set_claim_chip)
    monkeypatch.setattr(plugin_api, "_CLAIM_SYNCING", False)

    response = client.post("/summon/claim", json={})

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    # Arming runs off the request thread; wait for that short-lived worker.
    deadline = time.time() + 2.0
    while (not armed or plugin_api._CLAIM_SYNCING) and time.time() < deadline:
        time.sleep(0.01)
    assert armed == [True]


def test_summon_ack_consumes_only_the_named_pending_row(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    consumed = []

    def fake_consume(pending_id):
        consumed.append(pending_id)
        return pending_id == "abc123"

    monkeypatch.setattr(plugin_api, "_engine_module",
                        lambda: SimpleNamespace(consume_summon_pending=fake_consume))

    assert client.post("/summon/ack", json={"id": "abc123"}).json() == {
        "ok": True, "cleared": True
    }
    assert client.post("/summon/ack", json={"id": "other"}).json() == {
        "ok": True, "cleared": False
    }
    assert consumed == ["abc123", "other"]


def test_engine_module_initialises_the_process_that_owns_the_chip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin API process owns the companion, so it needs engine.init().

    register() runs in the agent runtime; without an init here the chip arms
    fine and every claim_click is dropped (no data dir, no on_event handler).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "DATA_DIR", None)
    monkeypatch.setattr(engine, "_OVERLAY", engine.bp_overlay.Overlay())

    loaded = plugin_api._engine_module()

    assert loaded is engine
    assert engine.DATA_DIR is not None
    assert callable(engine._OVERLAY.on_event)
    # Idempotent: a second call must not clobber an existing init.
    first = engine.DATA_DIR
    plugin_api._engine_module()
    assert engine.DATA_DIR == first


def test_cli_handler_prints_because_the_host_ignores_returned_strings() -> None:
    """`hermes butler doctor` printed nothing on a fresh install.

    The host calls ``args.func(args)`` and only honours an INT return code; a
    handler that returns its payload is silently discarded. Pin that the CLI
    handler prints and returns None.
    """
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    start = source.find("def butler_cli_command(")
    end = source.find("def cli_butler(", start)
    assert start != -1 and end != -1
    body = source[start:end]

    assert "print(" in body, "the payload must be printed, not returned"
    assert "return _json.dumps(" not in body, "a returned string is discarded"
    assert "return None" in body


def test_ask_field_spool_becomes_the_state_row_and_the_ack_clears_it(
        client: TestClient, tmp_path: Path) -> None:
    """The companion's ask field is a file spool, not a pipe event.

    A stale stdout handle once made every typed request vanish with no error, so
    /state merges the newest spooled row and /summon/ack removes exactly that
    row. This is the contract the desktop polls.
    """
    spool = bp_paths.plugin_data_dir("butler") / "summon-spool.jsonl"
    spool.write_text(
        '{"op":"summon_text","text":"older","id":"aaaa1111"}\n'
        '{"op":"summon_text","text":"turn off dnd","id":"bbbb2222"}\n',
        encoding="utf-8",
    )

    state = client.get("/state").json()
    assert state["summon_pending"]["id"] == "bbbb2222"
    assert state["summon_pending"]["text"] == "turn off dnd"
    assert state["summon_pending"]["origin"] == "chip"

    assert client.post("/summon/ack", json={"id": "bbbb2222"}).json()["cleared"] is True
    remaining = spool.read_text(encoding="utf-8").splitlines()
    assert len(remaining) == 1
    assert "aaaa1111" in remaining[0]
    # The older row is now the newest one and stays visible until acked.
    assert client.get("/state").json()["summon_pending"]["text"] == "older"


def test_a_row_with_blank_text_never_surfaces(client: TestClient) -> None:
    spool = bp_paths.plugin_data_dir("butler") / "summon-spool.jsonl"
    spool.write_text('{"op":"summon_text","text":"   ","id":"cccc3333"}\n', encoding="utf-8")
    assert "summon_pending" not in client.get("/state").json()


def test_the_spooled_destination_survives_the_state_row(client: TestClient) -> None:
    """A row read from the file must behave exactly like the pipe event."""
    spool = bp_paths.plugin_data_dir("butler") / "summon-spool.jsonl"
    spool.write_text(
        '{"op":"summon_text","text":"old","id":"dddd4444","destination":"recent"}\n'
        '{"op":"summon_text","text":"fresh chat","id":"eeee5555","destination":"new"}\n',
        encoding="utf-8",
    )

    assert client.get("/state").json()["summon_pending"] == {
        "origin": "chip",
        "id": "eeee5555",
        "text": "fresh chat",
        "destination": "new",
        "at": None,
    }

    # Missing or unknown values collapse to the safe default.
    spool.write_text('{"op":"summon_text","text":"default","id":"ffff6666"}\n',
                     encoding="utf-8")
    assert client.get("/state").json()["summon_pending"]["destination"] == "recent"
    spool.write_text(
        '{"op":"summon_text","text":"bogus","id":"gggg7777","destination":"cabinet"}\n',
        encoding="utf-8",
    )
    assert client.get("/state").json()["summon_pending"]["destination"] == "recent"


def test_a_look_spool_row_keeps_its_origin_and_target(
        client: TestClient) -> None:
    """The ⌘⇧L row is the same spool contract plus the hit-tested element."""
    spool = bp_paths.plugin_data_dir("butler") / "summon-spool.jsonl"
    target = {
        "app": "Preview",
        "role": "Button",
        "label": "Export",
        "window": "receipt.pdf",
        "doc": "file:///tmp/receipt.pdf",
        "enabled": True,
        "box": [10, 20, 30, 40],
    }
    spool.write_text(
        json.dumps({
            "op": "look_text",
            "origin": "look",
            "text": "what does this export?",
            "id": "look1234",
            "destination": "recent",
            "target": target,
        }) + "\n",
        encoding="utf-8",
    )

    pending = client.get("/state").json()["summon_pending"]
    assert pending["origin"] == "look"
    assert pending["id"] == "look1234"
    assert pending["text"] == "what does this export?"
    assert pending["target"] == target
    assert pending["destination"] == "recent"

    # Acking removes exactly that row, same as the ask field.
    assert client.post("/summon/ack", json={"id": "look1234"}).json()["cleared"] is True
    assert "summon_pending" not in client.get("/state").json()

    # The companion's actual wire line carries no origin key; the op is enough
    # to keep the row a LOOK row instead of mislabelling it as a chip send.
    spool.write_text(
        json.dumps({
            "op": "look_text",
            "text": "and this?",
            "id": "look5678",
            "target": target,
        }) + "\n",
        encoding="utf-8",
    )
    inferred = client.get("/state").json()["summon_pending"]
    assert inferred["origin"] == "look"
    assert inferred["target"] == target


LOOK_ROW_TEXT = (
    '[butler look] App: Preview · Window: receipt.pdf · '
    'Control: Button "Export"\nQuestion: what does this export?'
)


def test_summon_send_frames_a_look_row_with_the_butler_say_instruction(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A LOOK turn must ask for the on-screen answer, not just the chat one."""
    ctx = FakePluginCtx()
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": LOOK_ROW_TEXT,
        "session_id": "ses_123",
        "origin": "look",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session": "ses_123"}
    injected = ctx.injected[0]["text"]
    assert injected == frame_look_text(LOOK_ROW_TEXT)
    # One header, one question line, then the answer instruction.
    assert injected.startswith(
        '[butler look] App: Preview · Window: receipt.pdf · '
        'Control: Button "Export"\nQuestion: what does this export?\n\n'
    )
    assert injected.count("[butler look]") == 1
    assert "butler_say" in injected
    assert "at most 90 characters" in injected
    assert "beside their pointer" in injected
    assert not injected.startswith("[butler summon]")


def test_summon_send_keeps_the_summon_frame_without_the_look_origin(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain ask-field rows keep the exact summon frame they had before."""
    ctx = FakePluginCtx()
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "turn off wifi",
        "session_id": "ses_123",
        "origin": "chip",
    })

    assert response.json()["ok"] is True
    assert ctx.injected == [{
        "text": frame_summon_text("turn off wifi"),
        "role": "user",
        "session_key": "ses_123",
    }]
    assert "butler_say" not in ctx.injected[0]["text"]


def test_summon_send_frames_a_bare_look_question_with_a_fallback_context(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakePluginCtx()
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    client.post("/summon/send", json={
        "text": "what is this control?",
        "session_id": "ses_123",
        "origin": "look",
    })

    injected = ctx.injected[0]["text"]
    assert injected.startswith(
        "[butler look] the control under the user's pointer\n"
        "Question: what is this control?\n\n"
    )


def test_state_exposes_the_agents_on_screen_answer(
        client: TestClient, tmp_path: Path) -> None:
    """say.json is its own file, merged fresh into /state beside the guide."""
    say = {"id": "abcd1234", "text": "It exports the page as a PDF.", "at": 123.5}
    path = tmp_path / "plugin-data" / "butler" / "say.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(say), encoding="utf-8")

    body = client.get("/state").json()

    assert body["say"] == say


def test_state_omits_say_without_a_usable_row(client: TestClient, tmp_path: Path) -> None:
    assert "say" not in client.get("/state").json()

    path = tmp_path / "plugin-data" / "butler" / "say.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert "say" not in client.get("/state").json()

    # A row without an id or text can never be spoken once: keep it out.
    path.write_text(json.dumps({"id": "", "text": "orphan"}), encoding="utf-8")
    assert "say" not in client.get("/state").json()
    path.write_text(json.dumps({"id": "abcd1234", "text": "   "}), encoding="utf-8")
    assert "say" not in client.get("/state").json()


def test_the_answer_tool_and_state_share_one_say_file(
        client: TestClient) -> None:
    """engine.say writes where /state reads, even between two processes' paths."""
    engine.init(bp_paths.plugin_data_dir("butler"))

    payload = engine.say("It exports the page as a PDF.")
    assert payload["ok"] is True

    state = client.get("/state").json()
    assert state["say"]["text"] == "It exports the page as a PDF."
    stored = json.loads((bp_paths.plugin_data_dir("butler") / "say.json")
                        .read_text(encoding="utf-8"))
    assert state["say"]["id"] == stored["id"]

    # A second answer replaces the row and /state sees it without a restart.
    engine.say("The second answer.")
    assert client.get("/state").json()["say"]["text"] == "The second answer."
