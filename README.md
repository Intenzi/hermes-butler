# butler

Butler is a Follow-mode guide plugin for Hermes Agent on macOS: the agent plans
a short guide and the butler walks the real screen, step by step, with a ring
around the control, a one-line bubble, and live progress. Every step is
verified from the macOS Accessibility layer before the guide advances, so a
click that did not land stalls the guide instead of being counted. It is
species-free — it works with any petdex pet — and macOS-only in v0.2.

## Quick start

```
hermes plugins install file:///path/to/butler-plugin      # or the catalog/Git URL
hermes butler doctor --ask                                # grants + real diagnostics
hermes gateway restart                                    # the plugin loads at gateway start
```

Then quit and reopen the Hermes desktop app. The desktop half
(`desktop/plugin.js`) is copied into `desktop-plugins/butler/` when the app
starts; a running app keeps the copy it started with.

`hermes butler doctor --ask` raises the real macOS permission dialogs —
Accessibility is prompted by the plugin itself via
`AXIsProcessTrustedWithOptions`, Screen Recording via
`CGRequestScreenCaptureAccess` — and deep-links the two System Settings panes.
The CLI prints a JSON report; `--ask` is the only form that also raises the
dialogs.

These three commands work today and print JSON:

```
hermes butler doctor [--ask]
hermes butler capture [app]     # optional app-name substring
hermes butler status
```

## Grants

| Grant | Subject | Without it |
|---|---|---|
| Accessibility | `Butler companion - the butler plugin's bundled helper` | No capture, no guide, no overlay: every step reports `capture_failed` |
| Screen Recording | the Hermes host process | AX still works; the on-device OCR fallback (Chromium/gated pages) is unavailable |

Accessibility is what makes the rings and the step verification work: the
companion reads element labels and boxes through it. macOS requires the final
checkbox and one app restart by hand; the doctor tells you when it is your turn
and when you are clear.

## Two doors

There are two ways to summon the butler, and both open the same **Ask the
butler** bar.

- **The pet.** Installing the plugin is the opt-in; there is no separate
  switch. Clicking the pet inside the Hermes window opens the bar. When the pet
  is popped out into its own OS window, the bundled companion draws a 26x24 hat
  badge at the pet's upper right; the badge follows the pet live from AX window
  notifications (`AXWindowMoved` / `AXWindowResized`) instead of polling.
- **⌘⇧A.** "Ask the butler" (rebindable in Settings → Keybinds) opens a
  floating bar anywhere in the desktop. Enter sends, Escape or blur dismisses.
  Focus stays on the text input; the destination buttons are Tab-reachable
  after it.

The **Butler chat** destination is the most recent chat titled exactly
`Butler`; **New session** mints a fresh Butler-titled chat. The choice is kept
in the desktop's localStorage across openings, and the success toast names it.

## Usage

### Ask the butler

Summon the bar and hand it a text request. The bar posts the request to the
plugin's gateway route, which injects a real agent turn into the chosen chat.
The agent plans a short guide and starts it immediately; if the request is too
vague, it asks exactly one clarifying question as a normal chat reply, and you
answer it right there. Typing a plain message in any chat without the slash is
still ordinary conversation.

On send the chat is focused (or created if it does not exist yet), and the
butler's companion pet double appears by the bar before waving off. With no
petdex pet installed the double is skipped; the bar and the send still work. If
the gateway cannot inject the turn (gateway injection is not allowed or
unavailable), the bar falls back to posting `/butler <request>` and says plainly
that the chat could not host the full conversation.

The `/butler` pane in the sidebar shows an honest readout — **chip: on · pet
(in-app): found** or **not found** — and flashes **→ bar opened** after a
claimed click. The stored config key `summon.pet_click_claim` in
`plugin-data/butler/config.json` remains a manual escape hatch: set it to
`false` to disarm the pet click and the hat badge. The engine, dashboard, and
desktop watcher read that file fresh, so the change hot-reloads without a
gateway restart.

If a future Hermes build names or shapes the pop-out panel differently, the hat
badge stays hidden and the shortcut remains the fallback.

Agent-side tools and commands:

- `/butler <request>` — inject the request into the current chat as a real agent
  turn and return a one-line acknowledgement; the agent's guide plan (or its one
  clarifying question) then appears as a normal chat reply
- `butler_guide_start` — start a guide from a JSON plan of steps
- `butler_guide_status` — poll the live state of a running guide
- `butler_capture` — one foreground Accessibility read (element text and boxes)
- `butler_stop` — cancel the running guide and tear down the overlay

CLI equivalents from a terminal:

```
hermes butler capture
hermes butler status
hermes butler doctor [--ask]
```

## LOOK

LOOK answers a question about one control on the screen.

Right-click the hat badge beside the popped-out pet (or drag it). The screen
dims, the cursor becomes a crosshair, and a badge follows the pointer with the
live target under it. Click or release to ask about that control; `Esc` and
right-click cancel. While the picker is on, clicks do not reach your apps: the
overlay takes the click, which is what makes a picker safe to aim with.

LOOK needs the pet **popped out** into its own window: the hat badge lives
beside the popped-out pet. With the pet inside the Hermes window, clicking the
pet opens the ask field instead.

The ask field opens carrying what the companion read from the Accessibility
layer: **App**, **Window**, and the control — role, label, enabled state, and
box. The question becomes a real agent turn; the pet speaks the answer in one
line (`butler_say`) and the chat keeps the full explanation.

## WATCH

WATCH tells you when something appears or finishes in an app you already have
open. It is passive: the companion reads the Accessibility tree only — no
screenshots, no OCR, no new macOS permission.

```
butler_watch_start({"app": "Finder", "until": "invoice"})
butler_watch_status
butler_watch_stop
```

`until` is the text to announce: the watch says so once when a label, title, or
value in that app contains it. One watch runs at a time; starting a second
replaces the first. On a hit the pet says `<app>: <label> appeared`, and while
the watch is live the pet carries the change count. The `/butler` pane shows
the watch row — `⏲ Watching <app> · <N> changes · <mm:ss>` — with a **Stop**
button.

Three prompt frames reach the agent: `[butler summon]` for a guidance request,
`[butler look]` for a LOOK question, and `[butler watch]` for monitoring. A
typed `watch <app> for …` routes to the watch frame, the same way the menu
prefill does.

## What is verified, and how

Each step's arrival is read back from the OS before the guide advances: an
Accessibility read of the foreground app, then — when the AX tree looks thin —
one re-walk to depth 24 for Electron/Chromium content, then on-device OCR when
the Screen Recording grant is present. The poll runs every 1.5 s per step and a
step times out at 120 s. A guide that cannot verify a step stalls and says so;
it does not advance on assumption.

## Performance

- The overlay companion idles at 0.0% CPU while the pet is still (measured with
  `ps` on a live companion).
- Hat-badge placement is event-driven, not polled: AX `AXWindowMoved` events are
  coalesced to one placement per 40 ms. A probe logged 86 events in one drag,
  about 17 ms apart.
- With the AX observer live, a 3 s reconciliation tick covers what
  notifications miss (attach-set healing, windows created while the observer
  was not running). Without the observer the same code falls back to a 0.5 s
  poll so the badge is never left unpositioned.
- There is no frame-rate loop in the companion. The only continuous drawing it
  does is the pet double while a guide is running.
- The desktop-side click target follows the pet canvas on the renderer's
  animation frame, but only writes style when the canvas rect actually changes.

## Skills

The plugin bundles two skills. Skills load on demand: they are not eaten
by the agent's context at session start — a fresh agent session sees only
their names in the skills index, and `skill_view(name=...)` pulls the full
text only when the work calls for it.

- `butler:follow` (source: `skills/butler-follow/SKILL.md`) — teaches the
  agent when to offer a guide, how to pace the steps, and when not to run
  one at all. Guides are talk-through, never type-through: butler shows and
  verifies, the user's hands stay on the mouse and keyboard.
- `butler:conventions` (source: `skills/butler-conventions/SKILL.md`) —
  implementation conventions, for anyone doing butler board or code work
  (agents picking up kanban tasks from the `butler` board, plus human
  contributors). The kanban worker prompt points new worker sessions at it.
  It covers the plugin's four hard conventions:
  1. Flat step keys — one flat dict per guide step (`say` + `find.*` /
     `expect.*`); never nested or hierarchical step identifiers.
  2. Restart batching — land a whole changeset and request exactly one
     gateway restart, never dribble one fix per restart.
  3. Checklist page shape — the live checklist / closed-details pane
     layout is frozen by design decision; do not regress it.
  4. TCC single-subject design — all macOS screen access lives in the
     plugin's own bundled Swift companion so TCC grants stay stable across
     updates.

## Disclosure

Plain statements about what this plugin does when it runs.

1. The bundled Swift overlay source ships inside the plugin at
   `butler_core/services/ButlerOverlay` and is compiled on first use with
   `swiftc`. It is local source, not a fetched binary.
2. Overlay rendering runs in a spawned local companion process, driven by a
   JSON-over-stdin protocol. The same shared process serves guides, captures,
   the summon double, and the pop-out hat badge; it exits when idle (a roughly
   10 min watchdog) or when the host closes its pipe.
3. The plugin reads the foreground app via the Accessibility framework and,
   where needed, Apple Vision OCR. It reads element text and geometry boxes
   only. No screenshots are stored or transmitted. Element text goes to the
   chat's own model provider, same as any other tool output in the session.
4. Guide plans, live state, the summon config, and transcripts are small JSON
   files under `plugin-data/butler` (`guides/*.json`, `state.json`,
   `config.json`); nothing else is written outside the plugin's own data
   directory. There is no second runtime config path in the plugin install dir.
5. No network destination other than the model provider. No telemetry, no
   auto-updater, no phoning home.
6. Guides fail closed under unattended runs. From cron or a headless context,
   a guide refuses rather than prompt, so a follow-mode companion can never
   become a background screenreader.
7. Butler never writes to other tools' secrets, workspace files, or system
   state, and never types or clicks for the user. It is guide-only by design.
8. The pet-click receiver is on for every installed plugin: installing is the
   opt-in. It is a narrow reach-in into the Hermes desktop window, not into any
   app or the screen. It reads the position and size of the petdex pet canvas
   (`canvas[aria-label$=" pet"]`, plus the core's bare `canvas[aria-label="pet"]`
   spelling), places a transparent fixed overlay over it that follows the
   canvas on the renderer's animation frame (writing style only when the rect
   changes), and installs document-level capture-phase `pointerdown` listeners
   to notice a click on that canvas. The pop-out hat badge rides the same
   single companion subject: it is a separate mini window, anchored to the
   sprite rather than the mostly-empty 240x300 pet panel, and placed at the
   sprite's upper right when there is room. It never paints into the canvas,
   never edits Hermes core files, and never types or clicks. The stored
   `summon.pet_click_claim: false` config key remains a manual escape hatch; the
   next config sync removes the overlay, listeners, and badge. If a Hermes
   desktop update changes the canvas markup or the pop-out window shape, the
   matching surface quietly goes dark (no crash, no console spam) until the
   selector is re-glued. This receiver needs no Accessibility, Screen
   Recording, or other TCC grant beyond the companion that already exists for
   capture.

## Platform

macOS 13 or later in v0.2. Windows and Linux are not yet supported: capture and
overlay are swappable modules, so the guide logic carries over when those
platforms land. Requires Hermes >= 0.22.5.

## Uninstall

`hermes plugins disable butler` (or `hermes plugins remove butler`) stops the
ring and the hat badge; the companion exits on its idle watchdog. Runtime writes
stay inside `plugin-data/butler/` and `desktop-plugins/butler/`, so those two
directories are the whole runtime footprint.

## Limitations

- macOS only.
- Guides are capped at six steps per plan; a longer task becomes a first guide
  plus a follow-up.
- A plan made blind — a native app, a login-gated page — can still be wrong.
  The guide then stalls and says so rather than guessing.
- Multi-display placement is implemented but has not been exercised on a
  multi-monitor setup.
- OCR requires the Screen Recording grant; without it, gated content that AX
  cannot see stays unverifiable.

Screenshots land in `docs/img` before publishing.

## Tests

- Python: `python -m pytest -q` — 214 tests.
- Desktop half: `npm test` — 63 tests (Node's built-in test runner,
  `tests/desktop_plugin.test.mjs`).
