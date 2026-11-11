"""Companion overlay controller.

Owns the click-through companion window that rings controls over ANY app and
hosts the walking pet double. The renderer is a separate bundled Swift
process. On first use the plugin downloads the helper this repository's GitHub
Actions workflow built and attested, verifies it against the release's own
SHA-256, and only compiles the bundled Swift source when that download is not
available (disclosed in the README). If no route produces a helper, the guide
falls back to in-app speech bubbles only - never hard-fails.

Protocol: one JSON line per op on stdin -
  {"op":"ring","box":[x,y,w,h],"label":"...","say":"...","step":3,"total":7}
  {"op":"chip","step":3}
  {"op":"pet","frames_dir":"...","scale":0.33,"pet_enabled":true}
  {"op":"claim_chip","enabled":true}
  {"op":"claim_chip","enabled":true,"sprite_w":63,"sprite_h":69,"pad_bottom":24}
  {"op":"watch_start","app":"Slack","until":"Export complete"}
  {"op":"watch_stop"}
  {"op":"launcher"}
  {"op":"done"}
  {"op":"idle"}
  {"op":"quit"}

The companion writes one event line on clickable mini-windows:
  {"op":"launcher_click"}
  {"op":"claim_click"}
  {"op":"summon_text","text":"...","id":"a1b2c3d4"}
  {"op":"look_text","text":"...","id":"a1b2c3d4","destination":"recent",
   "target":{"app":"Preview","role":"Button","label":"Export","box":[1,2,3,4]}}
  {"op":"watch_ack"}   the hat menu's Acknowledge watch hit button

Ambient WATCH lines (passive AX observation; never a click):
  {"op":"watch-started","app":"Slack","until":"Export complete","pid":123}
  {"op":"watch-event","seq":7,"at":...,"kind":"focus","app":"Slack",
   "role":"AXTextArea","label":"message input","window":"general — Slack"}
  {"op":"watch-hit","app":"Slack","until":"Export complete","label":"Export complete"}
  {"op":"watch-stopped","events":12,"reason":"app-quit"}
"""
import hashlib
import json
import os
import platform
import queue
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

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


def buildinfo_path():
    """Sidecar written by scripts/build-companion.sh next to the binary."""
    return os.path.join(os.path.dirname(__file__), "services", ".buildinfo")


def source_sha256(path=None):
    """Content hash of the Swift source, used instead of its mtime."""
    target = path or binary_source_path()
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_buildinfo():
    try:
        with open(buildinfo_path(), "r", encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _current_archs(exe):
    """Best-effort arch list for a rebuilt binary (lipo, else the host)."""
    lipo = shutil.which("lipo")
    if lipo is not None:
        try:
            proc = subprocess.run([lipo, "-archs", exe], timeout=10,
                                  check=True, capture_output=True, text=True)
            archs = proc.stdout.split()
            if archs:
                return archs
        except Exception:  # noqa: BLE001
            pass
    return [platform.machine() or "unknown"]


def _write_buildinfo_best_effort(src, exe):
    """Record what a swiftc rebuild produced so it is not rebuilt every time.

    The sidecar is a cache hint, never a requirement; an install directory that
    is not writable keeps working and simply re-checks the hash next run. Only
    I/O failures are tolerated here - a bug in this function should surface in
    tests instead of silently disabling the cache contract.
    """
    try:
        info = {
            "source_sha256": source_sha256(src),
            "archs": _current_archs(exe),
            "built_at": int(time.time()),
        }
        with open(buildinfo_path(), "w", encoding="utf-8") as handle:
            json.dump(info, handle, sort_keys=False)
            handle.write("\n")
    except OSError:
        pass


#: Every request the helper download makes gets its own deadline, in seconds.
DOWNLOAD_TIMEOUT = 20
#: Set BUTLER_NO_DOWNLOAD=1 to refuse the release download and compile instead.
NO_DOWNLOAD_ENV = "BUTLER_NO_DOWNLOAD"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LAST_JOURNAL = None


def companion_url_path():
    """One-line release-asset URL template shipped beside the binary."""
    return os.path.join(os.path.dirname(binary_path()), ".companion_url")


def plugin_yaml_path():
    """The plugin manifest that names the release tag (``version:``)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "plugin.yaml")


def plugin_version():
    """``version:`` from plugin.yaml, without a YAML dependency."""
    try:
        with open(plugin_yaml_path(), "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("version:"):
                    value = _unquote(line.split(":", 1)[1])
                    return value or None
    except OSError:
        return None
    return None


def download_url():
    """Resolve the release asset URL for the installed version, or None.

    The template lives in the repo (``services/.companion_url``) so the remote
    host is auditable from the source tree; ``{version}`` comes from
    ``plugin.yaml`` so the download matches the installed plugin exactly.
    """
    try:
        with open(companion_url_path(), "r", encoding="utf-8") as handle:
            template = handle.readline().strip()
    except OSError:
        return None
    if not template:
        return None
    if "{version}" in template:
        version = plugin_version()
        if not version:
            return None
        template = template.replace("{version}", version)
    return template


def _journal(message):
    """One stderr line per resolution step: never spammy, never raising."""
    global _LAST_JOURNAL
    text = "butler companion: %s" % message
    if text == _LAST_JOURNAL:
        return
    _LAST_JOURNAL = text
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - a closed stderr must not break the guide
        pass


def _remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _http_get(url):
    """Read one URL with the fixed download deadline; raises on any failure."""
    response = urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT)
    try:
        return response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - a failed close cannot unsave us
                pass


def _parse_sha256(raw):
    """The first token of a ``shasum`` file, or None when it is not a digest."""
    try:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception:  # noqa: BLE001
        return None
    tokens = text.split()
    if not tokens:
        return None
    candidate = tokens[0].lower()
    return candidate if _SHA256_RE.match(candidate) else None


def _release_buildinfo(url):
    """The release's own ``.buildinfo`` payload, or None when not published."""
    base = url.rpartition("/")[0]
    candidates = []
    if base:
        # The workflow uploads ``butler_core/services/.buildinfo``, whose asset
        # name is the bare basename in the release directory.
        candidates.append(base + "/.buildinfo")
    # A renamed sibling is accepted too, so the asset can be made discoverable
    # without changing this resolver.
    candidates.append(url + ".buildinfo")
    for candidate in candidates:
        try:
            raw = _http_get(candidate)
        except Exception:  # noqa: BLE001 - an optional asset may 404
            continue
        try:
            info = json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(info, dict):
            return info
    return None


def _write_download_buildinfo(url, exe):
    """Record what the download installed; a cache hint, never a requirement."""
    info = _release_buildinfo(url)
    if info is None:
        info = {"archs": _current_archs(exe), "built_at": int(time.time())}
        try:
            info["source_sha256"] = source_sha256(binary_source_path())
        except OSError:
            # Source is not shipped in an installed plugin; the sidecar then
            # has no hash and the next run trusts the verified binary.
            pass
    try:
        with open(buildinfo_path(), "w", encoding="utf-8") as handle:
            json.dump(info, handle, sort_keys=False)
            handle.write("\n")
    except OSError:
        pass


def fetch_binary():
    """Download, verify, and install the attested universal helper.

    Returns ``(True, "downloaded")`` or ``(False, "download_failed: <reason>")``.
    The release's own ``.sha256`` asset supplies the digest, so the binary and
    the checksum travel together and a mismatch never reaches the executable.
    """
    url = download_url()
    if not url:
        return False, "download_failed: no release url configured"

    # Refuse to install what cannot be verified: no checksum, no download.
    try:
        expected = _parse_sha256(_http_get(url + ".sha256"))
    except Exception as exc:  # noqa: BLE001
        return False, "download_failed: sha256 asset unavailable: %s" % exc
    if not expected:
        return False, "download_failed: sha256 asset malformed"

    partial = binary_path() + ".download"
    try:
        payload = _http_get(url)
    except Exception as exc:  # noqa: BLE001
        _remove_quietly(partial)
        return False, "download_failed: %s" % exc

    # Stage first, verify the staged bytes, and only then touch the real file.
    try:
        parent = os.path.dirname(partial)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(partial, "wb") as handle:
            handle.write(payload)
    except OSError as exc:
        _remove_quietly(partial)
        return False, "download_failed: %s" % exc

    try:
        digest = hashlib.sha256()
        with open(partial, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
    except OSError as exc:
        _remove_quietly(partial)
        return False, "download_failed: %s" % exc
    if actual != expected:
        _remove_quietly(partial)
        return False, "download_failed: sha256 mismatch"

    try:
        os.chmod(partial, 0o755)
        os.replace(partial, binary_path())
    except OSError as exc:
        _remove_quietly(partial)
        return False, "download_failed: %s" % exc
    _write_download_buildinfo(url, binary_path())
    return True, "downloaded"


def ensure_binary():
    """Get the screen helper by the cheapest safe route.

    Returns ``(ok, detail)``. Resolution order, each step tried only when the
    one before it could not produce a usable helper:

    1. ``present`` - a binary whose ``.buildinfo`` hash matches the bundled
       source (or that has no hash to compare) is used as-is.
    2. ``downloaded`` - fetch the attested release asset, verify it against the
       release's own SHA-256, and install it atomically. Skipped when
       ``BUTLER_NO_DOWNLOAD`` is set or no ``.companion_url`` is shipped.
    3. ``compiled`` - compile the bundled Swift source with ``swiftc``.
    4. ``(False, ...)`` - ``download_failed`` when a compile was attempted and
       failed too, ``swiftc_not_found`` when the source is there but no
       compiler is, ``helper-unavailable`` when nothing could be tried. The
       doctor turns any of these into one remedy.

    Staleness is decided by the source SHA-256 in ``.buildinfo``, never by
    mtime: after ``git clone`` every file gets a fresh mtime. A missing sidecar
    still means "trust the binary" - doctor's capture probe reports a genuinely
    broken companion. Nothing here raises: no helper is a report, not a crash.
    """
    exe = binary_path()
    src = binary_source_path()
    exe_ready = os.path.exists(exe) and os.access(exe, os.X_OK)

    if exe_ready:
        info = _read_buildinfo()
        if info is None or not info.get("source_sha256"):
            _journal("present (no recorded source hash)")
            return True, "present"
        try:
            current = source_sha256(src)
        except OSError:
            # In an installed plugin the source may not be shipped; its
            # absence is not evidence that the binary is stale.
            _journal("present (source unavailable, trusting the binary)")
            return True, "present"
        if info.get("source_sha256") == current:
            _journal("present")
            return True, "present"
        _journal("stale (source hash changed)")

    download_failure = None
    url = download_url()
    if os.environ.get(NO_DOWNLOAD_ENV):
        _journal("download disabled by %s" % NO_DOWNLOAD_ENV)
    elif url is None:
        _journal("download unavailable (no .companion_url / version)")
    else:
        ok, detail = fetch_binary()
        if ok:
            _journal("downloaded %s" % url)
            return True, "downloaded"
        download_failure = (
            detail if str(detail).startswith("download_failed")
            else "download_failed: %s" % detail)
        _journal(download_failure)

    if not os.path.exists(src):
        # The download failure, if any, is already journaled above; the report
        # only needs the state the user can act on.
        _journal("helper-unavailable (no source to compile)")
        return False, "helper-unavailable"
    if shutil.which("swiftc") is None:
        _journal("swiftc_not_found")
        return False, "swiftc_not_found"

    _journal("compiling the bundled Swift source")
    # Compile beside the target and swap only on success. An in-place swiftc
    # run can truncate the shipped binary before failing, which would turn a
    # working companion into "needs a compiler" on a machine that has none.
    staged = exe + ".new"
    try:
        subprocess.run(["swiftc", "-O", "-o", staged, src], timeout=120,
                       check=True, capture_output=True)
        os.replace(staged, exe)
    except Exception as exc:  # noqa: BLE001
        _remove_quietly(staged)
        if download_failure:
            # Neither route produced a helper; report the one a reinstalled
            # plugin would fix first (the release download).
            _journal(download_failure)
            return False, download_failure
        _journal("compile_failed: %s" % exc)
        return False, "compile_failed: %s" % exc
    _write_buildinfo_best_effort(src, exe)
    _journal("compiled")
    return True, "compiled"


#: Back-compat name: the controller and doctor call build_if_needed().
build_if_needed = ensure_binary



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
        op = message.get("op")
        if op == "watch_ack":
            # The hat's Acknowledge watch hit button: one event line, routed to
            # the same engine door `butler_watch_ack` uses (never the request
            # inbox, which waits for an AX reply). Journal the hop for support.
            journal_click("watch-ack:menu")
            try:
                from . import engine as bp_engine
                bp_engine.ack_watch()
            except Exception:  # noqa: BLE001 - a menu click must not kill the pipe
                pass
            return
        if op in ("claim_click", "launcher_click", "summon_text", "look_text"):
            callback = self.on_event
            journal_click("pipe-%s cb=%s" % (
                message.get("op"), "set" if callable(callback) else "none"))
            if callable(callback):
                try:
                    callback(message)
                except Exception:  # noqa: BLE001 - a click must never kill the pipe
                    pass
            return
        # Ambient watch lines are unsolicited too, but they must never land in
        # the request inbox: `watch_start` waits for its own ack, and a stream
        # of focus/value events would starve it. They go straight to the
        # engine's ring instead (no journal: an event is not a click).
        if op in ("watch-event", "watch-hit", "watch-stopped"):
            callback = self.on_event
            if callable(callback):
                try:
                    callback(message)
                except Exception:  # noqa: BLE001 - a bad event must not kill the pipe
                    pass
            return
        self._inbox.put(message)

    def send(self, op):
        """Fire-and-forget op: no reply is expected, nothing is awaited.

        Used by the ambient watch's stop: the companion answers a stop with an
        unsolicited `watch-stopped` line (routed to the event callback, not the
        request inbox), so a `request()` here would sit out its whole timeout.
        Never raises.
        """
        try:
            self._send(op)
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
