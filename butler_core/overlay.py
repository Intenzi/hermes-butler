"""Companion overlay controller.

Owns the click-through companion window that rings controls over ANY app and
hosts the walking pet double. The renderer is a separate bundled Swift
process, compiled on first use (disclosed). If it is missing or refuses to run,
the guide falls back to in-app speech bubbles only - never hard-fails.

Protocol: one JSON line per op on stdin -
  {"op":"ring","box":[x,y,w,h],"label":"...","say":"...","step":3,"total":7}
  {"op":"chip","step":3}
  {"op":"pet","frames_dir":"...","scale":0.33}
  {"op":"idle"}
  {"op":"quit"}
"""
import json
import os
import shutil
import subprocess

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
    """Read display.pet.{slug,scale} from config.yaml without a YAML dependency.

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
        if key in ("slug", "scale"):
            values[key] = _unquote(value)

    return values


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
    return {
        "op": "pet",
        "frames_dir": frames_dir,
        "scale": _resolved_scale(read_display_pet()),
    }


class Overlay:
    """Best-effort controller. publish() and close() never raise."""

    def __init__(self):
        self.proc = None
        self.starting = True

    def start(self):
        try:
            ok, detail = build_if_needed()
            if not ok:
                self.starting = False
                return False, detail
            self.proc = subprocess.Popen(
                [binary_path()],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.starting = False
            # Send the pet once, before any ring can arrive, so the double is
            # already loaded when the first step is highlighted.
            try:
                pet_op = resolve_pet_op()
                if pet_op is not None:
                    self._send(pet_op)
            except Exception:  # noqa: BLE001
                pass
            return True, "spawned pid %d" % self.proc.pid
        except Exception as exc:  # noqa: BLE001
            self.proc = None
            self.starting = False
            return False, str(exc)

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

    def quit(self):
        self._send({"op": "quit"})
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        self.proc = None

    def _send(self, op):
        if not self.proc or not self.proc.stdin:
            return
        try:
            self.proc.stdin.write((json.dumps(op) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception:
            self.proc = None
