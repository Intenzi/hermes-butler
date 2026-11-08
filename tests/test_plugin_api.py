"""Contract tests for the Butler dashboard API router."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import plugin_api  # noqa: E402  (import after sys.path setup)
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


def test_history_reads_plugins_data_candidate(client: TestClient, tmp_path: Path) -> None:
    guides = tmp_path / "plugins" / "butler" / "data" / "guides"
    guides.mkdir(parents=True)
    guide = {
        "id": "fallback",
        "title": "Fallback guide",
        "state": {"status": "done"},
        "steps": [],
        "verifications": [],
        "started_at": 12.0,
    }
    (guides / "fallback.json").write_text(json.dumps(guide), encoding="utf-8")

    response = client.get("/history")

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["fallback"]


def test_config_defaults_pet_click_claim_off(client: TestClient) -> None:
    response = client.get("/config")

    assert response.status_code == 200
    assert response.json() == {"summon": {"pet_click_claim": False}}


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


def test_summon_send_returns_the_denied_contract_when_injection_is_refused(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = FakePluginCtx(accepted=False)
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: ctx)

    response = client.post("/summon/send", json={
        "text": "fix it",
        "session_id": "ses_123",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "injection_denied"}
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
    assert response.json() == {"ok": False, "reason": "injection_denied"}


def test_summon_send_fails_closed_without_a_live_plugin_context(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_api, "_plugin_context", lambda: None)

    response = client.post("/summon/send", json={"text": "fix it", "session_id": "ses_123"})

    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "injection_denied"}


def test_summon_send_requires_text_and_a_session_id(client: TestClient) -> None:
    assert client.post("/summon/send", json={"text": "", "session_id": "ses_123"}).status_code == 400
    assert client.post("/summon/send", json={"text": "fix it", "session_id": ""}).status_code == 400
