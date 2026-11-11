"""Two-pass capture tests: shallow first, one deep walk only when needed.

Electron/Chromium apps hide their real controls below the shallow AX depth, so
these tests pin the engine's shallow-hit / shallow-miss-then-deep behaviour and
the one-deep-walk-per-second budget.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import engine  # noqa: E402


class _FakeOverlay:
    def __init__(self) -> None:
        self.published = []
        self.idled = 0
        self.done_calls = 0

    def start(self):
        return True, "fake"

    def publish(self, state):
        self.published.append(dict(state))

    def idle(self):
        self.idled += 1

    def done(self):
        self.done_calls += 1

    def quit(self):
        pass


class _CaptureRecorder:
    """Return a shallow or deep snapshot and record every capture kwargs."""

    def __init__(self, shallow, deep) -> None:
        self.shallow = shallow
        self.deep = deep
        self.calls = []
        self.deep_calls = 0

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        is_deep = (kwargs.get("depth", 0) >= engine.DEEP_DEPTH
                   or kwargs.get("max_elements", 0) >= engine.DEEP_MAX_ELEMENTS)
        if is_deep:
            self.deep_calls += 1
            return self.deep
        return self.shallow


def _element(label, box=(10, 20, 30, 40), role="AXButton"):
    return {"role": role, "label": label, "box": list(box), "id": 0}


def _snapshot(elements, app="System Settings"):
    return {
        "trusted": True,
        "app": app,
        "window": "Settings",
        "count": len(elements),
        "with_box": len(elements),
        "elements": list(elements),
    }


def _step(find_label, expect_visible):
    return {
        "say": "Do the thing",
        "find": {"label": find_label},
        "expect": {"visible": expect_visible},
    }


def _session(step):
    now = time.time()
    return {
        "id": "depth-guide",
        "title": "Depth",
        "steps": [step],
        "state": {"idx": 0, "status": "running", "say": step["say"],
                  "step_opened_at": now},
        "verifications": [],
        "overlay": "waiting",
        "started_at": now,
    }


@pytest.fixture
def engine_state(tmp_path, monkeypatch):
    engine.init(tmp_path)
    fake = _FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", fake)
    monkeypatch.setattr(engine, "_loop", lambda: None)
    engine._session = None
    engine._stop_flag = False
    engine._last_deep_capture_at = None
    yield engine, fake
    engine._stop_flag = True
    engine._session = None
    engine._last_deep_capture_at = None


def test_shallow_miss_then_deep_hit_verifies_with_one_deep_walk(
        engine_state, monkeypatch) -> None:
    eng, _fake = engine_state
    shallow = _snapshot([{"role": "AXGroup", "label": None, "box": None, "id": 0}])
    deep = _snapshot([_element("Settings")])
    recorder = _CaptureRecorder(shallow, deep)
    monkeypatch.setattr(eng.bp_capture, "capture", recorder)

    session = _session(_step("Settings", "Settings"))
    eng._poll_once(session)

    assert recorder.deep_calls == 1
    assert len(recorder.calls) == 2
    assert recorder.calls[1]["depth"] == eng.DEEP_DEPTH
    assert recorder.calls[1]["max_elements"] == eng.DEEP_MAX_ELEMENTS
    assert session["state"]["idx"] == 1
    assert session["state"]["status"] == "done"
    assert session["verifications"][0]["reason"] == "element_visible"


def test_shallow_hit_never_runs_the_deep_pass(engine_state, monkeypatch) -> None:
    eng, _fake = engine_state
    shallow = _snapshot([_element("Open settings")])
    deep = _snapshot([_element("Open settings")])
    recorder = _CaptureRecorder(shallow, deep)
    monkeypatch.setattr(eng.bp_capture, "capture", recorder)

    session = _session(_step("Open settings", "Open settings"))
    eng._poll_once(session)

    assert recorder.deep_calls == 0
    assert len(recorder.calls) == 1
    assert session["state"]["idx"] == 1


def test_find_and_expect_share_the_deep_snapshot(engine_state, monkeypatch) -> None:
    eng, fake = engine_state
    # The find label exists in both walks, so only the unmet expect forces the
    # deep pass; after it, BOTH halves must be re-evaluated on that deep walk.
    shallow = _snapshot([_element("Settings", box=(1, 2, 3, 4))])
    deep = _snapshot([_element("Settings", box=(20, 30, 40, 50)),
                      _element("Search Cmd K")])
    recorder = _CaptureRecorder(shallow, deep)
    monkeypatch.setattr(eng.bp_capture, "capture", recorder)

    session = _session(_step("Settings", "Search Cmd K"))
    eng._poll_once(session)

    # One shallow walk + one deep walk, both reused by find and expect.
    assert len(recorder.calls) == 2
    assert recorder.deep_calls == 1
    assert session["state"]["idx"] == 1
    assert session["verifications"][0]["reason"] == "element_visible"
    # The ring came from the deep walk, not the shallow one.
    assert fake.published and fake.published[0]["current_box"] == [20, 30, 40, 50]


def test_two_ticks_inside_one_second_allow_at_most_one_deep_walk(
        engine_state, monkeypatch) -> None:
    eng, _fake = engine_state
    shallow = _snapshot([{"role": "AXGroup", "label": None, "box": None, "id": 0}])
    deep = _snapshot([{"role": "AXGroup", "label": None, "box": None, "id": 0}])
    recorder = _CaptureRecorder(shallow, deep)
    monkeypatch.setattr(eng.bp_capture, "capture", recorder)

    session = _session(_step("Settings", "Settings"))
    eng._poll_once(session)
    eng._poll_once(session)

    assert recorder.deep_calls == 1
    # tick 1: shallow + deep; tick 2: shallow only (deep budget spent)
    assert len(recorder.calls) == 3
    assert session["state"]["idx"] == 0


def test_guide_start_uses_deep_recapture_before_no_match(
        engine_state, monkeypatch) -> None:
    eng, fake = engine_state
    shallow = _snapshot([{"role": "AXGroup", "label": None, "box": None, "id": 0}])
    deep = _snapshot([_element("Settings")])
    recorder = _CaptureRecorder(shallow, deep)
    monkeypatch.setattr(eng.bp_capture, "capture", recorder)

    result = eng.guide_start({
        "title": "Electron overlay",
        "steps": [{"say": "Click Settings", "find": {"label": "Settings"}}],
    })

    assert result["overlay"] == "ringing"
    assert recorder.deep_calls == 1
    assert len(recorder.calls) == 2
    assert fake.published and fake.published[0]["current_box"] == [10, 20, 30, 40]
