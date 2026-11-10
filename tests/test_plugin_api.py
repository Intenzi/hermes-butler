"""Contract tests for the Butler dashboard API router."""
from __future__ import annotations

import json
import os
import sys
import time
import types
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
from butler_core.summon import frame_summon_text  # noqa: E402

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


class FakeEntry:
    def __init__(self, session_key):
        self.session_key = session_key


class FakeStore:
    def __init__(self, by_id=None, by_key=None):
        self.by_id = dict(by_id or {})
        self.by_key = dict(by_key or {})
        self.calls = []

    def lookup_by_session_id(self, session_id):
        self.calls.append(("id", session_id))
        return self.by_id.get(session_id)

    def lookup_by_session_key(self, session_key):
        self.calls.append(("key", session_key))
        return self.by_key.get(session_key)


class ResolvingPluginCtx(FakePluginCtx):
    """A plugin context whose manager publishes a live gateway runner."""

    def __init__(self, store, accepted=True, raises=False):
        super().__init__(accepted=accepted, raises=raises)
        runner = SimpleNamespace(session_store=store)
        self._manager = SimpleNamespace(
            _gateway_message_injector=(runner, lambda **kwargs: True))


def test_summon_send_derives_the_store_route_for_a_bare_local_id(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = "local-ec39b9cb"
    route = "agent:main:local:dm:local-ec39b9cb"
    store = FakeStore(by_id={bare: FakeEntry(route)})
    ctx = ResolvingPluginCtx(store)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={"text": "turn off wifi", "session_id": bare})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session": bare, "session_key": route}
    assert ctx.injected == [{
        "text": frame_summon_text("turn off wifi"),
        "role": "user",
        "session_key": route,
    }]
    assert store.calls[0] == ("id", bare), "the runner's own store resolver was asked"


def test_summon_send_accepts_an_already_full_route_only_when_the_store_knows_it(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    route = "agent:main:local:dm:local-known"
    store = FakeStore(by_key={route: FakeEntry(route)})
    ctx = ResolvingPluginCtx(store)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={"text": "fix it", "session_id": route})

    assert response.json() == {"ok": True, "session": route, "session_key": route}
    assert ctx.injected[0]["session_key"] == route


class KeyBuildingStore(FakeStore):
    """A store with no id lookup, only the gateway's own key builder."""

    def __init__(self, route):
        super().__init__(by_key={route: FakeEntry(route)})
        self.route = route
        self.generated = []

    def lookup_by_session_id(self, session_id):
        self.calls.append(("id", session_id))
        return None

    def _generate_session_key(self, source):
        self.generated.append((source.platform.value, source.chat_type, source.chat_id))
        return self.route


def test_summon_send_falls_back_to_the_stores_own_key_builder(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = "local-generated"
    route = "agent:main:local:dm:local-generated"
    store = KeyBuildingStore(route)
    ctx = ResolvingPluginCtx(store)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    class FakeSource:
        def __init__(self, platform, chat_type, chat_id):
            self.platform = platform
            self.chat_type = chat_type
            self.chat_id = chat_id

    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    gateway_config = types.ModuleType("gateway.config")
    gateway_config.Platform = SimpleNamespace(LOCAL=SimpleNamespace(value="local"))
    gateway_session = types.ModuleType("gateway.session")
    gateway_session.SessionSource = FakeSource
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.config", gateway_config)
    monkeypatch.setitem(sys.modules, "gateway.session", gateway_session)

    response = client.post("/summon/send", json={"text": "fix it", "session_id": bare})

    assert response.json() == {"ok": True, "session": bare, "session_key": route}
    assert store.generated == [("local", "dm", bare)]
    assert ctx.injected[0]["session_key"] == route


def test_summon_send_refuses_an_unknown_bare_id_as_unroutable(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    ctx = ResolvingPluginCtx(store)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={"text": "fix it", "session_id": "local-missing"})

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["reason"] == "unroutable_session"
    assert "local-missing" in body["detail"]
    assert ctx.injected == [], "a bare id is never handed to inject_message as a route"


def test_summon_send_injects_the_exact_frame_into_the_selected_session(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakePluginCtx()
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "turn off wifi",
        "session_id": "ses_123",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "session": "ses_123", "session_key": "ses_123"}
    assert ctx.injected == [{
        "text": frame_summon_text("turn off wifi"),
        "role": "user",
        "session_key": "ses_123",
    }]
    assert ctx.injected[0]["text"].startswith("[butler summon] turn off wifi\n\n")


def test_summon_send_returns_the_denied_contract_when_injection_is_refused(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakePluginCtx(accepted=False)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "fix it",
        "session_id": "ses_123",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "injection_denied"
    assert body["detail"] == "gateway refused session_key ses_123"
    assert len(ctx.injected) == 1


def test_summon_send_fails_closed_when_injection_raises(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakePluginCtx(raises=True)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "fix it",
        "session_id": "ses_123",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "injection_denied"
    assert body["detail"] == "gateway refused session_key ses_123"


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
