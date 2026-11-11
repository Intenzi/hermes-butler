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

# Ambient WATCH (passive Accessibility observation). The companion owns the AX
# observer; this process only keeps a bounded tail for the agent and the desktop.
WATCH_RING = 40          # in-memory ring of the newest events
WATCH_STATE_RECENT = 12  # what state.json carries (newest last) for the desktop
WATCH_JSONL_MAX = 500    # <plugin data dir>/watch.jsonl tail cap
WATCH_STATUS_EVENTS = 10 # event lines watch_status() prints
WATCH_REQUEST_TIMEOUT = 5.0

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
_watch_ring = []          # newest WATCH_RING events, oldest first
# Re-entrant: the stdout reader mutates the row, a status poll snapshots it,
# and _write_state takes it again from inside those paths.
_watch_lock = threading.RLock()
# One start at a time: two overlapping starts could otherwise nest provisional
# rows and resurrect a start that already failed.
_watch_start_lock = threading.Lock()


def init(data_dir):
    global DATA_DIR
    if not data_dir:
        raise ValueError("butler engine: init() requires a data_dir")
    DATA_DIR = data_dir
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
        best = bp_capture.best_match(snap.get("elements", []), label_needle,
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
    if isinstance(label, str) and label.strip():
        clean["find"] = {"label": label.strip()}
        if isinstance(find.get("role"), str) and find["role"].strip():
            clean["find"]["role"] = find["role"].strip()
    # No key whitelist here: expect keys evolve (app/visible/window/focus...)
    # and silently dropping an unknown but valid key starves a step of its
    # verification (expect=no_expect_given). Anything present passes through,
    # invalid keys simply never match in expect_pending.
    kept_expect = {k: str(v) for k, v in expect.items() if v}
    if kept_expect:
        clean["expect"] = kept_expect
    # A step the butler can neither locate nor verify is a dead step.
    if not (clean.get("find") or clean.get("expect")):
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


def _write_state(clear_summon=False, clear_watch=False):
    """Publish the shared snapshot. Never raises.

    Two processes host this engine: the agent runtime (guides) and the plugin
    API that owns the companion (the summon row from a chip click). Each owns
    part of ``state.json``, so a write merges the other owner's block instead of
    dropping it — otherwise a click silently erases a running guide, and a guide
    poll erases a click the desktop has not consumed yet. The ambient ``watch``
    block merges the same way (stop_watch clears it explicitly). A terminal
    guide block older than ``GUIDE_STALE_SECONDS`` is not carried forward: the
    guide stays in history, but the live-status row stops talking.
    """
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
                snap["watch"] = existing["watch"]
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
    if op in ("watch-event", "watch-hit", "watch-stopped"):
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


def _read_state_watch_row():
    """The watch row another process published in state.json, or None."""
    if not DATA_DIR:
        return None
    try:
        with open(os.path.join(str(DATA_DIR), "state.json"),
                  encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return None
    row = state.get("watch") if isinstance(state, dict) else None
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
            # Rows this engine starts always carry the flag. A hand-built or
            # legacy row without it takes the product default: one-shot.
            once = _watch.get("once") is not False
            hit = {
                "label": label,
                "window": event.get("window"),
                "at": event.get("at") or time.time(),
            }
            if not once:
                # Sticky mode keeps observing after the announcement; the hit
                # stays unacknowledged until the user (or the pane) says so.
                hit["acknowledged"] = False
            _watch["hit"] = hit
            # In place, with no model call and without waking the agent: the
            # pet says it now, the agent reads watch_status on its next turn.
            say("%s: %s appeared" % (_watch.get("app") or event.get("app") or "",
                                     label))
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
            _write_state()
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


def start_watch(app, until=None, once=True):
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
    """
    with _watch_start_lock:
        return _start_watch_locked(app, until, once)


def _start_watch_locked(app, until, once=True):
    global _watch
    try:
        name = str(app or "").strip()
        if not name:
            return {"error": "app is required"}
        needle = until.strip() if isinstance(until, str) else None
        if not needle:
            needle = None
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
                "once": once,
                "token": token,
                "owner_pid": os.getpid(),
                "started_at": time.time(),
                "events": 0,
                "hit": None,
                "stopped": None,
                "recent": [],
            }
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
            reply = _OVERLAY.request(op, timeout=WATCH_REQUEST_TIMEOUT)
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
            stopped = _watch.get("stopped")
            if stopped is not None:
                if stopped.get("token") == token:
                    # This watch really did start and then stopped (the app quit
                    # instantly). Keep the row: status reports the clean stop.
                    started_at = _watch["started_at"]
                    _write_state()
                    return {"ok": True, "app": name, "until": needle,
                            "once": once, "started_at": started_at,
                            "status": watch_status()}
                # A stop line written BEFORE this watch was installed (an old
                # watch quitting, a concurrent stop) must not mark the new row
                # dead; the start did not take.
                restore_previous()
                return {"error": "watch start was superseded by a stop", "app": name}
            started_at = _watch["started_at"]
            _write_state()
        return {"ok": True, "app": name, "until": needle, "once": once,
                "started_at": started_at, "status": watch_status()}
    except Exception as exc:  # noqa: BLE001 - a handler must always answer
        return {"error": str(exc)}


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
    hit = row.get("hit")
    stopped = row.get("stopped")
    lines = ["watching %s for %s · %d events · %s" % (
        row.get("app") or "?", until or "any change",
        int(row.get("events") or 0), "hit" if hit else "no hit")]
    if hit:
        suffix = (" — %s" % hit["window"]) if hit.get("window") else ""
        lines.append("hit: %s%s at %s" % (hit.get("label") or "", suffix,
                                          _watch_clock(hit.get("at"))))
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
    """Clear a sticky watch hit; the watch keeps observing (never raises).

    A sticky (``once: false``) watch records the hit and stays live so the user
    decides when it is done. This is that "done": drop the hit and the row goes
    back to the ordinary change count. A one-shot watch is already stopped, so
    there is nothing to acknowledge. ``acknowledged`` is False — never an error
    — when there is no hit, the watch already ended, or another live process
    owns the row.
    """
    with _watch_lock:
        row = _watch
        external = False
        if row is None:
            row = _read_state_watch_row()
            if row is None:
                return {"status": "idle", "acknowledged": False}
            external = True
        app = row.get("app")
        if row.get("stopped"):
            return {"status": "stopped", "acknowledged": False, "app": app}
        hit = row.get("hit")
        if not isinstance(hit, dict):
            return {"status": "no-hit", "acknowledged": False, "app": app}
        if external:
            owner = row.get("owner_pid")
            if _foreign_owner_alive(owner):
                return {"status": "elsewhere", "acknowledged": False,
                        "app": app, "owner_pid": owner,
                        "note": "another butler process owns this watch; "
                                "acknowledge it from that process"}
            return {"status": "stale", "acknowledged": False, "app": app,
                    "note": ("this process does not hold a live watch row; "
                             "butler_watch_stop clears it")
                    if owner == os.getpid() else
                    ("the process that owned this watch exited; "
                     "butler_watch_stop clears it")}
        row["hit"] = None
        try:
            events = int(row.get("events") or 0)
        except (TypeError, ValueError):
            events = 0
        _write_state()
        return {"status": "acknowledged", "acknowledged": True,
                "app": app, "events": events}


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
    _save_session(s)
    _OVERLAY.idle()
    _write_state()
    say("Guide stopped — step %d never verified" % (idx + 1))
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
    _save_session(active)
    _OVERLAY.idle()
    say("Replaced the previous guide")
    return active


def _poll_once(s):
    idx = s["state"]["idx"]
    step = s["steps"][idx]
    snap = bp_capture.capture(max_elements=SHALLOW_MAX_ELEMENTS,
                              depth=SHALLOW_DEPTH)
    if snap.get("error") or (snap.get("trusted") is False):
        s["state"] = dict(s["state"], status="capture_failed")
        s["capture_note"] = snap.get("note") or snap.get("error")
        s["fix"] = snap.get("fix")
        return
    label_needle, best, verdict = _evaluate_step(step, snap)
    # Deep pass: Electron overlays/pages nest their real controls below the
    # shallow walk's depth. Re-walk once, and only when the shallow snapshot
    # could not satisfy this step; both find and expect then reuse that deep
    # snapshot (never walk twice for the two halves of one step).
    if _step_needs_deep(step, best, verdict) and _deep_capture_allowed():
        deep = _deep_capture()
        if not deep.get("error") and deep.get("trusted") is not False:
            snap = deep
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
        else:
            nxt = s["steps"][idx]
            s["state"]["say"] = nxt["say"]
            s["current_box"] = None
            s["current_label"] = None
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
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "state": guide["state"], "capture_note": guide["capture_note"],
                    "total_steps": len(steps)}
        if first.get("error") or (first.get("trusted") is False):
            guide["state"] = dict(guide["state"], status="capture_failed")
            guide["capture_note"] = first.get("note") or first.get("error")
            guide["fix"] = first.get("fix")
            _save_session()
            _write_state()
            return {"guide_id": guide["id"], "title": guide["title"],
                    "state": guide["state"], "capture_note": guide["capture_note"],
                    "fix": guide["fix"], "total_steps": len(steps)}
        find0 = steps[0].get("find") or {}
        best = bp_capture.best_match(first.get("elements", []),
                                     find0.get("label"), find0.get("role"))
        # Same two-pass treatment as the poll: a first-step target inside an
        # Electron overlay lives below the shallow walk. Try one deep walk
        # before telling the agent the plan is wrong with overlay "no_match".
        if find0.get("label") and best is None and _deep_capture_allowed():
            deep = _deep_capture()
            if not deep.get("error") and deep.get("trusted") is not False:
                first = deep
                best = bp_capture.best_match(first.get("elements", []),
                                             find0.get("label"), find0.get("role"))
        guide["last_capture_app"] = first.get("app")
        guide["last_captured_at"] = time.time()
        if best is not None and best.get("box"):
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
