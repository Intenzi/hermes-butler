---
name: conventions
description: "Butler plugin dev conventions: flat step keys, restart batching, checklist page shape, TCC single-subject design. Load for butler plugin code work."
---

# Butler implementation conventions

Working rules accumulated across the butler plugin's development (guides,
engine, overlay, kanban board). Each section is a hard convention with a
concrete example; do not "improve" them without the user's explicit go-ahead.
Load this skill when a fresh session picks up a butler task from the kanban
`butler` board or edits butler plugin code — the sibling skill `butler:follow`
covers guide *authoring*; this one covers *how we build and coordinate*.

## 1. Flat step keys (no hierarchical step identifiers)

A guide step is ONE flat dict: `say` plus `find.*` and `expect.*` prefixed
keys, in a single level. Never invent nesting (`find: {label: ...}` is
tolerated for the hands-off special case only), never add step-id or
tree-shaped keys (`id`, `id: "1a"`, `step: "1a.i"`). Order and index carry
all structure — the checklist renders `Step 1:`, `Step 2:` from array order,
so hierarchical identifiers buy nothing and break the renderer.

```jsonc
// DO — one flat mapping per step
{"say": "Click Network",  "find.label": "Network", "expect.window": "Network"}
{"say": "Open System Settings first", "find": {}, "expect.app": "System Settings"} // hands-off: no ring, no label

// DON'T — nested ids / hierarchical shapes
{"id": "step-1a", "find": {"label": {"equals": "Network"}}}          // invented depth
{"say": "...", "substeps": [{"say": "..."}]}                        // hierarchy the engine never reads
```

Why it is hard: before the 2026-11-06 `_normalize_step` fix, a malformed
nested shape crashed `guide_start` at `steps[0]["find"]` and left an orphan
guide running silently (no say, no ring, no verify, no stall — the "pet said
nothing" failure). The normalizer folds flat `find.*` / `expect.*` keys and
rejects a step that could never verify ("no verifiable steps"), but it does
not read sub-keys two levels deep. Keep depth at exactly one.

## 2. Restart batching (never fix one-by-one)

A gateway restart loads ONLY what was on disk before it began. Dribbling
fixes across restart cycles makes the user re-test the old behavior and
report the bug as still broken — each restart costs a fresh grant-laden
boot and a user test cycle. Therefore: land the WHOLE changeset (code +
tests), run the test suite, then request exactly ONE restart and say what
it carries.

- Batch example — good: engine fix + overlay fix + skill copy edit on disk,
  tests green, one restart, REPORT before restart...
- One-by-one — bad: engine fix... restart... overlay fix... restart...
- Not a restart: plugin-data runtime knobs (`<plugin data
  dir>/config.json`: `poll_seconds`, `step_timeout_seconds`) are read fresh
  every poll tick with no restart; the desktop half (`desktop/plugin.js`)
  hot-reloads via the linked checkout (the app re-syncs on save — verify the
  copy actually refreshed with `cmp` instead of assuming).
- After one batched restart, re-verify before claiming: the restart that
  was supposed to load the fix can blow the 10 s plugin load budget
  (`plugins.load_timeout_seconds`, raised to 30 in config).

## 3. Checklist page shape (canonical)

The butler surface renders every guide as a vertical task checklist. This
shape is frozen by user decision — do not regress it to chip strips or an
always-expanded report.

LIVE guide:
- One row per step: 18px box + `Step N: <say>` text. Box states: `✓`
  verified (green outline), bright-bordered highlight = current step,
  empty = pending.
- The instruction line (current step's say) and any stall note render BELOW
  the checklist; the smoke counter ("pet.say smoke: N says fired (last
  HH:MM)") stays on the pane to distinguish plugin-side from
  renderer-side failure.

DONE / STOPPED guide:
- Collapses into a closed `<details>` dropdown per guide, newest first:
  `▸ <title> · N/M verified · <status>`. Up to 8 visible + a
  "show N more" footer; expanding shows the full completed checklist.

Empty state: exact string "No butler task running".

Mapping: a guide plan (max six steps per `butler:follow`) maps 1:1 onto
rows — flat step keys from convention 1 are what this shape consumes.

## 4. TCC single-subject design

macOS TCC trust attaches per binary, so a one-off fix that performs screen
access in whichever Python/Node binary happens to host it re-breaks grants
on every Hermes update or install (binary paths drift: command-tools python,
runtime venv, hash-built sugar). Launcher chains are worse — macOS names
the WRONG subject in the grant prompt.

Rules:
- ALL screen access (capture, AX reads) lives in the plugin's own bundled
  compiled Swift companion (`butler_core/services/ButlerOverlay` source →
  compiled at first use); Python `capture()` is a transport that talks to
  it. Port new screen-driven work INTO the companion, never into a second
  binary location.
- Grants name exactly one stable, plugin-relative subject: "Butler
  companion - the butler plugin's bundled helper", with no file paths, plus
  the one reason sentence ("Because the butler reads the screen through its
  own small helper, macOS asks once and remembers across updates.").
  Same wording in doctor fix strings and README.
- A probe running outside the granted subject LIES ABOUT ABSENCE — no
  Accessibility grant also yields empty window lists and "trusted: False".
  Check grants (doctor) before concluding a UI element does not exist.
- Shippability gate for every TCC or grant change: the walkthrough must
  survive `plugin install → grant → walkthrough` on a machine that has
  never seen the repo.

## 5. Capture depth must reach Electron web content

Guides inside Electron apps (Hermes desktop, VS Code, Slack) need a deep AX
walk. Measure before blaming the plan: `butler_capture(target=App, depth=12,
max=1200)` returns only containers, while `depth=24` returns the real
controls. Electron nests its DOM well past level 12, so app chrome ("Open
settings", "Search") rings fine while everything inside an overlay or page
(nav rows, tabs, buttons) is invisible — the step never verifies and the
guide stalls at idx 0 with `overlay: "waiting"` and `current_box: null`.

The engine therefore captures shallow first (depth 12) and re-walks once at
depth 24 (`engine.py`: `SHALLOW_*` / `DEEP_*`) only when the shallow snapshot
cannot satisfy the step, rate-limited to one deep walk per second. Never
collapse that back to a single shallow call, and never invent a `find.label`
for an Electron control you have not seen in a depth-24 capture.

Stall triage order: `butler_guide_status` (idx, overlay, `last_capture_app`) →
deep `butler_capture` of the window the user says they are looking at. A
`last_capture_app` that is not the app under discussion means a poll read the
wrong frontmost window; the plan is fine, the focus was not.

## 6. Pet double parity and companion lifetime

The companion draws a copy of the user's desktop pet, so it must animate like
one and it must not outlive the work it was spawned for.

- Step the sprite `loopMs / frameCount(row)` — LOOP_MS is 1100 in
  `agent/pet/constants.py`, and a `pet.json` may carry its own `loopMs`.
  Never hardcode a frame rate: the old fixed 0.125 s made the double blink
  ~1.5x faster than the real pet, which the user reads as a rendering bug in
  *their* pet.
- A finished guide fades the double AND exits the process. `guide_stop()` is
  not the only end path, so the farewell itself schedules the exit, and a
  ~10 min idle watchdog covers the paths that leave no windows.
- Capture-only spawns pass `show_pet=False`: reading the screen must not park
  a pet on the user's desktop, and a petless companion is one the watchdog can
  retire. A guide start re-asserts the pet op on an already-running companion,
  or the second guide of a session shows no double at all.
- Before "the pet animates wrong" becomes code, check the artwork: count
  closed-eye frames per loop in the sheet (terminal-ghost-2 has 2 of 6, i.e.
  two blinks per 1.1 s loop) and confirm the rate with the user first.
- The desktop half (`desktop/plugin.js`) does NOT reliably hot-reload on this
  app build. The linked checker compares CONTENT (`sameFile(target, entry)` in
  `electron/desktop-plugins-root.ts`), so writing the installed copy yourself
  makes the two equal and no reload ever fires; a copy that differs also did not
  re-sync within 14 s. Treat a desktop-plugin change as needing an app restart,
  and confirm the installed copy with `cmp` before telling the user to test.
- The pop-out claim chip anchors to the pet *sprite*, not the pop-out window:
  the window is `max(240, frame*scale + pad)` x `max(300, frame*scale + pad)`, so
  for a normal 192x208 pet at scale 0.33 the window is a 240x300 box around a
  63x69 sprite laid out bottom-centred with 24 pt of bottom padding. Anchoring to
  the window puts the chip ~70 pt above and ~90 pt right of the pet, which the
  user reads as "not next to my pet". The sprite size travels in the claim op
  (`sprite_w` / `sprite_h` / `pad_bottom`) because the chip's companion is
  spawned petless and cannot know it.
- The chip's placement is EVENT-DRIVEN: the companion holds an `AXObserver` on
  the Hermes app (window lifecycle) and on each of its window elements
  (`AXWindowMoved` / `AXWindowResized` / `AXUIElementDestroyed`), reads the frame
  straight off the notified element, and coalesces to one placement per 40 ms.
  A 3.0 s tick reconciles what notifications miss (pop-out created while
  unobserved, display change, wake); the 0.5 s poll survives only as the
  fallback when `AXObserverCreate` fails. Measured: 86 move events in one pet
  drag, ~17 ms apart — never re-enumerate `CGWindowList` on that hot path.
- Hiding the chip must NOT stop the observer: the slow tick hides it whenever
  the pop-out is closed or on another Space, so tying observer lifetime to chip
  visibility rebuilds the observer every few seconds. Stop it only on disarm and
  shutdown.
- The claim chip is a 26x24 hat badge (`🎩`) in the sprite's upper-right: the first
  placement candidate is `x = pet.maxX + gap`, `y = pet.minY + gap`, i.e. beside
  the head, deliberately NOT above it — the strip above the sprite is where the
  pet's own speech bubble ("tinkering…") renders. A borderless `NSWindow` cannot
  become key by default and then never routes clicks into its content view, so the
  chip is built from `ClaimChipWindow` (overrides `canBecomeKey` true,
  `canBecomeMain` false) and its view overrides `acceptsFirstResponder`.
- Never verify the chip's click by writing a `summon_pending` row into
  `state.json` yourself: `summon_ack` clears the ENGINE's in-memory pending only
  and does not rewrite the file, so a hand-written row survives forever whether
  or not the desktop consumed it. The only honest end-to-end test is a real
  mouse click on the chip (synthetic `CGEventPost` clicks are dropped without an
  Accessibility grant, and `screencapture` / cua-driver captures fail on this
  machine for the same reason).
- The companion is owned by whichever process serves `/summon/claim` — the
  **plugin API process**, not the agent runtime that runs `register()`. If that
  process never calls `engine.init()`, the chip arms fine and EVERY click is
  dropped silently: no data dir, `_OVERLAY.on_event is None`, and `_write_state`
  no-ops. `dashboard/plugin_api._engine_module()` must initialise the engine on
  first use, and `_write_state` must merge the other owner's block (guide vs
  `summon_pending`) instead of overwriting the shared `state.json`.
- A click is traceable hop by hop through `chip-events.log` in the plugin data
  dir: `click` (view) → `emit` (stdout) → `pipe-claim cb=set|none` (reader
  thread) → `engine-pending` (engine callback). Empty `state.json` after `emit`
  means the click died in Python, not in the window.
- Two plugin-hosting Python processes (the gateway and the tool worker) each
  own their own companion: a `butler_capture` call from the tool worker adds a
  second `ButlerOverlay` next to the gateway's claim-chip one. They do not
  fight (rings only come from the guide's process, the chip from the gateway's)
  but each is a real process; the petless one retires on its idle watchdog, and
  a proper single-companion server is not built yet — do not assume "one
  process" when counting overlays.

## 7. The pop-out pet window is titled "Hermes" and lives on CGWindow layer 3

Anything that has to find the user's popped-out pet (the claim chip anchors to
it) must identify it by **owner pid + shape only**. Measured on this machine
(`com.nousresearch.hermes`): the pet panel is an AXWindow whose title is the
literal string `"Hermes"` (not empty), sized 240x300 at (984,160), and in the
CGWindow list it is an **NSPanel on layer 3**, while the main window is
1280x803 on layer 0. Two "obvious" heuristics both fail silently:

- `title.isEmpty` — the panel is titled, so every candidate is rejected.
- `axTopmostLayer0Windows()` — the panel is layer 3, so it is never listed.
- `kCGWindowName` is empty without Screen Recording, and AX `AXWindows` can
  return an empty array before the app's a11y tree warms up, so pool AX frames
  AND an all-layer CGWindow walk by owner pid on every tick instead of treating
  CG as a fallback.

The failure mode is invisible from the outside: `positionClaimChip()` gets nil,
calls `hideClaimChip()` every 0.5 s, and the chip window is created and
destroyed forever — "nothing is around my pet", with the companion healthy and
no log line anywhere. Verify a chip fix with the CGWindow list, not by looking:
a live chip is an 84x24 window owned by the `ButlerOverlay` pid, 10 pt clear of
the pet panel.

## 8. LOOK: pointer gestures, self-hit, and the pet as an answer surface

LOOK reads the control under a point the user chose with the pointer. Three
rules keep it honest.

- **No look hot key.** The user rejected ⌘⇧L outright: looking must be a
  pointer gesture, never a chord and never a hop into the Hermes chat. The
  shipped gesture is: drag the hat onto the thing → a non-key card at the drop
  point → click the hat within 90 s to ask about *that* element.
- **Our own window wins every AX hit-test.** The hat is a `.statusBar`-level
  window, so `AXUIElementCopyElementAtPosition` answers with it wherever it is:
  first as `Button · "Ask butler"` (the chip's accessibility label), then, once
  parked, as `Window · ButlerOverlay`. `orderOut` is applied by the window
  server on the NEXT pass, so an immediate hit-test still sees it. Fix shape:
  park the window at (-20000, -20000), hit-test on the next run-loop turn,
  ignore any descriptor whose app is this process, retry once after 80 ms, then
  fall back to `NSWorkspace.menuBarOwningApplication`'s focused window (an
  accessory helper never owns the menu bar). Never print our own process name.
- **Electron answers with an unlabelled container.** The control is an
  `AXGroup` with an empty title, so the descriptor walks down 2 levels for a
  labelled child and then up 4 for a labelled ancestor before falling back to a
  window line. "Group" alone is never an answer.

### The pet is the answer surface

The pet already speaks (`ctx.pet.say(line, {ttlMs, id})`, driven by a 2 s beat in
`desktop/plugin.js`). To get the agent's answer onto that channel without
reading sessions or adding a transport, the agent calls a tool that writes a
file: `butler_say({"text": …})` → `say.json` (`{id, text, at}`, 280-char cap on
a word boundary) → `GET /state` merges a `say` key → the desktop says it once
per `id` at a 15 s ttl. LOOK turns use `LOOK_FRAMING_TEMPLATE`
(`butler_core/summon.py`), which instructs the agent to answer in at most two
sentences, call `butler_say` with that same sentence, and keep the full
explanation for the chat. The bubble is one line — detail belongs in chat — and
without an `at`-freshness gate a desktop reload re-speaks the last answer.

### Hermes already ships a HUD — pitch accordingly

`⌘⇧H` detaches the chat into an always-on-top bar whose *position* is the
context ("this / here" resolve to the app and screen under it), with ⌘⇧G to
snap it to the pointer and tap-⌘⌥ to summon it (macOS: needs Input Monitoring).
So "ask about what's on screen" is table stakes inside Hermes. What the HUD does
NOT do: element-level reads (role/label/enabled/value of the control under a
point), stepped guidance with per-step verification, passive AX watching, or an
on-screen character that carries state. Third-party blurbs claiming the HUD
draws annotations over your screen are wrong — the only annotation code
(`preview-annotate-host`) annotates the in-app preview pane. Lead the pitch with
Walk + verification, not screen Q&A.
