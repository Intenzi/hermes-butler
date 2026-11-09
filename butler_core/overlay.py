"""Companion overlay controller.

Owns the click-through companion window that rings controls over ANY app and
hosts the walking pet double. The renderer is a separate bundled Swift
process, compiled on first use (disclosed). If it is missing or refuses to run,
the guide falls back to in-app speech bubbles only - never hard-fails.

Protocol: one JSON line per op on stdin -
  {"op":"ring","box":[x,y,w,h],"label":"...","say":"...","step":3,"total":7}
  {"op":"chip","step":3}
  {"op":"pet","frames_dir":"...","scale":0.33,"pet_enabled":true}
  {"op":"claim_chip","enabled":true}
  {"op":"claim_chip","enabled":true,"sprite_w":63,"sprite_h":69,"pad_bottom":24}
  {"op":"launcher"}
  {"op":"done"}
  {"op":"idle"}
  {"op":"quit"}

The companion writes one event line on clickable mini-windows:
  {"op":"launcher_click"}
  {"op":"claim_click"}
"""
import json
import os
import queue
import select
import shutil
import subprocess
import threading
import time

DEFAULT_PET_SCALE = 0.33
MIN_PET_SCALE = 0.2
MAX_PET_SCALE = 1.0
# petdex frames are 192x208 (store/pet-overlay.ts and pet-sprite.tsx defaults);
# the app draws them at frame * display.pet.scale.
PET_FRAME_WIDTH = 192.0
PET_FRAME_HEIGHT = 208.0
# PET_PADDING_BOTTOM in app/pet-overlay/pet-overlay-app.tsx: the sprite is laid
# out bottom-centred this many points above the pop-out window's bottom edge.
CLAIM_CHIP_PAD_BOTTOM = 24


def binary_path():
    here = os.path.dirname(__file__)
    return os.path.join(here, "services", "ButlerOverlay")


def binary_source_path():
    return os.path.join(os.path.dirname(__file__), "overlay_swift", "ButlerOverlay.swift")


def build_if_needed():
    """Compile the bundled Swift overlay when it is missing or stale.

    Returns (ok, detail); detail stays "present" or "compiled" for the doctor.
    """
    exe = binary_path()
    src = binary_source_path()
    exe_ready = os.path.exists(exe) and os.access(exe, os.X_OK)
    src_exists = os.path.exists(src)

    if exe_ready:
        if not src_exists:
            return True, "present"
        try:
            if os.path.getmtime(exe) >= os.path.getmtime(src):
                return True, "present"
        except OSError:
            # Fall through to a rebuild; an unreadable timestamp is not proof
            # that the binary came from the current source.
            pass

    if not src_exists:
        return False, "source_missing"
    if shutil.which("swiftc") is None:
        return False, "swiftc_not_found"
    try:
        subprocess.run(["swiftc", "-O", "-o", exe, src], timeout=120,
                       check=True, capture_output=True)
        return True, "compiled"
    except Exception as exc:  # noqa: BLE001
        return False, "compile_failed: %s" % exc


def hermes_home():
    """Profile home, honoring HERMES_HOME so tests and profiles stay isolated."""
    override = os.environ.get("HERMES_HOME")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.hermes")


def pets_dir():
    return os.path.join(hermes_home(), "pets")


def _config_path():
    return os.path.join(hermes_home(), "config.yaml")


def _unquote(value):
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def read_display_pet(path=None):
    """Read display.pet.{enabled,slug,scale} without a YAML dependency.

    Missing files, malformed lines, and unsupported values all degrade to an
    empty/partial dict; callers apply the defaults.
    """
    values = {}
    target = path or _config_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return values

    in_display = False
    pet_indent = None
    for raw in lines:
        # The config only needs a plain-text scan; comments are always trailing.
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        if indent == 0:
            in_display = content == "display:"
            pet_indent = None
            continue
        if not in_display:
            continue

        if pet_indent is None:
            if content == "pet:":
                pet_indent = indent
            continue

        if indent <= pet_indent:
            # Left the display.pet block; do not pick up similarly named keys
            # from a sibling section.
            pet_indent = None
            continue

        if ":" not in content:
            continue
        key, _, value = content.partition(":")
        key = key.strip()
        if key in ("enabled", "slug", "scale"):
            values[key] = _unquote(value)

    return values


def _resolved_enabled(values):
    """display.pet.enabled, defaulting to enabled when the config is absent."""
    raw = values.get("enabled")
    if raw is None or raw == "" or raw.lower() in ("null", "~"):
        return True
    if raw.lower() in ("false", "no", "off", "0"):
        return False
    return True


def _resolved_scale(values):
    raw = values.get("scale")
    if raw is None or raw == "" or raw.lower() in ("null", "~"):
        return DEFAULT_PET_SCALE
    try:
        scale = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PET_SCALE
    if scale != scale:  # NaN
        return DEFAULT_PET_SCALE
    return max(MIN_PET_SCALE, min(MAX_PET_SCALE, scale))


def _has_spritesheet(path):
    return bool(path) and os.path.isfile(os.path.join(path, "spritesheet.webp"))


def resolve_frames_dir():
    """Resolve the active pet directory, or None when no pet can be shown."""
    values = read_display_pet()
    slug = values.get("slug")

    root = pets_dir()
    if slug:
        configured = os.path.join(root, slug)
        if _has_spritesheet(configured):
            return configured

    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        candidate = os.path.join(root, name)
        if os.path.isdir(candidate) and _has_spritesheet(candidate):
            return candidate
    return None


def resolve_pet_op():
    """Build the one-shot pet op for start(), or None when no pet is installed."""
    frames_dir = resolve_frames_dir()
    if not frames_dir:
        return None
    values = read_display_pet()
    return {
        "op": "pet",
        "frames_dir": frames_dir,
        "scale": _resolved_scale(values),
        "pet_enabled": _resolved_enabled(values),
    }


def journal_click(event):
    """Append one line to the shared chip/click journal (bounded, best-effort).

    The companion writes the same file, so a click can be traced hop by hop:
    ``click`` (view) → ``emit`` (stdout) → ``pipe-claim`` (reader thread) →
    ``engine-pending`` (engine callback). Never raises: diagnosis must not be
    able to break the pipe.
    """
    try:
        base = os.path.join(hermes_home(), "plugin-data", "butler")
        path = os.path.join(base, "chip-events.log")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [ln for ln in handle.read().splitlines() if ln.strip()]
        except OSError:
            lines = []
        lines.append("%.3f %s" % (time.time(), event))
        if len(lines) > 200:
            lines = lines[-200:]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


def resolve_pet_sprite():
    """On-screen sprite size (w, h) in points, or None when no pet is installed.

    Mirrors what the desktop app draws: a 192x208 frame scaled by the resolved
    ``display.pet.scale`` (default 0.33). The claim chip feeds this to the
    companion so it can anchor next to the sprite, not the empty window.
    """
    if resolve_frames_dir() is None:
        return None
    scale = _resolved_scale(read_display_pet())
    return (PET_FRAME_WIDTH * scale, PET_FRAME_HEIGHT * scale)


def _rounded_sprite(sprite):
    """Round a ``(w, h)`` sprite tuple to whole points, or None if unusable."""
    if sprite is None:
        return None
    try:
        width, height = sprite
        width = int(round(float(width)))
        height = int(round(float(height)))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


_SHARED_OVERLAY = None


class Overlay:
    """Best-effort controller. publish() and close() never raise.

    The controller is also the shared transport handle for Accessibility
    captures: one companion process serves both drawing and AX queries, so the
    plugin only needs a single TCC grant.
    """

    def __init__(self):
        global _SHARED_OVERLAY
        self.proc = None
        self.starting = True
        self._io_lock = threading.RLock()
        self._stdout_buf = b""
        self._inbox = queue.Queue()
        self._reader_thread = None
        self._claim_chip_enabled = False
        # Optional callback for unsolicited companion event lines (chip clicks).
        self.on_event = None
        _SHARED_OVERLAY = self

    def _is_running(self):
        proc = self.proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except AttributeError:
            # Test doubles do not all implement poll().
            return True
        except Exception:
            return False

    def is_running(self):
        """Public read-only check for callers (the summon door)."""
        return self._is_running()

    def start(self, show_pet=True):
        """Spawn the companion, or re-assert the pet double on a live one.

        ``show_pet=False`` is the capture-only path: a companion spawned just to
        read the screen must not park a pet double on the user's desktop, and
        without a pet window the idle watchdog can retire that process too.
        A guide start passes True even when a petless companion is already up
        (a capture spawned it moments ago), so the double always appears.
        """
        with self._io_lock:
            if self._is_running():
                if show_pet:
                    self._send_pet_op()
                if self._claim_chip_enabled:
                    # Re-assert with the sprite so a companion that came up
                    # petless (capture) still anchors the chip to the pet.
                    self.claim_chip(True, sprite=resolve_pet_sprite())
                return True, "already running pid %d" % self.proc.pid
            self.proc = None
            self._stdout_buf = b""
            self._inbox = queue.Queue()
            try:
                ok, detail = build_if_needed()
                if not ok:
                    self.starting = False
                    return False, detail
                self.proc = subprocess.Popen(
                    [binary_path()],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                self.starting = False
                self._start_reader()
                # Send the pet once, before any ring can arrive, so the double is
                # already loaded when the first step is highlighted.
                if show_pet:
                    self._send_pet_op()
                if self._claim_chip_enabled:
                    # Re-assert with the sprite so a companion that came up
                    # petless (capture) still anchors the chip to the pet.
                    self.claim_chip(True, sprite=resolve_pet_sprite())
                return True, "spawned pid %d" % self.proc.pid
            except Exception as exc:  # noqa: BLE001
                self.proc = None
                self.starting = False
                return False, str(exc)

    def _send_pet_op(self):
        try:
            pet_op = resolve_pet_op()
            if pet_op is not None:
                self._send(pet_op)
        except Exception:  # noqa: BLE001
            pass

    def claim_chip(self, enabled, sprite=None):
        """Show/hide the companion's pop-out pet click chip (idempotent).

        ``sprite`` is the on-screen ``(w, h)`` of the pet sprite, in points.
        Including it lets the companion anchor the chip to the sprite inside the
        mostly-empty pop-out window. ``None`` (no pet, or an old caller) sends
        the legacy two-key op unchanged.
        """
        self._claim_chip_enabled = bool(enabled)
        op = {"op": "claim_chip", "enabled": self._claim_chip_enabled}
        rounded = _rounded_sprite(sprite)
        if rounded is not None:
            op["sprite_w"] = rounded[0]
            op["sprite_h"] = rounded[1]
            op["pad_bottom"] = CLAIM_CHIP_PAD_BOTTOM
        self._send(op)

    def _start_reader(self):
        """Read unsolicited events (mini-window clicks) off the companion pipe.

        Requests still go through ``request``; its caller waits on the same
        inbox, so event lines never get mistaken for an AX response.
        """
        proc = self.proc
        stdout = getattr(proc, "stdout", None)
        if stdout is None:
            return

        def _read():
            journal_click("reader-start pid=%s" % getattr(proc, "pid", "?"))
            while True:
                try:
                    raw = stdout.readline()
                except Exception:  # noqa: BLE001
                    break
                if not raw:
                    break
                if isinstance(raw, bytes):
                    text = raw.decode("utf-8", "replace")
                else:
                    text = str(raw)
                text = text.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except Exception:  # noqa: BLE001
                    continue
                self._handle_message(message)
            if self.proc is proc:
                self.proc = None

        thread = threading.Thread(target=_read, name="butler-overlay-reader", daemon=True)
        self._reader_thread = thread
        thread.start()

    def _handle_message(self, message):
        if not isinstance(message, dict):
            return
        if message.get("op") in ("claim_click", "launcher_click"):
            callback = self.on_event
            journal_click("pipe-%s cb=%s" % (
                message.get("op"), "set" if callable(callback) else "none"))
            if callable(callback):
                try:
                    callback(message)
                except Exception:  # noqa: BLE001 - a click must never kill the pipe
                    pass
            return
        self._inbox.put(message)

    def request(self, op, timeout=5.0):
        """Send one op and return its single JSON response line, or None.

        This is the transport used by capture.py. It never raises and enforces
        a hard read deadline so a stalled companion cannot hang the guide poll.
        """
        with self._io_lock:
            try:
                budget = max(0.0, float(timeout))
            except (TypeError, ValueError):
                budget = 5.0
            deadline = time.monotonic() + budget
            try:
                if not self._is_running():
                    ok, _detail = self.start(show_pet=False)
                    if not ok:
                        return None
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return None
                if self._reader_thread is not None and self._reader_thread.is_alive():
                    while True:
                        try:
                            self._inbox.get_nowait()
                        except queue.Empty:
                            break
                self._send(op)
                if not self._is_running():
                    return None
                if self._reader_thread is not None and self._reader_thread.is_alive():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        return None
                    try:
                        return self._inbox.get(timeout=remaining)
                    except queue.Empty:
                        return None
                return self._read_response(remaining)
            except Exception:  # noqa: BLE001
                return None

    def _read_response(self, timeout):
        stdout = getattr(self.proc, "stdout", None)
        if stdout is None:
            return None
        try:
            fd = stdout.fileno()
        except Exception:  # noqa: BLE001
            fd = None
        if fd is None:
            # Test doubles are often in-memory streams without a selectable
            # fd; production pipes always take the deadline path below.
            try:
                raw = stdout.readline()
            except Exception:  # noqa: BLE001
                return None
            if not raw:
                return None
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            text = text.strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except Exception:  # noqa: BLE001
                return None
        try:
            deadline = time.monotonic() + max(0.0, float(timeout))
        except (TypeError, ValueError):
            deadline = time.monotonic() + 5.0

        while True:
            newline = self._stdout_buf.find(b"\n")
            if newline >= 0:
                raw = self._stdout_buf[:newline]
                self._stdout_buf = self._stdout_buf[newline + 1:]
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    return json.loads(text)
                except Exception:  # noqa: BLE001
                    return None

            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return None
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except Exception:  # noqa: BLE001
                return None
            if not ready:
                return None
            try:
                chunk = os.read(fd, 65536)
            except Exception:  # noqa: BLE001
                return None
            if not chunk:
                # EOF: the companion exited (or was rebuilt away).
                self.proc = None
                return None
            self._stdout_buf += chunk
            if len(self._stdout_buf) > 8 * 1024 * 1024:
                self._stdout_buf = b""
                return None

    def publish(self, state):
        if not self.proc or not self.proc.stdin:
            return
        op = {"op": "ring", "box": state.get("current_box"),
              "label": state.get("current_label"),
              "say": (state.get("state") or {}).get("say"),
              "step": (state.get("state") or {}).get("idx", 0) + 1,
              "total": len(state.get("steps") or [])}
        self._send(op)

    def idle(self):
        self._send({"op": "idle"})

    def done(self):
        """Tell the companion the guide finished so its double can wave off."""
        self._send({"op": "done"})

    def quit(self):
        with self._io_lock:
            self._send({"op": "quit"})
            if self.proc:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
            self.proc = None
            self._stdout_buf = b""

    def _send(self, op):
        with self._io_lock:
            if not self.proc or not self.proc.stdin:
                return
            try:
                self.proc.stdin.write((json.dumps(op) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except Exception:
                self.proc = None


def shared_overlay():
    """Return the process-wide companion handle, creating it on first use."""
    global _SHARED_OVERLAY
    if _SHARED_OVERLAY is None:
        _SHARED_OVERLAY = Overlay()
    return _SHARED_OVERLAY


def summon_double(hold_seconds=2.2):
    """Spawn the pet double for a summon, then let it wave off and exit.

    This is the engine overlay path, not a second binary: no petdex pet means
    resolve_frames_dir() returns None and we skip the spawn entirely. A live
    guide already owns the companion, so summoning never steals or duplicates
    it. Returns True only when a double was actually spawned.
    """
    if resolve_frames_dir() is None:
        return False
    overlay = shared_overlay()
    if overlay.is_running():
        return False
    ok, _detail = overlay.start(show_pet=True)
    if not ok:
        return False

    def _farewell():
        try:
            time.sleep(max(0.2, float(hold_seconds)))
        except (TypeError, ValueError):
            time.sleep(2.2)
        try:
            overlay.done()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_farewell, name="butler-summon-farewell",
                     daemon=True).start()
    return True
