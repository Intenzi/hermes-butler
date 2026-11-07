"""Transport tests for the Swift companion Accessibility capture.

The Python side is only a JSON-lines transport now; these tests exercise the
real stdin/stdout round-trip with pipes (no TCC or live GUI needed).
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from butler_core import capture, engine, overlay  # noqa: E402


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, chunk):
        self.data += chunk
        return len(chunk)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    pid = 4242

    def __init__(self, stdout=None) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout

    def poll(self):
        return None


def _proc_with_response(response) -> tuple[_FakeProc, int]:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (json.dumps(response) + "\n").encode("utf-8"))
    os.close(write_fd)
    return _FakeProc(stdout=os.fdopen(read_fd, "rb", buffering=0)), read_fd


def test_overlay_request_writes_one_op_and_parses_one_line() -> None:
    reply = {"trusted": True, "prompted": False}
    proc, _fd = _proc_with_response(reply)
    helper = overlay.Overlay()
    helper.proc = proc
    try:
        assert helper.request({"op": "axtrusted"}, timeout=1.0) == reply
    finally:
        proc.stdout.close()

    sent = [json.loads(line) for line in proc.stdin.data.decode("utf-8").splitlines()]
    assert sent == [{"op": "axtrusted"}]



def test_overlay_request_accepts_in_memory_stdout() -> None:
    # Fake helpers often expose BytesIO instead of a selectable pipe.
    proc = _FakeProc(stdout=io.BytesIO(b'{"trusted": false, "prompted": false}\n'))
    helper = overlay.Overlay()
    helper.proc = proc

    assert helper.request({"op": "axtrusted"}, timeout=0.1) == {
        "trusted": False,
        "prompted": False,
    }

def test_capture_sends_axcapture_and_returns_reply(monkeypatch) -> None:
    reply = {
        "trusted": True,
        "app": "System Settings",
        "window": "Accessibility",
        "count": 1,
        "with_box": 1,
        "elements": [{"role": "AXButton", "label": "Keyboard", "box": [1, 2, 3, 4], "id": 0}],
        "focused": "Keyboard",
    }
    proc, _fd = _proc_with_response(reply)
    helper = overlay.Overlay()
    helper.proc = proc
    monkeypatch.setattr(capture, "HAS_MAC", True)
    monkeypatch.setattr(capture, "_helper", lambda: helper)
    try:
        snap = capture.capture(target="System Settings", max_elements=25, depth=4)
    finally:
        proc.stdout.close()

    assert snap == reply
    sent = [json.loads(line) for line in proc.stdin.data.decode("utf-8").splitlines()]
    assert sent == [{
        "op": "axcapture",
        "target": "System Settings",
        "max_elements": 25,
        "depth": 4,
    }]


def test_capture_timeout_returns_failed_snapshot_without_raising(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()  # keep the write end open: silence, not EOF
    helper = overlay.Overlay()
    helper.proc = _FakeProc(stdout=os.fdopen(read_fd, "rb", buffering=0))
    monkeypatch.setattr(capture, "HAS_MAC", True)
    monkeypatch.setattr(capture, "_helper", lambda: helper)
    monkeypatch.setattr(capture, "REQUEST_TIMEOUT", 0.05)
    try:
        snap = capture.capture()
    finally:
        os.close(write_fd)
        helper.proc.stdout.close()

    assert snap["trusted"] is False
    assert snap["app"] is None
    assert snap["window"] is None
    assert snap["count"] == 0
    assert snap["with_box"] == 0
    assert snap["elements"] == []
    assert "did not return" in snap["note"]
    assert "Butler companion" in snap["fix"]


def test_untrusted_reply_propagates(monkeypatch) -> None:
    reply = {
        "trusted": False,
        "app": None,
        "window": None,
        "count": 0,
        "with_box": 0,
        "elements": [],
        "note": "Accessibility not granted to this process",
        "fix": "Grant Accessibility to Butler companion.",
    }
    proc, _fd = _proc_with_response(reply)
    helper = overlay.Overlay()
    helper.proc = proc
    monkeypatch.setattr(capture, "HAS_MAC", True)
    monkeypatch.setattr(capture, "_helper", lambda: helper)
    try:
        snap = capture.capture()
    finally:
        proc.stdout.close()

    assert snap == reply
    assert snap["trusted"] is False
    assert snap["note"]


def test_trusted_uses_silent_axtrusted_and_ask_sets_prompt(monkeypatch) -> None:
    ops = []

    def fake_request(op, timeout=None):
        ops.append(op)
        if op.get("prompt"):
            return {"trusted": False, "prompted": True}
        return {"trusted": True, "prompted": False}

    monkeypatch.setattr(capture, "HAS_MAC", True)
    monkeypatch.setattr(capture, "_request", fake_request)

    assert capture.trusted() is True
    assert capture._ask_accessibility() is True
    assert ops == [{"op": "axtrusted"}, {"op": "axtrusted", "prompt": True}]


class _FakeOverlay:
    def __init__(self) -> None:
        self.published = []
        self.idled = 0
        self.quit_calls = 0

    def start(self):
        return True, "fake"

    def publish(self, state):
        self.published.append(state)

    def idle(self):
        self.idled += 1

    def quit(self):
        self.quit_calls += 1


@pytest.fixture
def engine_state(tmp_path, monkeypatch):
    engine.init(tmp_path)
    fake = _FakeOverlay()
    monkeypatch.setattr(engine, "_OVERLAY", fake)
    monkeypatch.setattr(engine, "_loop", lambda: None)
    engine._session = None
    engine._stop_flag = False
    yield engine, fake
    engine._stop_flag = True
    engine._session = None


def test_guide_start_capture_failure_returns_guide_id_and_note(engine_state, monkeypatch) -> None:
    eng, _fake = engine_state
    failed = {
        "trusted": False,
        "app": None,
        "window": None,
        "count": 0,
        "with_box": 0,
        "elements": [],
        "note": "Accessibility not granted to this process",
        "fix": "Grant Accessibility to Butler companion.",
    }
    monkeypatch.setattr(eng.bp_capture, "capture", lambda **kwargs: failed)

    result = eng.guide_start({
        "title": "Grant AX",
        "steps": [{"say": "Open Settings", "find": {"label": "Settings"}}],
    })

    assert result["guide_id"]
    assert result["state"]["status"] == "capture_failed"
    assert result["capture_note"] == failed["note"]
    assert result["fix"] == failed["fix"]


def test_guide_start_first_capture_rings_match(engine_state, monkeypatch) -> None:
    eng, fake = engine_state
    snap = {
        "trusted": True,
        "app": "System Settings",
        "window": "Accessibility",
        "count": 1,
        "with_box": 1,
        "elements": [{"role": "AXButton", "label": "Keyboard",
                      "box": [10, 20, 30, 40], "id": 0}],
    }
    monkeypatch.setattr(eng.bp_capture, "capture", lambda **kwargs: snap)

    result = eng.guide_start({
        "title": "Ring",
        "steps": [{"say": "Click Keyboard", "find": {"label": "Keyboard"}}],
    })

    assert result["guide_id"]
    assert result["overlay"] == "ringing"
    assert fake.published and fake.published[0]["current_box"] == [10, 20, 30, 40]


def test_doctor_reports_the_companion_grant_subject(monkeypatch) -> None:
    from butler_core import doctor

    monkeypatch.setattr(doctor.bp_capture, "trusted", lambda: False)
    monkeypatch.setattr(doctor.bp_ocr, "preflight_screen_record", lambda: True)
    monkeypatch.setattr(doctor.bp_overlay, "build_if_needed", lambda: (True, "present"))

    out = doctor.report(ask=False)

    assert out["accessibility_granted"] is False
    assert out["accessibility_grant_subject"] == capture.GRANT_SUBJECT
    assert out["accessibility_grant_subject"] == (
        "Butler companion - the butler plugin's bundled helper")
    assert "Butler companion" in out["fix"]
    assert "Privacy & Security -> Accessibility" in out["fix"]
    assert ("Because the butler reads the screen through its own small helper, "
            "macOS asks once and remembers across updates.") in out["fix"]
    assert "butler_core" not in out["fix"]
    assert "osascript" not in out["fix"]
