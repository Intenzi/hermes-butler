"""Companion overlay controller.

Owns the click-through companion window that rings controls over ANY app and
hosts the walking pet double. The renderer is a separate bundled Swift
process, compiled on first use (disclosed). If it is missing or refuses to run,
the guide falls back to in-app speech bubbles only - never hard-fails.

Protocol: one JSON line per op on stdin -
  {"op":"ring","box":[x,y,w,h],"label":"...","say":"...","step":3,"total":7}
  {"op":"chip","step":3}
  {"op":"pet","frames_dir":"...","scale":0.33,"pet_enabled":true}
  {"op":"launcher"}
  {"op":"done"}
  {"op":"idle"}
  {"op":"quit"}
"""
import json
import os
import select
import shutil
import subprocess
import threading
import time

DEFAULT_PET_SCALE = 0.33
MIN_PET_SCALE = 0.2
MAX_PET_SCALE = 1.0


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
                return True, "already running pid %d" % self.proc.pid
            self.proc = None
            self._stdout_buf = b""
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
                # Send the pet once, before any ring can arrive, so the double is
                # already loaded when the first step is highlighted.
                if show_pet:
                    self._send_pet_op()
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
                self._send(op)
                if not self._is_running():
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
