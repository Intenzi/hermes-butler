"""Round 24: a guide never reads inside the Hermes window, explains an
off-screen control instead of ringing the origin, and ends its walker double.

The measured session (guide 96690d591c) had the user looking at Hermes while
the chat transcript carried the literal text "Privacy & Security". The poll
captured Hermes, matched the transcript AXStaticText row, rang it, and the
step could never verify; at 180 s the guide was abandoned and the companion's
walker pet stayed on screen. These tests pin all three guards plus the
once-only guide_end contract.
"""
from __future__ import annotations

import json
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
        self.quit_calls = 0
        self.guide_ends = []

    def start(self):
        return True, "fake"

    def publish(self, state):
        self.published.append(dict(state))

    def idle(self):
        self.idled += 1

    def done(self):
        self.done_calls += 1

    def guide_end(self, reason=None):
        self.guide_ends.append(reason)

    def quit(self):
        self.quit_calls += 1


def _snapshot(elements, app="System Settings", window="Screen Recording"):
    return {
        "trusted": True,
        "app": app,
        "window": window,
        "count": len(elements),
        "with_box": len(elements),
        "elements": list(elements),
    }


def _element(label, box=(10, 20, 30, 40), role="AXButton"):
    return {"role": role, "label": label, "box": list(box), "id": 0}


def _windowed_element(label, window, box=(10, 20, 30, 40), role="AXButton"):
    element = _element(label, box=box, role=role)
    element["window"] = window
    return element


def _step(find_label, expect=None, say="Do the thing"):
    step = {"say": say}
    if find_label is not None:
        step["find"] = {"label": find_label}
    if expect:
        step["expect"] = dict(expect)
    return step


def _session(steps, *, title="Screen Recording", status="running", opened_at=None):
    now = time.time()
    return {
        "id": "guide-guard",
        "title": title,
        "steps": steps,
        "state": {
            "idx": 0,
            "status": status,
            "say": steps[0]["say"],
            "step_opened_at": opened_at if opened_at is not None else now,
        },
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
    engine.SCREEN_VISIBLE_FRAMES = ()
    yield engine, fake, tmp_path
    engine._stop_flag = True
    engine._session = None
    engine._last_deep_capture_at = None
    engine.SCREEN_VISIBLE_FRAMES = ()


# ------------------------------------------------------------------ Fix 1


def test_hermes_capture_never_advances_a_step_about_another_app(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    transcript = _element("Privacy & Security", box=(331, 149, 126, 17),
                          role="AXStaticText")
    # The measured guide's title MENTIONS Hermes, but its step targets System
    # Settings: a mention is not a Hermes plan.
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [transcript], app="Hermes", window="Hermes"))
    session = _session(
        [_step("Privacy & Security", {"app": "System Settings"},
               say="Open Screen Recording")],
        title="Allow Screen Recording for Hermes")

    eng._poll_once(session)

    assert session["state"]["idx"] == 0, "the transcript must not verify a step"
    assert session["verifications"] == []
    assert session["state"]["note"] == (
        "Switch to System Settings — this step happens in System Settings")
    assert session["scope_app"] == "System Settings"
    assert fake.published == [], "nothing inside the Hermes window may ring"
    assert fake.idled >= 1
    assert session["state"]["status"] != "stalled"
    assert session["state"]["status"] != "abandoned"


def test_the_switch_note_names_the_last_verified_app(
        engine_state, monkeypatch) -> None:
    # No declared app anywhere: the step is unscoped, so the round-24 Hermes
    # guard still applies and its note names the app the guide last proved.
    eng, _fake, _tmp = engine_state
    session = _session([_step("Screen Recording", {"visible": "Screen Recording"})])
    session["last_verified_app"] = "System Settings"
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([], app="Hermes",
                                                   window="Hermes"))

    eng._poll_once(session)

    assert session["scope_app"] is None
    assert session["state"]["note"].endswith("switch to System Settings")


def test_a_guide_that_targets_hermes_still_matches_inside_hermes(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Settings")], app="Hermes",
                            window="Hermes"))
    session = _session(
        [_step("Settings", {"app": "Hermes"}, say="Open Settings")])

    eng._poll_once(session)

    assert session["state"]["idx"] == 1
    assert session["state"]["status"] == "done"
    assert session["state"]["note"] is None
    assert [row["reason"] for row in session["verifications"]] == ["app_focused"]
    assert fake.guide_ends == ["done"], "the walk ends exactly once"


def test_a_mixed_guide_keeps_the_guard_on_its_non_hermes_step(
        engine_state, monkeypatch) -> None:
    """Step 2 targeting Hermes must not unguard step 1's System Settings plan."""
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Privacy & Security",
                                      box=(331, 149, 126, 17),
                                      role="AXStaticText"),
                             _element("Settings")],
                            app="Hermes", window="Hermes"))
    session = _session([
        _step("Privacy & Security", {"app": "System Settings"}),
        _step("Settings", {"app": "Hermes"}),
    ])

    eng._poll_once(session)

    assert session["state"]["idx"] == 0
    assert session["state"]["note"] == (
        "Switch to System Settings — this step happens in System Settings")
    assert fake.published == []

    # Step 2 is a Hermes step and must still match inside Hermes.
    session["state"]["idx"] = 1
    session["state"]["step_opened_at"] = time.time()
    eng._poll_once(session)

    assert session["state"]["idx"] == 2
    assert session["state"]["status"] == "done"


def test_app_less_plan_that_mentions_hermes_is_still_guarded(
        engine_state, monkeypatch) -> None:
    """No expect.app anywhere + a title mention must still block the transcript.

    This is the app-less shape of the measured guide: ``visible`` expectations
    name a System Settings control, the title names Hermes only as the subject.
    """
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Privacy & Security",
                                      box=(331, 149, 126, 17),
                                      role="AXStaticText")],
                            app="Hermes", window="Hermes"))
    session = _session(
        [_step("Privacy & Security", {"visible": "Privacy & Security"})],
        title="Allow Screen Recording for Hermes")

    eng._poll_once(session)

    assert session["state"]["idx"] == 0
    assert session["state"]["note"] == (
        "Butler can't read the Hermes window for this step — "
        "switch to the app the step is about")
    assert fake.published == []


def test_an_app_less_step_inherits_a_hermes_plan(
        engine_state, monkeypatch) -> None:
    """A step with no app key in a plan that names Hermes as an app matches."""
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Settings")], app="Hermes",
                            window="Hermes"))
    session = _session([
        _step("Dashboard", {"app": "Hermes"}),
        _step("Settings", {"visible": "Settings"}),
    ])
    session["state"]["idx"] = 1
    session["state"]["step_opened_at"] = time.time()

    eng._poll_once(session)

    assert session["state"]["idx"] == 2
    assert session["state"]["status"] == "done"
    assert fake.published, "the app-less Hermes step must still ring"


def test_hermes_window_rows_are_dropped_before_best_match(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    # A mixed capture: one row explicitly claims the Hermes main window, the
    # other is an ordinary System Settings control. Only the latter may match.
    transcript = dict(_element("Privacy & Security", box=(331, 149, 126, 17)),
                      app="Hermes", window="Hermes")
    control = _element("Privacy & Security", box=(100, 200, 30, 30))
    snapshot = _snapshot([transcript, control], app="Preview",
                         window="Privacy & Security")
    filtered = eng._snapshot_without_hermes_window(snapshot)
    assert filtered["elements"] == [control]
    assert filtered["count"] == 1

    monkeypatch.setattr(eng.bp_capture, "capture", lambda **kwargs: snapshot)
    session = _session([_step("Privacy & Security", {"visible": "Privacy & Security"})])

    eng._poll_once(session)

    assert fake.published and fake.published[-1]["current_box"] == [100, 200, 30, 30]


# ------------------------------------------------------------------ Fix 2


def test_below_the_fold_box_scrolls_and_never_rings_or_stalls(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Hermes", box=(0, 832, 0, 0))]))
    opened_at = time.time() - eng.step_timeout() - 50
    session = _session(
        [_step("Hermes", {"visible": "Hermes"},
               say="Enable Hermes in Screen Recording")],
        opened_at=opened_at)

    eng._poll_once(session)

    assert session["state"]["note"] == "Scroll to bring Hermes into view"
    assert session["state"]["idx"] == 0
    assert session["verifications"] == []
    assert fake.published == [], "a degenerate box must not be ringed"
    assert fake.idled >= 1
    # The 120 s clock is reset when the note first appears and never escalates
    # while the user is plausibly scrolling.
    assert session["state"]["status"] == "running"
    assert "stalled_at" not in session["state"]
    reset_at = session["state"]["step_opened_at"]
    assert reset_at > opened_at

    eng._poll_once(session)

    assert session["state"]["note"] == "Scroll to bring Hermes into view"
    assert session["state"]["step_opened_at"] == reset_at, \
        "the clock must reset only when the note first appears"


def test_off_screen_origin_is_explained_when_screen_frames_are_known(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    eng.SCREEN_VISIBLE_FRAMES = ((0, 0, 1440, 900),)
    snapshot = _snapshot([_element("Export", box=(5000, 5000, 40, 20))])
    monkeypatch.setattr(eng.bp_capture, "capture", lambda **kwargs: snapshot)
    session = _session([_step("Export", {"visible": "Export"})])

    eng._poll_once(session)

    assert session["state"]["note"] == "Scroll to bring Export into view"
    assert fake.published == []


# ------------------------------------------------------------------ Fix 3/4


def test_a_terminal_guide_ends_the_walk_once_and_twice_is_harmless(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([_element("Keyboard")]))
    session = _session([_step("Keyboard", {"visible": "Keyboard"})])

    eng._poll_once(session)
    assert session["state"]["status"] == "done"
    assert fake.guide_ends == ["done"]

    # The same guide must never send a second end (a slow/duplicate caller).
    assert eng._end_guide_overlay(session, "abandoned") is False
    assert fake.guide_ends == ["done"]


def test_stopped_and_abandoned_guides_call_guide_end_once(
        engine_state, monkeypatch) -> None:
    eng, fake, tmp_path = engine_state
    (tmp_path / "config.json").write_text(
        '{"step_timeout_seconds": 120, "stall_auto_stop_seconds": 5}',
        encoding="utf-8")
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([]))
    says = []
    journaled = []
    monkeypatch.setattr(eng, "say",
                        lambda text: says.append(text) or {"ok": True})
    monkeypatch.setattr(eng.bp_overlay, "journal_click", journaled.append)

    abandoned = _session([_step("Keyboard", {"visible": "Keyboard"})],
                         status="stalled",
                         opened_at=time.time() - 200)
    abandoned["state"]["stalled_at"] = time.time() - 6
    eng._session = abandoned
    eng._poll_once(abandoned)

    assert abandoned["state"]["status"] == "abandoned"
    assert abandoned["state"]["note"] == "step 1 was never verified"
    assert fake.guide_ends == ["abandoned"]
    assert says == ["Guide stopped — step 1 never verified"]
    assert "engine-guide-end:abandoned" in journaled
    saved = json.loads((tmp_path / "guides" / (abandoned["id"] + ".json"))
                       .read_text(encoding="utf-8"))
    assert saved.get("abandon_said") is True, \
        "the once-only flag must persist so a reload cannot speak again"

    stopped = _session([_step("Keyboard", {"visible": "Keyboard"})])
    eng._session = stopped
    eng.guide_stop()

    assert fake.guide_ends == ["abandoned", "stopped"]
    assert eng.guide_stop() == {"status": "idle"}
    assert fake.guide_ends == ["abandoned", "stopped"]


def test_superseded_guide_ends_its_walk(engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot([_element("Keyboard")]))
    eng._session = _session([_step("Keyboard", {"visible": "Keyboard"})])

    eng.guide_start({"title": "Second", "steps": [_step("Keyboard")],
                     "replace": True})

    assert fake.guide_ends == ["superseded"]


def test_guide_start_in_hermes_returns_the_switch_note_without_ringing(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Privacy & Security",
                                      box=(331, 149, 126, 17),
                                      role="AXStaticText")],
                            app="Hermes", window="Hermes"))

    result = eng.guide_start({
        "title": "Allow Screen Recording for Hermes",
        "steps": [_step("Privacy & Security", {"app": "System Settings"})],
    })

    assert result["overlay"] == "waiting"
    assert result["note"] == (
        "Switch to System Settings — this step happens in System Settings")
    assert fake.published == []
    assert fake.guide_ends == [], "a live guide has not ended"


def test_guide_start_explains_a_first_step_below_the_fold(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Hermes", box=(0, 832, 0, 0))]))

    result = eng.guide_start({
        "title": "Screen Recording",
        "steps": [_step("Hermes", {"visible": "Hermes"})],
    })

    assert result["overlay"] == "waiting"
    assert result["note"] == "Scroll to bring Hermes into view"
    assert fake.published == []


def test_capture_failed_start_ends_the_walk(engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    failed = {
        "trusted": False,
        "app": None,
        "window": None,
        "count": 0,
        "with_box": 0,
        "elements": [],
        "note": "Accessibility not granted to this process",
    }
    monkeypatch.setattr(eng.bp_capture, "capture", lambda **kwargs: failed)

    result = eng.guide_start({"title": "Grant AX",
                              "steps": [_step("Settings")]})

    assert result["state"]["status"] == "capture_failed"
    assert fake.guide_ends == ["capture_failed"]


# ------------------------------------------- Round 25: every step has a scope
#
# The shipped guard only blocked the Hermes window. The general rule is wider:
# a step scoped to an app may not match a capture of any other app, and Hermes
# is one instance of "the wrong app". These tests pin the scope resolution, the
# app-scoped matching, the window PREFERENCE (never a hard filter) and the
# unscoped fallback with its one-time journal line.


def test_a_capture_of_another_app_never_matches_and_resets_the_clock_once(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Privacy & Security",
                                      box=(331, 149, 126, 17),
                                      role="AXStaticText")],
                            app="Hermes", window="Hermes"))
    opened_at = time.time() - eng.step_timeout() - 50
    session = _session(
        [_step("Privacy & Security", {"app": "System Settings"})],
        opened_at=opened_at)

    eng._poll_once(session)

    assert session["scope_app"] == "System Settings"
    assert session["state"]["idx"] == 0
    assert session["verifications"] == []
    assert session["state"]["note"] == (
        "Switch to System Settings — this step happens in System Settings")
    assert fake.published == [], "another app must never ring"
    assert fake.idled >= 1
    assert session["state"]["status"] == "running"
    assert "stalled_at" not in session["state"], \
        "switching apps is not a stall"
    reset_at = session["state"]["step_opened_at"]
    assert reset_at > opened_at

    eng._poll_once(session)

    assert session["state"]["note"] == (
        "Switch to System Settings — this step happens in System Settings")
    assert session["state"]["step_opened_at"] == reset_at, \
        "the clock resets only the first time the note appears"


def test_a_capture_of_the_scoped_app_matches_normally(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Privacy & Security",
                                      box=(100, 200, 30, 30))],
                            app="System Settings", window="Screen Recording"))
    session = _session(
        [_step("Privacy & Security", {"app": "System Settings"})])

    eng._poll_once(session)

    assert session["scope_app"] == "System Settings"
    assert session["state"]["idx"] == 1
    assert session["state"]["status"] == "done"
    assert session["state"]["note"] is None
    assert [row["reason"] for row in session["verifications"]] == ["app_focused"]
    assert fake.published and fake.published[-1]["current_box"] == [100, 200, 30, 30]


def test_an_app_less_step_inherits_the_nearest_earlier_scope(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    session = _session([
        _step("Keyboard", {"app": "System Settings"}),
        _step("Network", {"visible": "Network"}),
    ])
    session["state"]["idx"] = 1
    session["state"]["step_opened_at"] = time.time()
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Network")], app="Hermes",
                            window="Hermes"))

    assert eng._resolve_scope_app(session, 1) == "System Settings"

    eng._poll_once(session)

    assert session["scope_app"] == "System Settings"
    assert session["state"]["idx"] == 1, "an inherited scope still hard-filters"
    assert session["state"]["note"] == (
        "Switch to System Settings — this step happens in System Settings")
    assert fake.published == []


def test_expect_app_alone_sets_the_step_scope(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Keyboard")], app="Preview",
                            window="Keyboard"))
    session = _session([_step("Keyboard", {"app": "System Settings"})])

    eng._poll_once(session)

    assert session["scope_app"] == "System Settings"
    assert session["state"]["idx"] == 0
    assert session["state"]["note"] == (
        "Switch to System Settings — this step happens in System Settings")
    assert fake.published == []


def test_a_window_hint_prefers_but_never_hard_filters_a_menu_of_the_same_app(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    # The menu row is a different window of the same app: it must still match.
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_windowed_element("Network", "Menubar menu",
                                               box=(10, 20, 30, 40))],
                            app="System Settings", window="System Settings"))
    step = {"say": "Open Network",
            "find": {"label": "Network", "app": "System Settings"},
            "expect": {"window": "Network"}}
    session = _session([step])

    eng._poll_once(session)

    assert session["scope_app"] == "System Settings"
    assert fake.published and fake.published[-1]["current_box"] == [10, 20, 30, 40]
    # The window title differs, so the expect is not satisfied — but the find
    # still matched, which is what proves the hint is not a filter.
    assert session["state"]["idx"] == 0


def test_a_window_hint_prefers_the_named_window_on_a_tie(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_windowed_element("Network", "System Settings",
                                               box=(1, 2, 3, 4)),
                             _windowed_element("Network", "Network",
                                               box=(10, 20, 30, 40))],
                            app="System Settings", window="System Settings"))
    step = {"say": "Open Network", "find": {"label": "Network"},
            "expect": {"window": "Network"}}
    session = _session([step])

    eng._poll_once(session)

    assert fake.published and fake.published[-1]["current_box"] == [10, 20, 30, 40], \
        "the named window ranks first among equal matches"


def test_a_step_naming_hermes_matches_hermes_content(
        engine_state, monkeypatch) -> None:
    """The guard is a scope, not a blanket ban on any capture."""
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Settings")], app="Hermes",
                            window="Hermes"))
    session = _session([
        {"say": "Open Settings",
         "find": {"label": "Settings", "app": "Hermes"},
         "expect": {"visible": "Settings"}},
    ])

    eng._poll_once(session)

    assert session["scope_app"] == "Hermes"
    assert session["state"]["idx"] == 1
    assert session["state"]["status"] == "done"
    assert fake.published and fake.published[-1]["current_label"] == "Settings"


def test_an_unresolved_scope_matches_unscoped_and_journals_once(
        engine_state, monkeypatch) -> None:
    eng, fake, _tmp = engine_state
    journaled = []
    monkeypatch.setattr(eng.bp_overlay, "journal_click", journaled.append)
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Keyboard"), _element("Network")],
                            app="Preview", window="Keyboard"))
    session = _session([
        _step("Keyboard", {"visible": "Keyboard"}),
        _step("Network", {"visible": "Network"}),
    ])

    eng._poll_once(session)
    assert session["state"]["idx"] == 1
    assert session["scope_app"] is None

    eng._poll_once(session)

    assert session["state"]["idx"] == 2
    assert session["state"]["status"] == "done"
    assert session["scope_app"] is None
    assert journaled.count("guide-scope:unresolved") == 1, \
        "one journal line per guide, not per step/poll"
    assert fake.published, "an unscoped step still matches, exactly as before"


def test_normalize_step_keeps_find_app_and_window() -> None:
    clean = engine._normalize_step({
        "say": "Open Network",
        "find.label": "Network",
        "find.app": "System Settings",
        "find.window": "Network",
    })

    assert clean["find"] == {"label": "Network", "app": "System Settings",
                             "window": "Network"}


def test_scope_only_keys_do_not_make_a_dead_step_verifiable() -> None:
    assert engine._normalize_step({"say": "Open System Settings",
                                   "find.app": "System Settings"}) is None


def test_an_inherited_hermes_scope_still_matches_hermes_content(
        engine_state, monkeypatch) -> None:
    """Inheritance must not trip the round-24 Hermes title heuristic.

    Step 2 declares no app and inherits Hermes from step 1: the capture IS the
    scoped app, so the belt-and-braces Hermes guard has nothing to block.
    """
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Send")], app="Hermes", window="Hermes"))
    session = _session([
        {"say": "Open Settings",
         "find": {"label": "Settings", "app": "Hermes"}},
        _step("Send", {"visible": "Send"}),
    ])
    session["state"]["idx"] = 1
    session["state"]["step_opened_at"] = time.time()

    eng._poll_once(session)

    assert session["scope_app"] == "Hermes"
    assert session["state"]["idx"] == 2
    assert session["state"]["status"] == "done"
    assert fake.published and fake.published[-1]["current_label"] == "Send"


def test_a_second_blocked_step_in_the_same_scope_resets_its_own_clock(
        engine_state, monkeypatch) -> None:
    """Each switch note is a NEW guard note, so step 2 gets a fresh clock."""
    eng, fake, _tmp = engine_state
    blocked = _snapshot(
        [_element("Privacy & Security", box=(331, 149, 126, 17))],
        app="Hermes", window="Hermes")
    allowed = _snapshot(
        [_element("Privacy & Security", box=(100, 200, 30, 30))],
        app="System Settings", window="Screen Recording")
    calls = {"n": 0}

    def capture(**kwargs):
        calls["n"] += 1
        return blocked if calls["n"] in (1, 3) else allowed

    monkeypatch.setattr(eng.bp_capture, "capture", capture)
    session = _session([
        _step("Privacy & Security", {"app": "System Settings"}),
        _step("Privacy & Security", {"app": "System Settings"}),
    ])

    eng._poll_once(session)
    assert session["state"]["note"] == (
        "Switch to System Settings — this step happens in System Settings")

    eng._poll_once(session)
    assert session["state"]["idx"] == 1, "the scoped capture advances"
    assert session["state"]["note"] is None

    old = time.time() - eng.step_timeout() - 50
    session["state"]["step_opened_at"] = old
    eng._poll_once(session)

    assert session["state"]["idx"] == 1
    assert session["state"]["step_opened_at"] > old, \
        "a repeated note on a NEW step must reset the clock again"
    assert session["state"]["status"] == "running"
    assert "stalled_at" not in session["state"]


def test_a_capture_that_only_carries_per_row_apps_is_still_scoped(
        engine_state, monkeypatch) -> None:
    """The companion sets ``app`` per snapshot; a row-only capture still works."""
    eng, fake, _tmp = engine_state
    row = dict(_element("Privacy & Security", box=(100, 200, 30, 30)),
               app="System Settings")
    snapshot = {"trusted": True, "app": None, "window": "Screen Recording",
                "count": 1, "with_box": 1, "elements": [row]}
    monkeypatch.setattr(eng.bp_capture, "capture", lambda **kwargs: snapshot)
    session = _session([
        {"say": "Open Screen Recording",
         "find": {"label": "Privacy & Security", "app": "System Settings"},
         "expect": {"visible": "Privacy & Security"}},
    ])

    eng._poll_once(session)

    assert session["scope_app"] == "System Settings"
    assert session["state"]["idx"] == 1
    assert fake.published and fake.published[-1]["current_box"] == [100, 200, 30, 30]


def test_a_broad_scope_name_does_not_match_a_longer_app_name(
        engine_state, monkeypatch) -> None:
    """A hard filter must not widen "Mail" into "Gmail"."""
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Inbox")], app="Gmail",
                            window="Inbox"))
    session = _session([
        {"say": "Open the Inbox", "find": {"label": "Inbox", "app": "Mail"},
         "expect": {"visible": "Inbox"}},
    ])

    eng._poll_once(session)

    assert session["state"]["idx"] == 0
    assert session["state"]["note"] == (
        "Switch to Mail — this step happens in Mail")
    assert fake.published == []


def test_a_short_scope_name_matches_the_localized_app_name(
        engine_state, monkeypatch) -> None:
    """The plan may spell "Settings" for "System Settings" (whole-word)."""
    eng, fake, _tmp = engine_state
    monkeypatch.setattr(eng.bp_capture, "capture",
                        lambda **kwargs: _snapshot(
                            [_element("Keyboard")], app="System Settings",
                            window="Keyboard"))
    session = _session([
        {"say": "Open Keyboard", "find": {"label": "Keyboard", "app": "Settings"},
         "expect": {"visible": "Keyboard"}},
    ])

    eng._poll_once(session)

    assert session["state"]["idx"] == 1
    assert fake.published
