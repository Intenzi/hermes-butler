"""Companion overlay controller.

Owns the click-through companion window that rings controls over ANY app and
hosts the walking pet double. The renderer is a separate bundled Swift
process, compiled on first use (disclosed). If it is missing or refuses to run,
the guide falls back to in-app speech bubbles only - never hard-fails.

Protocol: one JSON line per op on stdin -
  {"op":"ring","box":[x,y,w,h],"label":"...","say":"...","step":3,"total":7,"trail":[...]}
  {"op":"chip","step":3}
  {"op":"idle"}
  {"op":"quit"}
"""
import json
import os
import shutil
import subprocess


def binary_path():
    here = os.path.dirname(__file__)
    return os.path.join(here, "services", "ButlerOverlay")


def binary_source_path():
    return os.path.join(os.path.dirname(__file__), "overlay_swift", "ButlerOverlay.swift")


def build_if_needed():
    """Compile the bundled Swift overlay on first use (swiftc). Returns (ok, detail)."""
    exe = binary_path()
    if os.path.exists(exe) and os.access(exe, os.X_OK):
        return True, "present"
    src = binary_source_path()
    if not os.path.exists(src):
        return False, "source_missing"
    if shutil.which("swiftc") is None:
        return False, "swiftc_not_found"
    try:
        subprocess.run(["swiftc", "-O", "-o", exe, src], timeout=120,
                       check=True, capture_output=True)
        return True, "compiled"
    except Exception as exc:  # noqa: BLE001
        return False, "compile_failed: %s" % exc


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
