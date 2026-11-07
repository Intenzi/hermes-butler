"""Contract tests for the Butler dashboard API router."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.plugin_api import router  # noqa: E402  (import after sys.path setup)


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
