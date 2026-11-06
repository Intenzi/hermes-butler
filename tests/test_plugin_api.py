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
