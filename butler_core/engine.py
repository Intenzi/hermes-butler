"""Butler guide engine: step state machine + background verification thread.

State lives under the plugin data dir:
  guides/<id>.json   one session
  state.json         snapshot for the desktop surface / plugin_api
  say.json           the agent's short on-screen answer (butler_say)
  watch.jsonl        ambient watch event tail (bounded, 500 lines)

Fail-closed: unattended GUIDE runs are rejected at the tool layer (kwargs has no
approval context), always. WATCH is the exception by design: it only reads the
Accessibility tree and writes its own log, so an unattended run cannot act on
the screen. This module only writes its own data files (sessions, ``say.json``,
``watch.jsonl``); the poll thread refreshes them from live captures.
"""
import json
import os
import threading
import time
import uuid

from . import capture as bp_capture
from . import overlay as bp_overlay

_OVERLAY = bp_overlay.Overlay()

DATA_DIR = None
DEFAULT_POLL = 1.5
DEFAULT_TIMEOUT = 120.0
#: A stalled guide stops itself after this long without a verification. The
#: note is stable (no elapsed counter), so the pet stops repeating a changing
#: line; this deadline turns the silence into a clean stop instead of leaving
#: an unverified guide alive forever.
DEFAULT_STALL_AUTO_STOP = 180.0
DEFAULT_PET_CLICK_CLAIM = True
ERROR_STREAK_NOTE = 3  # consecutive poll errors before the note surfaces
#: The host truncates every plugin line at PET_MESSAGE_MAX_CHARS = 120, so a
#: longer answer shows an ellipsis in the bubble. Stay under it with headroom
#: for the plugin label; the chat carries the full explanation.
SAY_MAX_CHARS = 110
SAY_ELLIPSIS = "…"

# Electron/Chromium apps render their chrome as deeply nested divs. At depth 12
# a walk sees only the container groups of an overlay/page (settings nav rows,
# tab labels and buttons all sit below it), so every find.match and expect check
# silently misses while native apps (much shallower) keep working. The poll
# therefore starts cheap and shallow, then re-walks ONCE at depth 24 only when
# the shallow snapshot cannot satisfy the step. Do not "simplify" this back to
# a single shallow capture.
SHALLOW_MAX_ELEMENTS = 200
SHALLOW_DEPTH = 12
DEEP_MAX_ELEMENTS = 900
DEEP_DEPTH = 24
DEEP_CAPTURE_INTERVAL = 1.0  # seconds between deep walks, even if poll is faster

#: The Hermes desktop app, matched by app name. A guide must never match a
#: control inside this window unless its own plan targets Hermes: the chat
#: transcript is rendered as AXStaticText rows, so a user who is looking at
#: Hermes while reading the instructions can otherwise "ring" their own words
#: and never verify. One place changes the name.
HERMES_APP_NAMES = ("Hermes",)
#: Titles of the Hermes main window. The second capture filter drops elements
#: that claim both a Hermes app and this window, before ``best_match`` runs.
HERMES_MAIN_WINDOW_TITLES = ("Hermes",)
#: An AX box at or below this width/height is the "below the fold" sentinel
#: macOS reports for a control that is in the tree but scrolled out of view
#: (the measured row was [0, 832, 0, 0]). Ringing it draws a highlight at the
#: origin, so the engine explains the scroll instead.
DEGENERATE_BOX_MAX = 1.0
#: Fallback visible screen frames ``(x, y, w, h)`` in AX coordinates, used only
#: when a capture snapshot does not carry its own ``screens`` list. Empty means
#: "unknown"; the engine then treats only a degenerate box as off-screen rather
#: than guessing a display layout.
SCREEN_VISIBLE_FRAMES = ()

# Ambient WATCH (passive Accessibility observation). The companion owns the AX
# observer; this process only keeps a bounded tail for the agent and the desktop.
WATCH_RING = 40          # in-memory ring of the newest events
WATCH_STATE_RECENT = 12  # what state.json carries (newest last) for the desktop
WATCH_JSONL_MAX = 500    # <plugin data dir>/watch.jsonl tail cap
WATCH_STATUS_EVENTS = 10 # event lines watch_status() prints
WATCH_REQUEST_TIMEOUT = 5.0
# A scan start records its arm-time baseline (one full AX walk) BEFORE it acks,
# so it needs a longer budget than an ordinary start: a 1500-node walk on a
# busy app is tens of milliseconds per hundred nodes, not per thousand.
WATCH_SCAN_REQUEST_TIMEOUT = 15.0

# Content SCAN (the author-distinguishing mode): a repeating walk of the app's
# AX tree compared against an arm-time signature baseline. AX roles that count
# as an AUTHOR: AXStaticText is deliberately absent because that is the message
# BODY — including it would flag anyone who merely writes the name.
WATCH_SCAN_ROLES = ("AXImage", "AXButton", "AXLink")
WATCH_SCAN_INTERVAL_DEFAULT = 3
WATCH_SCAN_INTERVAL_MIN = 2
WATCH_SCAN_INTERVAL_MAX = 10
WATCH_SCAN_NODES_DEFAULT = 1500
WATCH_SCAN_NODES_MAX = 4000

#: The three surfaces a condition hit can name. ``window`` is the frontmost
#: window title, ``label`` the focused control's AXTitle/AXDescription, and
#: ``value`` its AXValue (the terminal pane's output). The companion records
#: which ones already contained the needle at arm and requires a transition
#: from those; the status lines name the surface the hit actually came from.
WATCH_SURFACES = ("window", "label", "value")
#: How a surface reads in the status text: at arm and on a hit.
WATCH_SURFACE_ARM = {"window": "window title", "label": "label",
                     "value": "value"}
WATCH_SURFACE_HIT = {"window": "window title", "label": "control label",
                     "value": "output"}

# A guide block on the shared snapshot is LIVE status only while the guide is
# actually being walked (running/capture_failed/stalled). Once terminal, the
# block is kept just long enough for the pet to say its one ending line; a row
# a crashed or replaced process left behind must not speak forever.
GUIDE_TERMINAL_STATUSES = ("done", "stopped", "abandoned", "superseded")
GUIDE_STALE_SECONDS = 600.0

_lock = threading.Lock()
_last_deep_capture_at = None  # monotonic timestamp of the last deep walk
_session = None           # latest guide dict
_thread = None
_stop_flag = False
_summon_pending = None    # companion chip click waiting for the desktop bar
_claim_chip_enabled = False
_watch = None             # the one active watch row (v1 is single-watch)
#: Id of the top-level ``watch_hit`` THIS process published, kept so a sticky
#: row's hit can be reconciled away when another process acknowledges it.
_watch_hit_id = None
_watch_ring = []          # newest WATCH_RING events, oldest first
# Re-entrant: the stdout reader mutates the row, a status poll snapshots it,
# and _write_state takes it again from inside those paths.
_watch_lock = threading.RLock()
# One start at a time: two overlapping starts could otherwise nest provisional
# rows and resurrect a start that already failed.
_watch_start_lock = threading.Lock()


def init(data_dir):
    global DATA_DIR, _watch_hit_id
    if not data_dir:
        raise ValueError("butler engine: init() requires a data_dir")
    DATA_DIR = data_dir
    # A fresh engine has published no hit announcement yet; the shared file
    # remains the source of truth for one that another process wrote.
    _watch_hit_id = None
    os.makedirs(os.path.join(str(data_dir), "guides"), exist_ok=True)
    # The companion is a long-lived shared transport; route its click events
    # back into the same state snapshot the desktop already polls.
    _OVERLAY.on_event = _on_overlay_event


def _guides_dir():
    if not DATA_DIR:
        raise RuntimeError("butler engine: init() not called")
    return os.path.join(str(DATA_DIR), "guides")


def _cfg():
    cfg = {}
    try:
        with open(os.path.join(os.path.dirname(_guides_dir()), "config.json")) as f:
            cfg = json.load(f)
    except Exception:
        pass
    return cfg


def poll_seconds():
    return float(_cfg().get("poll_seconds", DEFAULT_POLL))


def step_timeout():
    return float(_cfg().get("step_timeout_seconds", DEFAULT_TIMEOUT))


def stall_auto_stop_seconds():
    """How long a stalled guide may stay stalled before it stops itself."""
    return float(_cfg().get("stall_auto_stop_seconds", DEFAULT_STALL_AUTO_STOP))


#: Back-compat spelling for callers that follow step_timeout()'s short name.
def stall_auto_stop():
    return stall_auto_stop_seconds()


def _evaluate_step(step, snap):
    """Match a step's find and evaluate its expect against ONE snapshot.

    Both halves of a step must see the same AX walk: a find match from one
    capture and an expect verdict from another can disagree mid-navigation.
    """
    find = step.get("find") or {}
    label_needle = find.get("label")
    best = None
    if label_needle:
        # The app filter already ran (``_scope_capture``); the window hint only
        # PREFERS rows, so a control in a menu/sheet of the same app still wins.
        elements = _prefer_window(snap.get("elements", []),
                                  _step_window_hint(step),
                                  snap.get("window"))
        best = bp_capture.best_match(elements, label_needle,
                                     find.get("role"))
    verdict = bp_capture.expect_pending(step.get("expect") or {}, snap, best)
    return label_needle, best, verdict


def _step_needs_deep(step, best, verdict):
    """True when the shallow walk could not satisfy the current step."""
    find = step.get("find") or {}
    if find.get("label") and best is None:
        return True
    # An empty expect is a find-only step: expect_pending reports no_expect_given,
    # which is not a reason to pay for a deep walk.
    return bool(step.get("expect")) and not verdict.get("verified")


def _deep_capture_allowed():
    if _last_deep_capture_at is None:
        return True
    return (time.monotonic() - _last_deep_capture_at) >= DEEP_CAPTURE_INTERVAL


def _deep_capture():
    """One deep AX walk, rate-limited to one per DEEP_CAPTURE_INTERVAL."""
    global _last_deep_capture_at
    _last_deep_capture_at = time.monotonic()
    return bp_capture.capture(max_elements=DEEP_MAX_ELEMENTS, depth=DEEP_DEPTH)


def _is_hermes_app(value):
    """True when an app name is the Hermes desktop app (case-insensitive)."""
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    return bool(text) and any(text == name.strip().lower()
                              for name in HERMES_APP_NAMES)


def _is_hermes_window_title(value):
    """True when a window title is the Hermes main window title."""
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    return bool(text) and any(text == title.strip().lower()
                              for title in HERMES_MAIN_WINDOW_TITLES)


# --- guide scope: a step may only match inside the app it is about ----------
#
# The shipped guard only blocked the Hermes window. The general rule is wider:
# a step about System Settings must never match anything captured from another
# app, and the Hermes case is one instance of it. Every step therefore resolves
# a scope app (find.app, else expect.app, else the nearest earlier step that
# names one). An unresolved step keeps the old unscoped behavior — the scope is
# never invented from a title or a capture.

def _clean_app_name(value):
    """A non-empty app name from a step or a capture, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _step_declared_app(step):
    """The app a single step names for its own scope, or None.

    ``find.app`` wins over ``expect.app``: locating is what the step is about.
    ``expect.app_frontmost`` is accepted as the same explicit declaration (it
    names an app, it does not infer one), so a "Focus System Settings" step is
    scoped even though its only app key is the focus one.
    """
    if not isinstance(step, dict):
        return None
    for key in ("find", "expect"):
        source = step.get(key)
        if not isinstance(source, dict):
            continue
        for app_key in ("app", "app_frontmost"):
            app = _clean_app_name(source.get(app_key))
            if app:
                return app
    return None


def _resolve_scope_app(session, idx):
    """The app the step at ``idx`` may match inside, or None (unscoped).

    The step's own declared app wins; otherwise it inherits the NEAREST EARLIER
    step that resolves to one, so a guide that says "System Settings" once keeps
    the following app-less steps there. Nothing is inferred from a title, a say
    line or a capture.
    """
    steps = session.get("steps") if isinstance(session, dict) else None
    if not isinstance(steps, list) or not steps:
        return None
    try:
        start = int(idx)
    except (TypeError, ValueError):
        return None
    start = min(max(start, 0), len(steps) - 1)
    for back in range(start, -1, -1):
        app = _step_declared_app(steps[back])
        if app:
            return app
    return None


def _record_scope_app(session, idx):
    """Publish ``session["scope_app"]`` for the current step.

    An unresolved step records ``None`` (the guide then matches unscoped,
    exactly as before) and journals ``guide-scope:unresolved`` ONCE per guide,
    so the log says the guard is off instead of implying every capture was
    checked against an app.
    """
    scope = _resolve_scope_app(session, idx)
    session["scope_app"] = scope
    if scope is None and not session.get("scope_unresolved_journaled"):
        session["scope_unresolved_journaled"] = True
        try:
            bp_overlay.journal_click("guide-scope:unresolved")
        except Exception:  # noqa: BLE001 - diagnosis must not break a poll
            pass
    return scope


def _app_names_match(expected, actual):
    """True when both names denote the same app.

    Case-insensitive, and the scope name may be a whole-word segment of the
    capture's ``NSRunningApplication.localizedName`` ("Settings" vs "System
    Settings"), the same spelling latitude ``expect.app`` already gives. WORD
    boundaries only: a hard filter that accepted any substring would let scope
    "Mail" match a capture of "Gmail" and ring the wrong app. The reverse
    (scope longer than the capture) is not accepted either: a broad capture
    name must not widen the scope.
    """
    left = _clean_app_name(expected)
    right = _clean_app_name(actual)
    if not left or not right:
        return False
    wanted = left.lower().split()
    have = right.lower().split()
    if not wanted or len(wanted) > len(have):
        return False
    if wanted == have:
        return True
    return any(have[i:i + len(wanted)] == wanted
               for i in range(len(have) - len(wanted) + 1))


def _snapshot_app(snap):
    """The app a capture actually carries, or None when it carries none.

    The companion reports ``app`` PER SNAPSHOT; an older/synthetic capture may
    only carry per-row ``app`` values, so the first declared one stands in. A
    capture with no app information at all returns None and fails closed.
    """
    if not isinstance(snap, dict):
        return None
    app = _clean_app_name(snap.get("app"))
    if app:
        return app
    elements = snap.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if isinstance(element, dict):
                app = _clean_app_name(element.get("app"))
                if app:
                    return app
    return None


def _element_app(element, snap_app):
    """The app of one captured row: its own ``app`` wins over the snapshot's."""
    app = _clean_app_name(element.get("app")) if isinstance(element, dict) else None
    return app or snap_app


def _snapshot_for_app(snap, scope_app):
    """Hard-filter captured rows to ``scope_app`` before ``best_match``.

    The companion reports the app PER SNAPSHOT (``app`` at the top level, one
    walk of one app) and every row inherits it; an older/synthetic capture may
    carry a per-row ``app``, which wins. A row that claims another app is
    dropped, so a mixed capture can never ring a control from a different app.
    """
    elements = snap.get("elements") if isinstance(snap, dict) else None
    if not isinstance(elements, list) or not elements:
        return snap
    snap_app = _snapshot_app(snap)
    kept = [element for element in elements
            if not isinstance(element, dict)
            or _app_names_match(scope_app, _element_app(element, snap_app))]
    if len(kept) == len(elements):
        return snap
    filtered = dict(snap)
    filtered["elements"] = kept
    filtered["count"] = len(kept)
    # Keep the diagnostics honest: the counters describe the rows that reach
    # best_match/expect_pending, not the raw walk.
    filtered["with_box"] = sum(
        1 for element in kept
        if isinstance(element, dict)
        and isinstance(element.get("box"), (list, tuple))
        and len(element["box"]) == 4
        and element["box"][0] is not None)
    return filtered


def _scope_capture(session, idx, snap):
    """Return the app-scoped snapshot, or None when the capture is another app.

    None means "this step happens in ``scope_app`` and the capture is not it":
    nothing in the capture may be matched or rung, the step must not advance,
    and the user is told which app to switch to. A snapshot with no app at all
    fails closed (it cannot prove it is the right app).
    """
    scope = _record_scope_app(session, idx)
    if not scope:
        return snap
    if not _app_names_match(scope, _snapshot_app(snap)):
        return None
    return _snapshot_for_app(snap, scope)


def _step_window_hint(step):
    """The window title a step PREFERS (find.window first, then expect.window)."""
    if not isinstance(step, dict):
        return None
    for key in ("find", "expect"):
        source = step.get(key)
        value = source.get("window") if isinstance(source, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _element_window(element, snap_window):
    """One row's window title: its own ``window`` wins over the snapshot's."""
    value = element.get("window") if isinstance(element, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return snap_window if isinstance(snap_window, str) and snap_window.strip() else None


def _prefer_window(elements, window_hint, snap_window=None):
    """Rank rows whose window equals ``window_hint`` first — PREFERENCE ONLY.

    Rows of the same app in another window stay eligible: a menu, sheet or
    alert is a different window of the same app, and menu steps must keep
    verifying. best_match's scores still decide; this order only breaks ties,
    so the scope stays the app and never hardens into the window title.
    """
    if not window_hint or not isinstance(elements, list):
        return elements
    hint = window_hint.strip().lower()
    if not hint:
        return elements
    preferred, others = [], []
    for element in elements:
        window = _element_window(element, snap_window)
        if window and window.lower() == hint:
            preferred.append(element)
        else:
            others.append(element)
    return preferred + others


def _step_app_names(step):
    """The app names a single step explicitly targets.

    Both halves count: ``find.app`` names the app the step locates a control
    in, ``expect.app``/``expect.app_frontmost`` the app it verifies. A step
    that names Hermes in either place IS a Hermes step, so the round-24 guard
    must not fall back to its title heuristic and block it.
    """
    if not isinstance(step, dict):
        return []
    names = []
    for source_key in ("find", "expect"):
        source = step.get(source_key)
        if not isinstance(source, dict):
            continue
        for key in ("app", "app_frontmost"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
    return names


def _guide_names_hermes_without_apps(session):
    """True when a plan with no app expectations is about the Hermes app.

    This is the narrow fallback for a hands-off guide ("Open Hermes
    settings"): with no ``expect.app`` anywhere, the title/instruction/find
    label is the only statement of intent. When ANY step verifies something
    (a window, a visible control) that does not name Hermes, a title mention is
    the SUBJECT of the task — the measured "Allow Screen Recording for Hermes"
    expects System Settings — and the plan is not a Hermes plan.
    """
    steps = session.get("steps") if isinstance(session, dict) else None
    steps = steps if isinstance(steps, list) else []
    if any(_step_app_names(step) for step in steps):
        # An explicit app expectation decides per step; never guide-wide.
        return False
    expect_texts = []
    any_expect = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        expect = step.get("expect") if isinstance(step.get("expect"), dict) else {}
        if expect:
            any_expect = True
        expect_texts.extend(value for value in expect.values()
                            if isinstance(value, str))
    if any("hermes" in text.lower() for text in expect_texts):
        return True
    if any_expect:
        return False
    texts = [session.get("title")]
    for step in steps:
        if not isinstance(step, dict):
            continue
        texts.append(step.get("say"))
        find = step.get("find") if isinstance(step.get("find"), dict) else {}
        texts.append(find.get("label"))
    return any(isinstance(text, str) and "hermes" in text.lower()
               for text in texts)


def _guide_names_app_hermes(session):
    """True when any step explicitly targets Hermes as its app."""
    steps = session.get("steps") if isinstance(session, dict) else None
    for step in steps if isinstance(steps, list) else []:
        if any(_is_hermes_app(name) for name in _step_app_names(step)):
            return True
    return False


def _guide_hermes_guard_applies(session, step):
    """True when a Hermes capture must not be matched for THIS step.

    Per-step, not guide-wide: a mixed plan (step 1 System Settings, step 2
    Hermes) still protects step 1, because a step that names its own app is
    judged only by that app. An app-less step inherits the guide's intent: a
    plan that names Hermes as an app anywhere keeps its app-less steps
    working; a hands-off plan is judged by its title/instruction.
    """
    names = _step_app_names(step)
    if any(_is_hermes_app(name) for name in names):
        return False
    if names:
        # This step explicitly targets another app; the guide-wide intent must
        # not unguard it (the measured mixed-guide regression).
        return True
    if _guide_names_app_hermes(session):
        return False
    return not _guide_names_hermes_without_apps(session)


def _guide_target_app(session, step):
    """The app a blocked Hermes step is really about, for the switch note.

    Precedence: this step's declared app (``find.app``, else ``expect.app``),
    then the last app the guide verified, then the first step that names an
    app, else None (the caller says "the app the step is about").
    """
    names = _step_app_names(step)
    if names:
        return names[0]
    if isinstance(session, dict):
        last = session.get("last_verified_app")
        if isinstance(last, str) and last.strip():
            return last.strip()
        for other in session.get("steps") or []:
            other_names = _step_app_names(other)
            if other_names:
                return other_names[0]
    return None


def _snapshot_without_hermes_window(snap):
    """Drop captured rows that belong to the Hermes main window.

    A capture's elements normally only carry role/label/box, so this is the
    second layer of the Hermes guard: when a row DOES claim its app/window
    (older helpers, synthetic captures), the chat transcript must never reach
    ``best_match``. Missing per-element app/window fall back to the capture's
    own, which is what every row in a single-app walk belongs to.
    """
    elements = snap.get("elements") if isinstance(snap, dict) else None
    if not isinstance(elements, list) or not elements:
        return snap
    kept = []
    dropped = False
    snap_app = snap.get("app")
    snap_window = snap.get("window")
    for element in elements:
        if not isinstance(element, dict):
            kept.append(element)
            continue
        app = element.get("app")
        window = element.get("window")
        if app is None:
            app = snap_app
        if window is None and _is_hermes_app(app):
            window = snap_window
        if _is_hermes_app(app) and _is_hermes_window_title(window):
            dropped = True
            continue
        kept.append(element)
    if not dropped:
        return snap
    filtered = dict(snap)
    filtered["elements"] = kept
    filtered["count"] = len(kept)
    # Keep the diagnostics honest: both counters describe the rows that reach
    # best_match/expect_pending, not the raw walk.
    filtered["with_box"] = sum(
        1 for element in kept
        if isinstance(element, dict)
        and isinstance(element.get("box"), (list, tuple))
        and len(element["box"]) == 4
        and element["box"][0] is not None)
    return filtered


def _guard_capture(session, step, snap):
    """Return a screenshot safe to match for this step, or None when blocked.

    None means "the frontmost capture is the Hermes window and this step is
    about another app": nothing in that window may be rung, and the user is
    told to switch. A guide that legitimately targets Hermes never blocks.
    """
    if not _guide_hermes_guard_applies(session, step):
        return snap
    if _is_hermes_app(snap.get("app")):
        return None
    return _snapshot_without_hermes_window(snap)


def _box_value(value):
    """A finite float from an AX scalar, or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return None if number != number else number


def _visible_screen_frames(snapshot=None):
    """Visible frames ``(x, y, w, h)`` reported by the capture, best-effort.

    A companion snapshot may carry them under ``screens``; when it does not,
    the module-level ``SCREEN_VISIBLE_FRAMES`` is the testable escape hatch.
    An empty result means the layout is unknown (no off-screen guess is made
    beyond the degenerate-box sentinel).
    """
    candidates = []
    if isinstance(snapshot, dict):
        raw = snapshot.get("screens")
        if isinstance(raw, (list, tuple)):
            candidates.extend(raw)
    if not candidates:
        candidates.extend(SCREEN_VISIBLE_FRAMES)
    frames = []
    for item in candidates:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            continue
        values = [_box_value(value) for value in item]
        if any(value is None for value in values):
            continue
        x, y, width, height = values
        if width <= 0.0 or height <= 0.0:
            continue
        frames.append((x, y, width, height))
    return frames


def _box_is_offscreen(box, snapshot=None):
    """True when a matched box is degenerate or anchored outside every screen.

    The degenerate case is the measured defect: a sidebar row scrolled below
    the fold reports ``[0, 832, 0, 0]``, which the companion cannot ring, so a
    bare highlight used to land at the origin with no explanation.
    """
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    values = [_box_value(value) for value in box]
    if any(value is None for value in values):
        return False
    x, y, width, height = values
    if width <= DEGENERATE_BOX_MAX or height <= DEGENERATE_BOX_MAX:
        return True
    frames = _visible_screen_frames(snapshot)
    if not frames:
        return False
    return not any(fx <= x <= fx + fw and fy <= y <= fy + fh
                   for fx, fy, fw, fh in frames)


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0", ""):
            return False
    return default


def pet_click_claim():
    """Whether the desktop may listen for pet clicks to summon the butler.

    Installing the plugin is the opt-in, so a missing key means on. The key is
    still read from plugin data on every call (poll_seconds pattern) so an
    explicit ``false`` — the manual escape hatch — hot-reloads without a
    gateway restart.
    """
    cfg = _cfg()
    # Nested first, then the flat dotted spelling — the same precedence the
    # dashboard API uses. They used to disagree, which only mattered once both
    # keys were present with different values; with the default now ON a
    # disagreement would hide or show the chip inconsistently between processes.
    summon = cfg.get("summon")
    value = summon.get("pet_click_claim") if isinstance(summon, dict) else None
    if value is None:
        value = cfg.get("summon.pet_click_claim")
    if value is None:
        return DEFAULT_PET_CLICK_CLAIM
    # A stored off spelling ("false"/"off"/"no"/"0") disarms; anything
    # unreadable falls back to the default, so junk cannot hide the chip.
    return _as_bool(value, DEFAULT_PET_CLICK_CLAIM)


def _normalize_step(step):
    """Accept the documented shape, or a flat-key typo of it.

    Returns a clean step dict, or None when the step can never verify.
    Flat keys like "find.label" / "expect.app" are folded into nested dicts so
    a malformed call degrades into the step it meant to be instead of a
    KeyError half-way through startup, leaving an orphan guide running.
    """
    if not isinstance(step, dict):
        return None
    say = step.get("say")
    if not isinstance(say, str) or not say.strip():
        return None
    expect: dict = dict(step.get("expect") or {})
    find: dict = dict(step.get("find") or {})
    for key, value in step.items():
        if key.startswith("expect."):
            expect[key[len("expect."):]] = value
        elif key.startswith("find."):
            find[key[len("find."):]] = value
    clean: dict = {"say": say.strip()}
    label = find.get("label")
    clean_find: dict = {}
    if isinstance(label, str) and label.strip():
        clean_find["label"] = label.strip()
        if isinstance(find.get("role"), str) and find["role"].strip():
            clean_find["role"] = find["role"].strip()
    # find.app is the step's scope and find.window its preferred window; the
    # normalizer used to keep only label/role, which silently threw the scope
    # away and left the step matching anything it saw.
    for key in ("app", "window"):
        value = find.get(key)
        if isinstance(value, str) and value.strip():
            clean_find[key] = value.strip()
    if clean_find:
        clean["find"] = clean_find
    # No key whitelist here: expect keys evolve (app/visible/window/focus...)
    # and silently dropping an unknown but valid key starves a step of its
    # verification (expect=no_expect_given). Anything present passes through,
    # invalid keys simply never match in expect_pending.
    kept_expect = {k: str(v) for k, v in expect.items() if v}
    if kept_expect:
        clean["expect"] = kept_expect
    # A step the butler can neither locate nor verify is a dead step. Scope and
    # window keys do not count as locating: they scope an existing find.label.
    if not (clean_find.get("label") or clean.get("expect")):
        return None
    return clean


def _normalize_steps(args_steps):
    steps = []
    for st in args_steps:
        clean = _normalize_step(st)
        if clean is not None:
            steps.append(clean)
    return steps


def _guide_last_update(guide):
    """Newest epoch second on a guide block, or None when it carries none.

    ``stopped_at`` is the stamp a clean stop writes; older/foreign blocks may
    only have a start or an update time, so every known stamp is considered and
    the newest wins (a last verification is an update too).
    """
    if not isinstance(guide, dict):
        return None
    state = guide.get("state") if isinstance(guide.get("state"), dict) else {}
    stamps = []
    for row in (state, guide):
        for key in ("stopped_at", "updated_at", "ended_at", "generated_at",
                    "step_opened_at", "started_at"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stamps.append(float(value))
    for row in guide.get("verifications") or []:
        if isinstance(row, dict):
            value = row.get("verified_at")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stamps.append(float(value))
    return max(stamps) if stamps else None


def guide_block_is_stale(guide, now=None, updated_at=None):
    """True when ``guide`` is a TERMINAL block older than the sweep age.

    Only terminal blocks are swept: a running/capture_failed/stalled guide is
    live status whatever its age (its own stall deadline stops it). The block's
    own newest stamp (``stopped_at`` first) governs; the envelope's
    ``generated_at`` is only a fallback for a block that carries no stamp at
    all. It must NOT extend an old block's life: an ``abandoned`` row can stay
    in memory while later state writes republish the envelope, and a block with
    no timestamp is not swept — there is no evidence it is stale.
    """
    if not isinstance(guide, dict):
        return False
    state = guide.get("state") if isinstance(guide.get("state"), dict) else {}
    status = state.get("status") or guide.get("status")
    if status not in GUIDE_TERMINAL_STATUSES:
        return False
    stamp = _guide_last_update(guide)
    if stamp is None and isinstance(updated_at, (int, float)) \
            and not isinstance(updated_at, bool):
        stamp = float(updated_at)
    if stamp is None:
        return False
    return (time.time() if now is None else now) - stamp > GUIDE_STALE_SECONDS


def _write_state(clear_summon=False, clear_watch=False, watch_hit=None,
                 clear_watch_hit=False, clear_sticky_hit=False):
    """Publish the shared snapshot. Never raises.

    Two processes host this engine: the agent runtime (guides) and the plugin
    API that owns the companion (the summon row from a chip click). Each owns
    part of ``state.json``, so a write merges the other owner's block instead of
    dropping it — otherwise a click silently erases a running guide, and a guide
    poll erases a click the desktop has not consumed yet. The ambient ``watch``
    block merges the same way (stop_watch clears it explicitly). A terminal
    guide block older than ``GUIDE_STALE_SECONDS`` is not carried forward: the
    guide stays in history, but the live-status row stops talking.

    The top-level ``watch_hit`` announcement OUTLIVES the watch row: a hit is
    published here beside ``watch``/``say``/``summon_pending`` and is carried
    forward like the other shared rows until an acknowledgement clears it
    (``clear_watch_hit``). It is deliberately not held in memory, so an ack in
    any process removes it for every other writer instead of it being
    republished by the one that saw the hit.
    """
    global _watch_hit_id
    if not DATA_DIR:
        return
    path = os.path.join(str(DATA_DIR), "state.json")
    snap = {}
    if _summon_pending is not None:
        snap["summon_pending"] = dict(_summon_pending)
    with _watch_lock:
        watch_snap = _watch_snapshot()
    if watch_snap is not None:
        snap["watch"] = watch_snap
    explicit_hit = watch_hit is not None or clear_watch_hit
    if watch_hit is not None:
        snap["watch_hit"] = dict(watch_hit)
    if _session is not None:
        snap.update({
            "guide": _session,
            "overlay": _session.get("overlay", "unavailable"),
            "generated_at": time.time(),
        })
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            if _session is None and "guide" in existing \
                    and not guide_block_is_stale(
                        existing.get("guide"),
                        updated_at=existing.get("generated_at")):
                snap["guide"] = existing["guide"]
                for key in ("overlay", "generated_at"):
                    if key in existing:
                        snap[key] = existing[key]
            if not clear_summon and _summon_pending is None \
                    and "summon_pending" in existing:
                snap["summon_pending"] = existing["summon_pending"]
            # A guide poll in the agent process must not erase a watch row the
            # plugin API process published (and vice versa); only stop_watch
            # clears it, via clear_watch.
            if not clear_watch and watch_snap is None and "watch" in existing:
                watch_row = existing["watch"]
                # An ack in a process that does not own the row can still clear
                # a sticky hit left by a dead owner; a live foreign owner is
                # left alone (its own write reconciles it).
                if clear_sticky_hit and isinstance(watch_row, dict) \
                        and "hit" in watch_row:
                    watch_row = dict(watch_row)
                    watch_row["hit"] = None
                snap["watch"] = watch_row
            # A sticky hit acknowledged by ANOTHER process leaves the top-level
            # block gone (or acknowledged) while this process still holds
            # row.hit in memory. Reconcile on the next write: return the row to
            # the change count and re-arm the duplicate guard for the next hit.
            if not explicit_hit and watch_snap is not None \
                    and _watch_hit_id is not None:
                existing_hit = existing.get("watch_hit")
                acked_elsewhere = not isinstance(existing_hit, dict) \
                    or existing_hit.get("acknowledged") is True
                with _watch_lock:
                    if acked_elsewhere and _watch is not None \
                            and _watch.get("once") is False \
                            and _watch.get("hit") is not None \
                            and not _watch.get("stopped"):
                        _watch["hit"] = None
                        _watch_hit_id = None
                        watch_snap = _watch_snapshot()
                        snap["watch"] = watch_snap
            # Preserve the outliving hit row unless THIS write sets or clears it.
            if not explicit_hit and "watch_hit" in existing:
                snap["watch_hit"] = existing["watch_hit"]
    except Exception:
        pass
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _on_overlay_event(event):
    """A companion window event: queue the matching summon for the desktop.

    Two doors share this row. ``summon_text`` carries text typed in the local
    ask field, so the desktop posts it straight to the Butler chat and never
    opens its bar; its ``destination`` says whether that is the newest chat
    (``recent``) or a freshly minted one (``new``). ``look_text`` is the same
    text row plus the element the ⌘⇧L hit-test found, so the desktop can frame
    the question around that control. The legacy ``claim_click`` (older
    companion binaries) stays text-less and keeps the old "open the bar"
    behaviour.
    """
    global _summon_pending
    if not isinstance(event, dict):
        return
    op = event.get("op")
    if op == "watch_ack":
        # The hat's acknowledgement button rides the same callback as a click.
        # overlay.py normally routes it straight to ack_watch(); accepting it
        # here too keeps an older transport honest.
        ack_watch()
        return
    if op in ("watch-event", "watch-hit", "watch-stopped", "watch-scan"):
        # Ambient watch lines are not summons: they feed the ring/state and the
        # condition announcement, never the summon row.
        _handle_watch_message(event)
        return
    if op == "summon_text":
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        pending_id = event.get("id")
        if not isinstance(pending_id, str) or not pending_id.strip():
            pending_id = uuid.uuid4().hex[:8]
        # The ask field chooses per send: ⌘↵ starts a fresh chat, ↵ continues
        # the newest one. Anything else is the safe default, never an error.
        destination = "new" if event.get("destination") == "new" else "recent"
        _summon_pending = {
            "origin": "chip",
            "id": pending_id.strip(),
            "text": text,
            "destination": destination,
            "at": time.time(),
        }
        # Hop-level breadcrumb for support: companion → engine → desktop.
        bp_overlay.journal_click("engine-pending:text")
        _write_state()
        return
    if op == "look_text":
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        pending_id = event.get("id")
        if not isinstance(pending_id, str) or not pending_id.strip():
            pending_id = uuid.uuid4().hex[:8]
        destination = "new" if event.get("destination") == "new" else "recent"
        # The hit-tested element travels with the question so the desktop can
        # frame the turn; keep every key the companion managed to read.
        target = event.get("target")
        _summon_pending = {
            "origin": "look",
            "id": pending_id.strip(),
            "text": text,
            "target": dict(target) if isinstance(target, dict) else {},
            "destination": destination,
            "at": time.time(),
        }
        bp_overlay.journal_click("engine-pending:look")
        _write_state()
        return
    if op != "claim_click":
        return
    _summon_pending = {
        "origin": "pet",
        "id": uuid.uuid4().hex[:8],
        "at": time.time(),
    }
    _write_state()


def summon_pending():
    """The pending companion-click summon (or None), for tests/diagnostics."""
    return dict(_summon_pending) if _summon_pending is not None else None


def consume_summon_pending(pending_id=None):
    """Clear the pending summon after the desktop opened its bar (idempotent)."""
    global _summon_pending
    if _summon_pending is None:
        return False
    if pending_id and _summon_pending.get("id") != pending_id:
        return False
    _summon_pending = None
    # Explicit clear: the merge in _write_state must not resurrect the row we
    # just consumed.
    _write_state(clear_summon=True)
    return True


def _clip_say_text(text, limit=SAY_MAX_CHARS):
    """Trim an answer to the bubble cap, cutting on a word boundary.

    The stored text fits ``limit`` INCLUDING the ellipsis. A single unbroken
    token (a URL, a hash) has no boundary to cut on, so it keeps the hard cut
    rather than being dropped.
    """
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: max(0, limit - 1)]
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"), head.rfind("\r"))
    if cut > 0:
        head = head[:cut]
    return head.rstrip() + SAY_ELLIPSIS


def say(text):
    """Publish a short answer for the pet to speak once, beside the pointer.

    The agent calls this after a LOOK question: the short answer belongs on
    screen, the full explanation stays in the chat. The row is written to
    ``say.json`` atomically (id/text/at); ``/state`` merges it on every read.
    Never raises — a lost bubble must not fail the agent's turn.
    """
    try:
        shown = _clip_say_text(text)
        if not shown:
            return {"ok": False, "error": "text is required"}
        if not DATA_DIR:
            return {"ok": False, "error": "butler engine: init() not called"}
        row = {"id": uuid.uuid4().hex[:8], "text": shown, "at": time.time()}
        path = os.path.join(str(DATA_DIR), "say.json")
        # A unique temp name: two processes (two sessions, a CLI call) may write
        # the answer at the same moment. A shared "say.json.tmp" could interleave
        # two small writes and publish unparsable JSON while both report ok.
        tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(row, f, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return {"ok": True, "shown": shown}
    except Exception as exc:  # noqa: BLE001 - the chat answer is primary
        return {"ok": False, "error": str(exc)}


def _clear_watch_state():
    """Drop the in-memory watch and its state row (call with the lock held)."""
    global _watch
    _watch = None
    _watch_ring.clear()
    _write_state(clear_watch=True)


def _pid_alive(pid):
    """True when *pid* is a live process, by the signal-0 probe. Never raises."""
    try:
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_state_dict():
    """The shared state snapshot, or {} when it does not exist/is unreadable."""
    if not DATA_DIR:
        return {}
    try:
        with open(os.path.join(str(DATA_DIR), "state.json"),
                  encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _read_state_watch_row():
    """The watch row another process published in state.json, or None."""
    row = _read_state_dict().get("watch")
    return row if isinstance(row, dict) else None


def _read_state_watch_hit():
    """The outliving hit announcement in state.json, or None."""
    row = _read_state_dict().get("watch_hit")
    return row if isinstance(row, dict) else None


def _foreign_owner_alive(owner):
    """True when another LIVE process owns the row (never our own pid)."""
    try:
        owner = int(owner)
    except (TypeError, ValueError, OverflowError):
        return False
    return owner != os.getpid() and _pid_alive(owner)


def _watch_snapshot():
    """Copy the watch row for state.json (call with ``_watch_lock`` held)."""
    if _watch is None:
        return None
    row = dict(_watch)
    row["recent"] = [dict(event) for event in (_watch.get("recent") or [])]
    if isinstance(row.get("scan"), dict):
        # The scan counters are mutated by the tick handler; the snapshot is
        # serialized outside the lock, so it gets its own copy.
        row["scan"] = dict(row["scan"])
    return row


def _watch_event_row(event):
    """Whitelist one companion event into the ring/state shape (JSON-safe)."""
    row = {}
    for key in ("seq", "at", "kind", "app", "role", "label", "window", "old", "new"):
        value = event.get(key)
        if value is not None:
            row[key] = value
    row.setdefault("at", time.time())
    row.setdefault("kind", "unknown")
    return row


def _watch_clock(value):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "--:--:--"


def _watch_event_line(event):
    line = "%s %s %s" % (_watch_clock(event.get("at")),
                         event.get("kind") or "?",
                         event.get("role") or "?")
    label = event.get("label")
    if label:
        line += ' "%s"' % label
    window = event.get("window")
    if window:
        line += " — %s" % window
    return line


def _watch_scan_line(scan, row, hit):
    """One status line for the content scan: matcher, roles, baseline, ticks.

    ``baseline`` is the number of match signatures present at arm; 0 means the
    app exposes no author-role markup, so the scan can only ever come up empty
    (say so instead of guessing that a message arrived).
    """
    author = scan.get("author")
    if isinstance(author, str) and author:
        matcher = 'author "%s"' % author
        roles = ",".join(scan.get("roles") or []) or "?"
    else:
        matcher = 'text "%s"' % (scan.get("text") or "")
        # The text matcher is deliberately role-agnostic: that is exactly why
        # it is the weaker mode, and "any" is the honest denominator.
        roles = "any"
    baseline = row.get("scan_baseline")
    if not isinstance(baseline, int):
        baseline = scan.get("baseline")
    try:
        baseline = max(0, int(baseline or 0))
    except (TypeError, ValueError):
        baseline = 0
    try:
        ticks = int(row.get("scan_ticks") or 0)
    except (TypeError, ValueError):
        ticks = 0
    # Until the companion acks, the arm-time count is unknown: "0" would read
    # as "this app has no author-role markup", which is a different claim.
    baseline_text = str(baseline) if row.get("scan_baseline_at") else "?"
    return "scan: %s · roles %s · baseline %s · %d ticks · %s" % (
        matcher, roles, baseline_text, ticks, "hit" if hit else "no hit")


def _append_watch_jsonl(event):
    """One JSON line per event in <plugin data dir>/watch.jsonl, tail-capped.

    A watch can outlive the process that started it (gateway restart, agent
    turn ends), so the file — not the in-memory ring — is the durable log. The
    tail is rewritten once it grows past the cap; never raises.
    """
    if not DATA_DIR:
        return
    path = os.path.join(str(DATA_DIR), "watch.jsonl")
    try:
        line = json.dumps(event, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        if len(lines) > WATCH_JSONL_MAX:
            tail = lines[-WATCH_JSONL_MAX:]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(tail) + "\n")
            os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - logging must never break the watch
        pass


def _handle_watch_message(event):
    """Route one companion watch line into the ring, state row, and the pet.

    Called from the companion's stdout reader thread. Never raises.
    """
    global _watch_hit_id
    op = event.get("op")
    stop_row = None
    with _watch_lock:
        if _watch is None:
            # A stale line from a companion the agent already stopped: drop it
            # rather than resurrecting a row nobody asked for.
            return
        # A line from the watch this row replaced arrives AFTER the replacement
        # was requested: its token never matches, so it can neither mark the new
        # watch stopped nor count its events. (A tokenless line is an older
        # companion and is accepted.)
        token = event.get("token")
        row_token = _watch.get("token")
        if token is not None and token != row_token:
            # (A tokenless line is an older companion and is still accepted;
            # a tokened line against a tokenless row is foreign by construction.)
            return
        if op != "watch-stopped" and _watch.get("stopped"):
            # A stopped watch is inert: a straggler already in the pipe must not
            # count an event (or announce a hit) for a watch that is over.
            return
        if op == "watch-scan":
            # One accounting line per scan tick: the baseline size, how many
            # nodes matched this tick and how many of them are new. It is not
            # an event (the ring stays a change log), so only the scan counters
            # and the durable status move.
            scan = _watch.get("scan")
            if not isinstance(scan, dict):
                # Not a scan watch: a stray line cannot invent one.
                return
            for key in ("baseline", "matches", "new"):
                value = event.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    scan[key] = int(value)
            _watch["scan_ticks"] = int(_watch.get("scan_ticks") or 0) + 1
            _watch["scan_last_at"] = event.get("at") or time.time()
            # The companion's arm-time count is authoritative; keep it even
            # when a tick reports a shrinking current match count.
            baseline = scan.get("baseline")
            if isinstance(baseline, int) and baseline >= 0:
                _watch["scan_baseline"] = baseline
            _write_state()
            return
        if op == "watch-event":
            row = _watch_event_row(event)
            _watch_ring.append(row)
            del _watch_ring[:-WATCH_RING]
            _watch["events"] = int(_watch.get("events") or 0) + 1
            recent = _watch.setdefault("recent", [])
            recent.append(row)
            del recent[:-WATCH_STATE_RECENT]
            _append_watch_jsonl(row)
            _write_state()
            return
        if op == "watch-hit":
            if _watch.get("hit") is not None:
                # One announcement per arm, even if a duplicate line arrives.
                return
            label = event.get("label") or event.get("until") or ""
            app = _watch.get("app") or event.get("app") or ""
            # Rows this engine starts always carry the flag. A hand-built or
            # legacy row without it takes the product default: one-shot.
            once = _watch.get("once") is not False
            hit = {
                "label": label,
                "window": event.get("window"),
                "at": event.get("at") or time.time(),
            }
            # A content-scan hit names its matcher: "author" is a role-filtered
            # author node, "text" is the weaker label substring match. A text
            # hit is never dressed up as an author.
            kind = event.get("kind")
            if isinstance(kind, str) and kind:
                hit["kind"] = kind
            # WHICH surface matched: the companion knows, the agent should not
            # have to infer it from the event tail (a terminal's window title is
            # the command line; its value is the output).
            matched = event.get("matched")
            if isinstance(matched, str) and matched in WATCH_SURFACES:
                hit["matched"] = matched
            surfaces = event.get("baseline_had")
            if isinstance(surfaces, list):
                hit["baseline_had"] = [
                    surface for surface in surfaces
                    if isinstance(surface, str) and surface in WATCH_SURFACES]
            if not once:
                # Sticky mode keeps observing after the announcement; the hit
                # stays unacknowledged until the user (or the pane) says so.
                hit["acknowledged"] = False
            _watch["hit"] = hit
            # The ANNOUNCEMENT is a top-level row of its own: a one-shot hit
            # ends the watch row, and the pet must keep saying the hit until
            # the user acknowledges it (the row is the thing that outlives the
            # watch). Published in the same write as the terminal row below, so
            # /state never sees one without the other.
            watch_hit = {
                "app": app,
                "label": label,
                "window": hit.get("window"),
                "at": hit.get("at"),
                "acknowledged": False,
                "id": uuid.uuid4().hex[:8],
            }
            if hit.get("kind"):
                # Same matcher label on the outliving announcement, so the
                # desktop can say "author" vs a mere text match after the watch
                # row is gone.
                watch_hit["kind"] = hit["kind"]
            if hit.get("matched"):
                # The surface survives the ended watch row too: the status/desk
                # text still says which one fired.
                watch_hit["matched"] = hit["matched"]
            if hit.get("baseline_had") is not None:
                watch_hit["baseline_had"] = hit["baseline_had"]
            # Remember that THIS process published the top-level block, so a
            # later write can tell "acked elsewhere" from "never announced".
            _watch_hit_id = watch_hit["id"]
            # In place, with no model call and without waking the agent: the
            # pet says it now, the agent reads watch_status on its next turn.
            say("%s: %s appeared" % (app, label))
            if once:
                # A hit ends a one-shot watch: the request was "tell me when X
                # appears", and X appeared. The terminal row is published below
                # BEFORE the companion is told, so a /state poll can never see
                # `hit` without its ending; later lines are inert (the stopped
                # guard above).
                _watch["stopped"] = {
                    "reason": "hit",
                    "events": int(_watch.get("events") or 0),
                    "at": time.time(),
                    "token": _watch.get("token"),
                }
                stop_row = _watch
            _write_state(watch_hit=watch_hit)
        elif op == "watch-stopped":
            if _watch.get("stopped") is not None:
                # A watch ends ONCE. The terminal reason wins: in particular the
                # companion's reply to OUR one-shot watch_stop must not rewrite
                # "hit" as "stopped" (that would make the desktop say a second
                # ending line).
                return
            _watch["stopped"] = {
                "reason": event.get("reason") or "stopped",
                "events": event.get("events") or _watch.get("events"),
                "at": time.time(),
                "token": event.get("token"),
            }
            _write_state()
    if stop_row is not None:
        # Detach the observers OUTSIDE ``_watch_lock``: a request-style
        # transport can block for its whole timeout, and a synchronous test
        # double would otherwise re-enter this handler while we hold the lock.
        # Only send while this row is still the one we ended: a stop/start that
        # landed in between already owns the companion.
        with _watch_lock:
            still_ours = _watch is stop_row and stop_row.get("stopped") is not None
        if still_ours:
            _send_watch_stop()


def _watch_scan_number(value):
    """One scan number, or None when the payload is not numeric.

    A model may spell a number as a string ("3"); a bool is not a number here.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_watch_scan(scan):
    """Validate one ``scan`` object for the companion (None when absent).

    Returns ``(normalized, error)`` with ``error`` a tool-shaped dict, never an
    exception: a handler must always answer. The normalized keys are the wire
    protocol's (author|text, roles, interval, max_nodes), so what the row keeps
    for status/debug is exactly what the companion was asked to do.
    """
    if scan is None:
        return None, None
    if not isinstance(scan, dict):
        return None, {"error": "scan must be an object"}
    author = scan.get("author")
    author = author.strip() if isinstance(author, str) else ""
    text = scan.get("text")
    text = text.strip() if isinstance(text, str) else ""
    # One matcher only: author is the strong mode, text the explicitly weaker
    # one (a text hit is never reported as an author).
    if author and text:
        return None, {"error": "pass scan.author or scan.text, not both"}
    if not author and not text:
        return None, {"error": "scan needs author or text"}
    roles = scan.get("roles")
    if roles is None:
        roles = list(WATCH_SCAN_ROLES)
    elif isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, (list, tuple)):
        return None, {"error": "scan.roles must be a list of AX roles"}
    cleaned = []
    for role in roles:
        if not isinstance(role, str) or not role.strip():
            return None, {"error": "scan.roles must be a list of AX roles"}
        role = role.strip()
        if role not in cleaned:
            cleaned.append(role)
    if not cleaned:
        return None, {"error": "scan.roles must name at least one role"}
    raw_interval = scan.get("interval_seconds")
    if raw_interval is None:
        raw_interval = scan.get("interval")
    interval = WATCH_SCAN_INTERVAL_DEFAULT
    if raw_interval is not None:
        number = _watch_scan_number(raw_interval)
        if number is None:
            return None, {"error": "scan.interval_seconds must be a number"}
        interval = int(round(min(max(number, WATCH_SCAN_INTERVAL_MIN),
                                 WATCH_SCAN_INTERVAL_MAX)))
    raw_nodes = scan.get("max_nodes")
    max_nodes = WATCH_SCAN_NODES_DEFAULT
    if raw_nodes is not None:
        number = _watch_scan_number(raw_nodes)
        if number is None:
            return None, {"error": "scan.max_nodes must be a number"}
        max_nodes = int(round(max(1.0, min(number, WATCH_SCAN_NODES_MAX))))
    normalized = {"roles": cleaned, "interval": interval,
                  "max_nodes": max_nodes}
    if author:
        normalized["author"] = author
    else:
        normalized["text"] = text
    return normalized, None


def _watch_start_failure(app, code):
    """Map a companion rejection to an honest, exception-free tool result."""
    if code == "app-not-running":
        return {"error": "app-not-running", "app": app,
                "note": "no running app matches %r" % app}
    if code == "app-not-trusted":
        return {"error": "app-not-trusted", "app": app,
                "note": "Accessibility is not granted to the butler companion",
                "fix": bp_capture.GRANT_FIX}
    return {"error": code or "watch_start failed", "app": app}


def start_watch(app, until=None, scan=None, once=True):
    """Observe one running app's Accessibility tree; never clicks or types.

    Wholly passive: the companion attaches AX observers and streams change
    lines, so this is safe in the background and needs no new TCC grant (the
    companion is the existing single grant subject). One watch at a time in v1:
    starting a second replaces the first. Never raises — a missing app or a
    missing grant comes back as an error string. Every start carries a token
    echoed by the companion, so a late line from a replaced watch is dropped.

    ``once`` (default True) makes a condition hit END the watch: one
    announcement, a terminal ``stopped.reason == "hit"`` row, and no re-arm
    until the agent starts a new watch. ``once=False`` keeps observing after the
    hit until ``acknowledge_watch_hit`` (or a stop) clears it.

    ``scan`` is the content version of the same question: pass
    ``{"author": "Adolan"}`` (or the weaker ``{"text": ...}``) and the
    companion re-walks the app's AX tree on a timer, announcing a matching node
    that was NOT on screen when the watch armed. AX has no timestamps, so
    "newer than now" is "new since arming". ``until`` and ``scan`` are mutually
    exclusive: one matcher per watch.
    """
    with _watch_start_lock:
        return _start_watch_locked(app, until, once, scan)


def _start_watch_locked(app, until, once=True, scan=None):
    global _watch
    try:
        name = str(app or "").strip()
        if not name:
            return {"error": "app is required"}
        needle = until.strip() if isinstance(until, str) else None
        if not needle:
            needle = None
        if needle and scan is not None:
            # One matcher per watch: the event condition and the content scan
            # would compete for the single hit slot.
            return {"error": "pass until or scan, not both"}
        scan_spec, scan_error = _normalize_watch_scan(scan)
        if scan_error is not None:
            return scan_error
        # Be liberal with the flag a model may spell as a string; the schema
        # says boolean. One-shot is the default, so only an explicit false
        # spelling opts into sticky mode (an empty/unknown string must not).
        if isinstance(once, str):
            once = once.strip().lower() not in ("false", "no", "off", "0")
        else:
            once = True if once is None else bool(once)
        try:
            ok, detail = _OVERLAY.start(show_pet=False)
        except Exception as exc:  # noqa: BLE001 - transport must not raise
            return {"error": "companion unavailable: %s" % exc, "app": name}
        if not ok:
            return {"error": "companion unavailable: %s" % detail, "app": name}
        token = uuid.uuid4().hex[:12]
        op = {"op": "watch_start", "app": name, "token": token}
        if needle:
            op["until"] = needle
        if scan_spec is not None:
            op["scan"] = scan_spec
        # Install the provisional row BEFORE the round trip: events can start
        # flowing the instant the companion attaches its observers. A rejected
        # start RESTORES the row that was running before it — the companion
        # validates the new app before it tears the old observers down.
        with _watch_lock:
            previous_row = _watch
            previous_ring = list(_watch_ring)
            _watch_ring.clear()
            provisional = {
                "app": name,
                "until": needle,
                "scan": scan_spec,
                "once": once,
                "token": token,
                "owner_pid": os.getpid(),
                "started_at": time.time(),
                "events": 0,
                "hit": None,
                "stopped": None,
                "recent": [],
            }
            if scan_spec is not None:
                # The companion answers with the arm-time baseline size; until
                # that ack, the honest count is "unknown" (0 found so far).
                provisional["scan_baseline"] = 0
                provisional["scan_baseline_at"] = None
                provisional["scan_ticks"] = 0
            _watch = provisional

        def restore_previous():
            global _watch
            with _watch_lock:
                if _watch is not provisional:
                    # A stop (or another start) took over while we waited:
                    # never resurrect a row the user already ended.
                    return
                _watch = previous_row
                _watch_ring[:] = previous_ring
                _write_state()

        try:
            # A scan start walks the tree once to record its baseline before it
            # acks; give that round trip the longer budget it actually needs.
            timeout = (WATCH_SCAN_REQUEST_TIMEOUT if scan_spec is not None
                       else WATCH_REQUEST_TIMEOUT)
            reply = _OVERLAY.request(op, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - transport must not raise
            restore_previous()
            return {"error": str(exc), "app": name}
        if not isinstance(reply, dict):
            restore_previous()
            return {"error": "companion did not answer watch_start", "app": name}
        reply_token = reply.get("token")
        if reply_token is not None and reply_token != token:
            # A late reply to an EARLIER start (already timed out): it must not
            # answer this one, in either direction.
            restore_previous()
            return {"error": "stale watch reply", "app": name}
        failure = reply.get("watch_error")
        if failure:
            # The companion resolves and installs the NEW app before it drops
            # the running watch, so a rejected start leaves the previous watch
            # exactly as it was: put its row back (or publish no row at all).
            restore_previous()
            return _watch_start_failure(name, str(failure))
        with _watch_lock:
            if _watch is not provisional:
                # A concurrent stop_watch landed while we waited for the ack.
                return {"error": "watch start was cancelled", "app": name}
            if scan_spec is not None:
                # The companion recorded the baseline BEFORE it acked, so its
                # reply is the first honest count of matches at arm (0 means
                # the app exposes no author-role markup at all).
                baseline = reply.get("baseline")
                if isinstance(baseline, (int, float)) \
                        and not isinstance(baseline, bool):
                    _watch["scan_baseline"] = max(0, int(baseline))
                _watch["scan_baseline_at"] = time.time()
            # Arm-time surface baseline (until watches): the surfaces that
            # already contained the needle. A surface in this set waits for a
            # transition, so the status line can say why it is still "no hit"
            # instead of implying nothing is being watched.
            surfaces = reply.get("baseline_had")
            if isinstance(surfaces, list):
                _watch["baseline_had"] = [
                    surface for surface in surfaces
                    if isinstance(surface, str) and surface in WATCH_SURFACES]
            if reply.get("window_title_disabled") is True:
                _watch["window_title_disabled"] = True
            stopped = _watch.get("stopped")
            if stopped is not None:
                if stopped.get("token") == token:
                    # This watch really did start and then stopped (the app quit
                    # instantly). Keep the row: status reports the clean stop.
                    started_at = _watch["started_at"]
                    _write_state()
                    result = {"ok": True, "app": name, "until": needle,
                              "once": once, "started_at": started_at,
                              "status": watch_status()}
                    if scan_spec is not None:
                        result["scan"] = scan_spec
                    _add_baseline_result(result, provisional)
                    return result
                # A stop line written BEFORE this watch was installed (an old
                # watch quitting, a concurrent stop) must not mark the new row
                # dead; the start did not take.
                restore_previous()
                return {"error": "watch start was superseded by a stop", "app": name}
            started_at = _watch["started_at"]
            _write_state()
        result = {"ok": True, "app": name, "until": needle, "once": once,
                  "started_at": started_at, "status": watch_status()}
        if scan_spec is not None:
            result["scan"] = scan_spec
        _add_baseline_result(result, provisional)
        return result
    except Exception as exc:  # noqa: BLE001 - a handler must always answer
        return {"error": str(exc)}


def _add_baseline_result(result, row):
    """Echo the arm-time surface facts on a successful watch_start result."""
    surfaces = row.get("baseline_had") if isinstance(row, dict) else None
    if isinstance(surfaces, list):
        result["baseline_had"] = list(surfaces)
    if isinstance(row, dict) and row.get("window_title_disabled"):
        result["window_title_disabled"] = True
    return result


def _watch_baseline_lines(row, until):
    """Status lines for the at-arm surfaces that already contained the needle.

    A pre-existing surface is not a hit and is not news: the watch waits for it
    to change into something new. Saying that plainly is the whole point — the
    old status ("no hit") left the agent to infer why nothing fired while its
    terminal title literally read the command it had just typed.
    """
    if not isinstance(until, str) or not until:
        return []
    surfaces = row.get("baseline_had")
    if not isinstance(surfaces, list) or not surfaces:
        return []
    lines = []
    for surface in surfaces:
        if surface not in WATCH_SURFACES:
            continue
        reason = ""
        if surface == "window" and row.get("window_title_disabled"):
            # Terminal titles are the command line: name the trap explicitly.
            reason = " (command echo)"
        # A label waits on the label, not on a value that will never carry the
        # match. The window case keeps the brief's sentence verbatim.
        waits_on = "label" if surface == "label" else "value"
        lines.append(
            'at arm: %s already contained "%s"%s \u2014 waiting for the '
            '%s to change' % (WATCH_SURFACE_ARM[surface], until, reason,
                              waits_on))
    return lines


def watch_status():
    """Compact text block for the agent: header plus the last 10 events.

    Falls back to the row in ``state.json`` when this process does not own a
    live watch, so a gateway restart cannot turn an observable watch into a lie
    ("no watch active" while the pet still carries the line).
    """
    external = False
    with _watch_lock:
        row = _watch_snapshot()
        ring = [dict(event) for event in _watch_ring]
        if row is None:
            row = _read_state_watch_row()
            external = row is not None
    if row is None:
        return "no watch active"
    until = row.get("until")
    scan = row.get("scan") if isinstance(row.get("scan"), dict) else None
    hit = row.get("hit")
    stopped = row.get("stopped")
    subject = until or ("content scan" if scan else None) or "any change"
    lines = ["watching %s for %s · %d events · %s" % (
        row.get("app") or "?", subject,
        int(row.get("events") or 0), "hit" if hit else "no hit")]
    if scan is not None:
        lines.append(_watch_scan_line(scan, row, bool(hit)))
    if hit:
        matched = hit.get("matched")
        if matched in WATCH_SURFACES:
            # Name the surface: a terminal hit came from the pane's value (the
            # output), not from its window title (the command line).
            lines.append('hit: %s \u2014 "%s" (%s) at %s' % (
                matched, hit.get("label") or "", WATCH_SURFACE_HIT[matched],
                _watch_clock(hit.get("at"))))
        else:
            suffix = (" \u2014 %s" % hit["window"]) if hit.get("window") else ""
            lines.append("hit: %s%s at %s" % (hit.get("label") or "", suffix,
                                              _watch_clock(hit.get("at"))))
    else:
        lines.extend(_watch_baseline_lines(row, until))
    if row.get("window_title_disabled"):
        lines.append("title matching disabled for terminal app")
    source = ring if not external else (row.get("recent") or [])
    for event in source[-WATCH_STATUS_EVENTS:]:
        lines.append(_watch_event_line(event))
    if stopped:
        lines.append("stopped: %s" % (stopped.get("reason") or "stopped"))
    if external:
        owner = row.get("owner_pid")
        if _foreign_owner_alive(owner):
            lines.append("note: another butler process owns this watch (pid %s)"
                         % owner)
        else:
            lines.append("note: stale row from a process that exited; "
                         "butler_watch_stop clears it, watch.jsonl keeps the log")
    return "\n".join(lines)


def _send_watch_stop():
    """Ask the live companion to detach its observers, fire-and-forget.

    The companion answers a stop with an unsolicited ``watch-stopped`` line
    (routed to the event callback), so ``request()`` here would always burn its
    whole timeout. Only ask a live companion: request() would otherwise spawn one
    just to stop a watch nobody is running. Never raises.
    """
    try:
        if _OVERLAY.is_running():
            send = getattr(_OVERLAY, "send", None)
            if callable(send):
                send({"op": "watch_stop"})
            else:  # test doubles that only speak the request dialect
                _OVERLAY.request({"op": "watch_stop"}, timeout=WATCH_REQUEST_TIMEOUT)
    except Exception:  # noqa: BLE001 - a dead pipe must not keep a stale row
        pass


def acknowledge_watch_hit():
    """Acknowledge the hit announcement that outlives the watch (never raises).

    A hit writes a top-level ``watch_hit`` row, so the announcement survives the
    watch that produced it — in particular a one-shot watch has already stopped
    when the user sees it. This is the user's acknowledgement: mark the block
    ``acknowledged: true`` and then remove it. In sticky mode it also drops the
    live row's ``hit`` so the row returns to the ordinary change count. Works
    with no watch running (the normal case after a one-shot hit); a second ack
    is an honest false, never an error. Only an ack removes the announcement:
    stopping the watch (``stop_watch``/the pane's Stop) leaves it in place.
    """
    with _watch_lock:
        global _watch_hit_id
        row = _watch
        external = False
        if row is None:
            row = _read_state_watch_row()
            external = row is not None
        top_hit = _read_state_watch_hit()
        row_hit = row.get("hit") if isinstance(row, dict) else None
        row_hit = row_hit if isinstance(row_hit, dict) else None
        stopped = bool(row and row.get("stopped"))
        sticky = bool(row and row.get("once") is False and not stopped)
        app = (top_hit or {}).get("app") or (row or {}).get("app")
        try:
            events = int((row or {}).get("events") or 0)
        except (TypeError, ValueError, AttributeError):
            events = 0

        if top_hit is None and sticky and row_hit is not None and external \
                and _foreign_owner_alive(row.get("owner_pid")):
            # Legacy sticky hit with no top-level block, owned by another LIVE
            # process: this process cannot clear it. Be honest (and a second
            # ack stays a no-op) instead of reporting an acknowledgement that
            # did not happen.
            return {"status": "elsewhere", "acknowledged": False,
                    "app": app, "owner_pid": row.get("owner_pid"),
                    "note": "another butler process owns this watch; "
                            "acknowledge it from that process"}

        if top_hit is None and not (sticky and row_hit is not None):
            if row is None:
                return {"status": "idle", "acknowledged": False}
            return {"status": "stopped" if stopped else "no-hit",
                    "acknowledged": False, "app": app, "events": events}

        # 1. Say it was seen BEFORE removing it: a poll between the two writes
        #    can only ever see an acknowledged hit, never look unacked.
        if top_hit is not None:
            acked = dict(top_hit)
            acked["acknowledged"] = True
            _write_state(watch_hit=acked)

        # 2. Sticky only: the live row's own hit goes back to the change count.
        #    A row owned by another LIVE process is left to that process, whose
        #    next write reconciles it against the now-gone top-level block; a
        #    dead owner's row is cleared here.
        clear_foreign_hit = False
        if sticky and row_hit is not None:
            if external:
                clear_foreign_hit = not _foreign_owner_alive(
                    row.get("owner_pid"))
            else:
                row["hit"] = None
        _watch_hit_id = None

        # 3. Remove the announcement block (never clobbering the watch row).
        _write_state(clear_watch_hit=True,
                     clear_sticky_hit=clear_foreign_hit)
        return {"status": "acknowledged", "acknowledged": True,
                "app": app, "events": events}


def ack_watch():
    """The hat menu's acknowledgement door: same work as ``butler_watch_ack``."""
    return acknowledge_watch_hit()


def stop_watch():
    """Stop the companion's watch and clear its state row.

    When the row was published by another process (a gateway restart, a second
    plugin host), this process cannot reach that process's companion: a live
    owner is reported as "elsewhere", a dead one leaves a stale row this call
    clears from the shared snapshot.
    """
    external = False
    with _watch_lock:
        row = _watch
        if row is None:
            row = _read_state_watch_row()
            if row is None:
                return {"status": "idle"}
            external = True
        try:
            events = int(row.get("events") or 0)
        except (TypeError, ValueError):
            events = 0
        app = row.get("app")
        owner = row.get("owner_pid")
    if external:
        if _foreign_owner_alive(owner):
            return {"status": "elsewhere", "app": app, "owner_pid": owner,
                    "note": "another butler process owns this watch; "
                            "stop it from that process"}
        with _watch_lock:
            _clear_watch_state()
        return {"status": "cleared", "app": app, "events": events}
    # Fire-and-forget: the companion answers a stop with an unsolicited
    # `watch-stopped` line (routed to the event callback), so request() here
    # would always burn its whole timeout. Only ask a live companion: request()
    # would otherwise spawn one just to stop a watch nobody is running.
    _send_watch_stop()
    with _watch_lock:
        _clear_watch_state()
    return {"status": "stopped", "app": app, "events": events}


def set_claim_chip(enabled):
    """Arm/disarm the companion's pop-out pet click chip.

    Starts the shared companion (without a pet double) when the claim is armed
    and no guide/summon owns it yet. A failed build/degrade is reported, never
    raised: the in-app listener and the shortcut remain usable.
    """
    global _claim_chip_enabled
    _claim_chip_enabled = bool(enabled)
    if not _claim_chip_enabled:
        _OVERLAY.claim_chip(False)
        return {"enabled": False, "running": _OVERLAY.is_running()}
    ok, detail = _OVERLAY.start(show_pet=False)
    if not ok:
        return {"enabled": True, "running": False, "detail": str(detail)}
    # Tell the petless companion the live sprite size; without it the chip can
    # only anchor to the (mostly empty) pop-out window frame.
    _OVERLAY.claim_chip(True, sprite=bp_overlay.resolve_pet_sprite())
    return {"enabled": True, "running": True, "detail": str(detail)}


def _save_session(session=None):
    target = session if session is not None else _session
    if not target:
        return
    path = os.path.join(_guides_dir(), target["id"] + ".json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(target, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _loop():
    poll = poll_seconds()
    while not _stop_flag:
        time.sleep(poll)
        with _lock:
            s = _session
            if not s or s.get("state", {}).get("status") not in ("running", "stalled"):
                continue
            try:
                _poll_once(s)
            except Exception as exc:
                # Never swallow silently: surface repeated failures in the
                # session note so agents can see the loop is impaired.
                streak = s.get("poll_errors", 0) + 1
                s["poll_errors"] = streak
                if streak == ERROR_STREAK_NOTE:
                    s["state"] = dict(
                        s["state"],
                        note="butler poll failing: %r" % exc)
                _save_session()
                _write_state()
                continue
            s["poll_errors"] = 0
            _save_session()
            _write_state()


def _set_guard_note(s, note, reset_clock=True):
    """Publish a guard note and keep the step's timeout from firing under it.

    The note is stable (never carries a changing counter), so the pet's speech
    dedupe can suppress repeats. ``guard_note`` records that the note belongs
    to a guard (Hermes capture / off-screen control) so a normal poll can clear
    it without ever clobbering a real stall note. The clock is reset only when
    the note FIRST appears, so a user who is plausibly scrolling is not
    abandoned at 120 s while they read it.
    """
    state = s.setdefault("state", {})
    first = state.get("guard_note") != note
    if first and reset_clock:
        state["step_opened_at"] = time.time()
    # Re-assert on EVERY poll while the guard holds, not only on first
    # appearance: if the stall block ran between two guarded polls, the
    # actionable note must replace the generic stall note and the guide must
    # leave `stalled` instead of escalating to the 180 s abandonment.
    state.pop("stalled_at", None)
    if state.get("status") == "stalled":
        state["status"] = "running"
    state["guard_note"] = note
    state["note"] = note


def _clear_guard_note(s):
    """Drop a guard note once its condition no longer holds."""
    state = s.get("state") if isinstance(s, dict) else None
    if not isinstance(state, dict):
        return
    guard = state.pop("guard_note", None)
    if guard and state.get("note") == guard:
        state["note"] = None


def _journal_token(value, limit=40):
    """A single space-free journal token (same rule as the companion).

    The journal is one event per line; a multi-word reason would otherwise
    break a consumer that tokenizes it. Reads the same as ``guide-end:<token>``
    on both sides of the pipe.
    """
    text = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in "_-")
    return text[:limit] or "stopped"


def _end_guide_overlay(s, reason):
    """Tell the companion this guide is over, at most once per guide.

    Every terminal path (done / stopped / abandoned / superseded / a
    capture-failed give-up) funnels through here. The ``guide_end`` op fades
    the walking double so abandonment cannot leave the "ghost pet" that the
    measured session did. The journal line traces a ghost back to a missed
    end. Returns True only for the first end of this guide.
    """
    if not isinstance(s, dict) or s.get("guide_end"):
        return False
    # One safe token: the journal is newline-delimited and the companion
    # sanitizes the same way, so a reason can never inject a log line.
    reason = " ".join(str(reason or "stopped").split())[:40] or "stopped"
    s["guide_end"] = reason
    try:
        bp_overlay.journal_click("engine-guide-end:%s" % _journal_token(reason))
    except Exception:  # noqa: BLE001 - tracing must not fail a terminal path
        pass
    end = getattr(_OVERLAY, "guide_end", None)
    if callable(end):
        try:
            end(reason)
        except TypeError:
            # Older/simple test doubles may only accept no argument.
            try:
                end()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - overlay cleanup is best-effort
            pass
    return True


def _abandon_stalled(s, idx):
    """Stop a guide that stayed stalled past the auto-stop deadline.

    Mirrors ``guide_stop``: finalise the session, persist it to history, idle
    the overlay, and leave exactly one pet line. The session row stays in
    ``_session`` so ``butler_guide_status`` can report the terminal
    ``abandoned`` status; the poll loop exits via ``_stop_flag``.
    """
    global _stop_flag
    reason = "step %d was never verified" % (idx + 1)
    s["state"]["status"] = "abandoned"
    s["state"]["note"] = reason
    s["state"].pop("stalled_at", None)
    s["reason"] = reason
    s["current_box"] = None
    s["current_label"] = None
    s["overlay"] = "idle"
    # This is the measured ghost: the walker double stayed on screen because
    # abandonment only idled the ring. End it (once) before the state write.
    _end_guide_overlay(s, "abandoned")
    # Exactly one pet line per abandonment: the say row is spoken once by id,
    # and the terminal block's precedence no longer repeats it afterwards.
    # Gate on its OWN flag, not the guide_end latch, so a guide that was
    # already ended under another reason still speaks the promised line once;
    # set it BEFORE the save so a reloaded session cannot speak it again.
    if not s.get("abandon_said"):
        s["abandon_said"] = True
        say("Guide stopped — step %d never verified" % (idx + 1))
    _save_session(s)
    _OVERLAY.idle()
    _write_state()
    _stop_flag = True


def _supersede_active(reason="replaced by a new guide"):
    """Archive the running guide as ``superseded`` and reset its overlay.

    Returns the archived session row, or None when no guide was active. The
    caller is responsible for installing the replacement session.
    """
    active = _session
    if not active:
        return None
    active["state"]["status"] = "superseded"
    active["state"]["note"] = reason
    active["state"].pop("stalled_at", None)
    active["reason"] = reason
    active["current_box"] = None
    active["current_label"] = None
    active["overlay"] = "idle"
    _end_guide_overlay(active, "superseded")
    _save_session(active)
    _OVERLAY.idle()
    say("Replaced the previous guide")
    return active


def _note_scope_blocked(s, snap):
    """Explain that this step happens in another app than the capture.

    The scope note replaces the Hermes-specific wording whenever the step
    resolves a scope app: "Switch to System Settings" is the actionable line,
    and the round-24 Hermes guard is the special case of this general rule, not
    a separate one. ``_set_guard_note`` resets the step clock the first time the
    note appears, so the 120 s stall deadline does not run while the user is
    switching apps.
    """
    scope = _clean_app_name(s.get("scope_app")) or "the app the step is about"
    note = "Switch to %s — this step happens in %s" % (scope, scope)
    _set_guard_note(s, note)
    s["current_box"] = None
    s["current_label"] = None
    s["overlay"] = "waiting"
    s["last_capture_app"] = snap.get("app")
    s["last_captured_at"] = time.time()
    _OVERLAY.idle()


def _note_hermes_blocked(s, step, snap):
    """Explain that this step cannot be read inside the Hermes window."""
    target = _guide_target_app(s, step) or "the app the step is about"
    note = "Butler can't read the Hermes window for this step — switch to %s" % target
    _set_guard_note(s, note)
    s["current_box"] = None
    s["current_label"] = None
    s["overlay"] = "waiting"
    s["last_capture_app"] = snap.get("app")
    s["last_captured_at"] = time.time()
    _OVERLAY.idle()


def _poll_once(s):
    idx = s["state"]["idx"]
    step = s["steps"][idx]
    snap = bp_capture.capture(max_elements=SHALLOW_MAX_ELEMENTS,
                              depth=SHALLOW_DEPTH)
    if snap.get("error") or (snap.get("trusted") is False):
        s["state"] = dict(s["state"], status="capture_failed")
        s["capture_note"] = snap.get("note") or snap.get("error")
        s["fix"] = snap.get("fix")
        # A guide that cannot read the screen is a give-up: the poll loop will
        # not retry a capture_failed row, so end the walk instead of leaving
        # the pet double parked forever.
        _OVERLAY.idle()
        _end_guide_overlay(s, "capture_failed")
        return
    # Fix 1: the frontmost app may be Hermes while this step is about another
    # app. Its chat transcript is AXStaticText and must never be a target.
    # Scope first: a step that names its app may only match a capture of THAT
    # app, so a Hermes capture is just one instance of "the wrong app".
    scoped = _scope_capture(s, idx, snap)
    if scoped is None:
        _note_scope_blocked(s, snap)
        return
    snap = scoped
    guarded = _guard_capture(s, step, snap)
    if guarded is None:
        _note_hermes_blocked(s, step, snap)
        return
    snap = guarded
    label_needle, best, verdict = _evaluate_step(step, snap)
    # Deep pass: Electron overlays/pages nest their real controls below the
    # shallow walk's depth. Re-walk once, and only when the shallow snapshot
    # could not satisfy this step; both find and expect then reuse that deep
    # snapshot (never walk twice for the two halves of one step).
    if _step_needs_deep(step, best, verdict) and _deep_capture_allowed():
        deep = _deep_capture()
        if not deep.get("error") and deep.get("trusted") is not False:
            scoped = _scope_capture(s, idx, deep)
            if scoped is None:
                _note_scope_blocked(s, deep)
                return
            guarded = _guard_capture(s, step, scoped)
            if guarded is None:
                _note_hermes_blocked(s, step, scoped)
                return
            snap = guarded
            label_needle, best, verdict = _evaluate_step(step, snap)
    s["last_capture_app"] = snap.get("app")
    s["last_captured_at"] = time.time()
    if not verdict["verified"]:
        # "Open the app" counts when the app is OPEN, not only frontmost:
        # users follow the pet side-by-side without chasing focus. The
        # targeted fallback applies ONLY to the `app` key — `app_frontmost`
        # steps deliberately wait for real focus ("Focus System Settings").
        want_app = (step.get("expect") or {}).get("app")
        if want_app and not (step.get("expect") or {}).get("app_frontmost"):
            app_snap = bp_capture.capture(target=want_app, max_elements=40, depth=8)
            if app_snap.get("app") and (app_snap.get("window")
                                        or int(app_snap.get("count") or 0) > 0):
                verdict = {"verified": True, "reason": "app_open_targeted"}
    # Fix 2: a control scrolled below the fold reports a degenerate box (often
    # at the origin), or its origin is outside every visible screen. Ringing it
    # would draw a highlight over nothing, so explain the scroll instead and
    # leave the step unresolved without starting the stall clock again.
    if best is not None and _box_is_offscreen(best.get("box"), snap):
        label = best.get("label") or label_needle or "the control"
        _set_guard_note(s, "Scroll to bring %s into view" % label)
        s["current_box"] = None
        s["current_label"] = None
        s["overlay"] = "waiting"
        _OVERLAY.idle()
        return
    _clear_guard_note(s)
    if best is not None and best.get("box"):
        s["current_box"] = best["box"]
        s["current_label"] = best.get("label")
        s["overlay"] = "ringing"
        _OVERLAY.publish(s)
    elif not label_needle:
        # hands-off step (e.g. open an app): nothing to ring, just verify
        s["current_box"] = None
        s["current_label"] = None
        s["overlay"] = "waiting"
    else:
        # target not visible right now: un-park any ring so a highlight never
        # stays welded over a window the user left. It re-rings when the
        # target is in view again.
        if s.get("current_box") is not None or s.get("current_label") is not None:
            s["current_box"] = None
            s["current_label"] = None
            s["overlay"] = "waiting"
            _OVERLAY.idle()
    if verdict["verified"]:
        # A stalled guide that verifies before the deadline clears the stall.
        s["state"]["status"] = "running"
        s["state"]["note"] = None
        s["state"].pop("stalled_at", None)
        # Remember the app this guide last proved: a later Hermes capture names
        # it instead of guessing "the app the step is about".
        if isinstance(snap.get("app"), str) and snap["app"].strip():
            s["last_verified_app"] = snap["app"].strip()
        s["verifications"].append({"step": idx, "reason": verdict["reason"],
                                   "verified_at": time.time()})
        idx += 1
        s["state"]["idx"] = idx
        s["state"]["note"] = None
        s["state"]["step_opened_at"] = time.time()
        if idx >= len(s["steps"]):
            s["state"]["status"] = "done"
            s["state"]["say"] = "Guide complete"
            s["current_box"] = None
            s["current_label"] = None
            _OVERLAY.idle()
            _OVERLAY.done()
            _end_guide_overlay(s, "done")
        else:
            nxt = s["steps"][idx]
            s["state"]["say"] = nxt["say"]
            s["current_box"] = None
            s["current_label"] = None
            # The next step may name another app (or inherit this one); publish
            # its scope now so status and the next poll agree.
            _record_scope_app(s, idx)
            # clear any stale ring now; the next step re-rings when found.
            _OVERLAY.idle()
        return
    opened_at = s["state"].get("step_opened_at") or s.get("started_at") or time.time()
    held = time.time() - opened_at
    if held > step_timeout():
        now = time.time()
        # Stable note: never carry the elapsed number, or the pet's status line
        # changes every tick and the speech dedupe can never suppress it.
        note = "step %d unverified — expect=%s" % (idx + 1, verdict["reason"])
        stalled_at = s["state"].get("stalled_at")
        if stalled_at is None:
            # The stall began when the step passed its timeout; deriving it
            # from the step timer (not from this poll tick) means a guide that
            # was stalled while nobody polled still stops on the next tick.
            stalled_at = opened_at + step_timeout()
            s["state"]["stalled_at"] = stalled_at
        s["state"]["status"] = "stalled"
        if now - stalled_at >= stall_auto_stop_seconds():
            _abandon_stalled(s, idx)
            return
        s["state"]["note"] = note


def guide_start(args, unattended=False):
    if unattended:
        return {"error": "butler guides require an interactive session; refusing under unattended runs",
                "refusal": "unattended_run"}
    global _session, _thread, _stop_flag
    with _lock:
        active = (_session
                  if _session and _session.get("state", {}).get("status")
                  in ("running", "stalled") else None)
        replace = _as_bool(args.get("replace"))
        if active and not replace:
            return {"error": "a guide is already active",
                    "active": _session["id"], "step": _session["state"]["idx"] + 1,
                    "hint": "a guide is already active — pass replace=true to "
                            "take over, or butler_stop first"}
        steps = _normalize_steps(args.get("steps") or [])
        if not steps:
            return {"error": "guide start rejected: no verifiable steps "
                             "(each step needs say + find.label or an expect key)"}
        replaced_id = None
        if active and replace:
            old = _supersede_active()
            replaced_id = old["id"] if old else None
        guide = {
            "id": uuid.uuid4().hex[:10],
            "title": args.get("title") or "Guide",
            "steps": steps,
            "state": {"idx": 0, "status": "running",
                      "say": steps[0]["say"],
                      "step_opened_at": time.time()},
            "verifications": [],
            "overlay": "starting",
            "started_at": time.time(),
        }
        _session = guide
        _stop_flag = False
        # Publish step 1's scope before the startup capture: an unresolved plan
        # has to journal its one-time line even if the first capture is another
        # app, and status must carry the scope from the very first response.
        _record_scope_app(guide, 0)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="butler-poll", daemon=True)
            _thread.start()
        ok, detail = _OVERLAY.start()
        guide["overlay"] = ("unavailable:%s" % detail) if not ok else "starting"
        try:
            first = bp_capture.capture(max_elements=SHALLOW_MAX_ELEMENTS,
                                       depth=SHALLOW_DEPTH)
        except Exception as exc:  # capture must never orphan a live guide
            guide["state"] = dict(guide["state"], status="capture_failed")
            guide["capture_note"] = "startup capture raised: %r" % exc
            _OVERLAY.idle()
            _end_guide_overlay(guide, "capture_failed")
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "state": guide["state"], "capture_note": guide["capture_note"],
                    "total_steps": len(steps)}
        if first.get("error") or (first.get("trusted") is False):
            guide["state"] = dict(guide["state"], status="capture_failed")
            guide["capture_note"] = first.get("note") or first.get("error")
            guide["fix"] = first.get("fix")
            _OVERLAY.idle()
            _end_guide_overlay(guide, "capture_failed")
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "state": guide["state"], "capture_note": guide["capture_note"],
                    "fix": guide["fix"], "total_steps": len(steps)}
        scoped = _scope_capture(guide, 0, first)
        if scoped is None:
            # The first capture is another app than the step's scope: say which
            # app to switch to instead of ringing something from the wrong one.
            _note_scope_blocked(guide, first)
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "step": {"idx": 1, "say": guide["state"]["say"]},
                    "overlay": guide["overlay"],
                    "note": guide["state"].get("note"),
                    "replaced": replaced_id,
                    "total_steps": len(steps)}
        first = scoped
        guarded = _guard_capture(guide, steps[0], first)
        if guarded is None:
            # Same Hermes protection as the poll: never ring the chat
            # transcript just because the user is reading the guide there.
            _note_hermes_blocked(guide, steps[0], first)
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "step": {"idx": 1, "say": guide["state"]["say"]},
                    "overlay": guide["overlay"],
                    "note": guide["state"].get("note"),
                    "replaced": replaced_id,
                    "total_steps": len(steps)}
        first = guarded
        find0 = steps[0].get("find") or {}
        best = bp_capture.best_match(
            _prefer_window(first.get("elements", []),
                           _step_window_hint(steps[0]), first.get("window")),
            find0.get("label"), find0.get("role"))
        # Same two-pass treatment as the poll: a first-step target inside an
        # Electron overlay lives below the shallow walk. Try one deep walk
        # before telling the agent the plan is wrong with overlay "no_match".
        if find0.get("label") and best is None and _deep_capture_allowed():
            deep = _deep_capture()
            if not deep.get("error") and deep.get("trusted") is not False:
                scoped = _scope_capture(guide, 0, deep)
                if scoped is None:
                    _note_scope_blocked(guide, deep)
                    _save_session()
                    _write_state()
                    return {"guide_id": guide["id"], "title": guide["title"],
                            "step": {"idx": 1, "say": guide["state"]["say"]},
                            "overlay": guide["overlay"],
                            "note": guide["state"].get("note"),
                            "replaced": replaced_id,
                            "total_steps": len(steps)}
                guarded = _guard_capture(guide, steps[0], scoped)
                if guarded is None:
                    _note_hermes_blocked(guide, steps[0], scoped)
                    _save_session()
                    _write_state()
                    return {"guide_id": guide["id"], "title": guide["title"],
                            "step": {"idx": 1, "say": guide["state"]["say"]},
                            "overlay": guide["overlay"],
                            "note": guide["state"].get("note"),
                            "replaced": replaced_id,
                            "total_steps": len(steps)}
                first = guarded
                best = bp_capture.best_match(
                    _prefer_window(first.get("elements", []),
                                   _step_window_hint(steps[0]),
                                   first.get("window")),
                    find0.get("label"), find0.get("role"))
        guide["last_capture_app"] = first.get("app")
        guide["last_captured_at"] = time.time()
        if best is not None and _box_is_offscreen(best.get("box"), first):
            # A first-step control below the fold: explain the scroll rather
            # than parking a ring at the origin.
            label = best.get("label") or find0.get("label") or "the control"
            _set_guard_note(guide, "Scroll to bring %s into view" % label)
            guide["current_box"] = None
            guide["current_label"] = None
            guide["overlay"] = "waiting"
            _OVERLAY.idle()
        elif best is not None and best.get("box"):
            guide["current_box"] = best["box"]
            guide["current_label"] = best.get("label")
            guide["overlay"] = "ringing"
            _OVERLAY.publish(guide)
        elif find0.get("label"):
            guide["overlay"] = "no_match"
            guide["state"]["note"] = "target not found in the first capture; the agent should re-check the plan"
        else:
            # hands-off step: the butler is waiting on the user, not missing a target
            guide["overlay"] = "waiting"
        _save_session()
        _write_state()
        return {"guide_id": guide["id"], "title": guide["title"],
                "step": {"idx": 1, "say": guide["state"]["say"]},
                "overlay": guide["overlay"],
                "note": guide["state"].get("note"),
                "replaced": replaced_id,
                "total_steps": len(steps)}


def guide_status():
    with _lock:
        if not _session:
            return {"status": "idle"}
        s = dict(_session)
        s.pop("steps", None)
        return s


def guide_stop(reason="stopped"):
    global _session
    with _lock:
        if not _session:
            return {"status": "idle"}
        stopped_at = time.time()
        _session["state"]["status"] = "stopped"
        # A terminal block must not carry the live stall note or the current
        # instruction: the pet's dead-guide precedence and the /state sweep
        # both key off a terminal row being quiet.
        _session["state"]["note"] = None
        _session["state"]["say"] = None
        _session["state"]["stopped_at"] = stopped_at
        _session["reason"] = reason
        _session["current_box"] = None
        # End the walk before the companion is quit: the journal line makes a
        # missing/present guide_end traceable even though quit tears the
        # process down. Idempotent, so a guide already ended calls nothing.
        _end_guide_overlay(_session, reason)
        stopped = dict(_session)
        _OVERLAY.quit()
        stopped["overlay"] = "stopped"
        _save_session()
        # Publish BEFORE dropping the in-memory session. ``_write_state`` only
        # carries the guide block forward from disk when this process owns no
        # session, so releasing first resurrected the stale ``stalled`` row
        # (round 19: butler_stop said stopped while state.json kept the note).
        # The write still merges the watch / summon_pending blocks.
        _write_state()
        _session = None
        return {"status": "stopped", "reason": reason,
                "verified": stopped.get("verifications", [])}


#: Round 19 names the writer behind ``butler_stop`` ``stop_guide``; keep the
#: historical spelling as the implementation so existing call sites stay put.
stop_guide = guide_stop
