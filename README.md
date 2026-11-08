# butler

A Follow-mode guide plugin for Hermes Agent on macOS. In Follow mode, the agent
plans and edits with the user's eyes on it; butler extends that partnership to
the whole Mac screen. The agent plans a guide, and the butler walks the user
through it step by step: a ring around the real UI control on screen, a
one-line bubble explaining the current move, live progress as steps complete.
Each step is not assumed but verified — butler re-reads the macOS Accessibility
layer to confirm the click actually landed. On-device Apple Vision OCR is the
fallback when Accessibility comes back empty, e.g. gated content in Chromium
tabs where the AX tree is unavailable.

Butler is species-free: it uses whatever pet plugin the user has installed from
petdex. The rings, chips, and walk-trail are drawn by butler's own
click-through Swift companion process, so the overlay works with any pet.

## Install and first run

Install through the normal `hermes plugins` flow. Then run:

    hermes butler doctor --ask

That raises the real macOS permission dialogs — accessibility is prompted by
the plugin itself via `AXIsProcessTrustedWithOptions`, screen recording via
`CGRequestScreenCaptureAccess` — and deep-links the two System Settings panes
so the grant is one toggle away. The Accessibility grant is **Butler companion
- the butler plugin's bundled helper**. Because the butler reads the screen
through its own small helper, macOS asks once and remembers across updates.
The final checkbox and one app restart stay manual, because macOS TCC requires
it; the doctor tells you when it is your turn and tells you when you are clear.

## Usage

### Ask the butler

Summon the dedicated **Butler** chat from anywhere in the desktop and hand it a
text request. The bar posts the request to the plugin's gateway route, which
injects a real agent turn into the chosen chat. The agent plans a short guide and
starts it immediately; if the request is too vague, it asks exactly one
clarifying question as a normal chat reply, and you answer it right there. Typing
a plain message in any chat without the slash is still ordinary conversation.

- **Keyboard shortcut** — "Ask the butler" (default ⌘⇧A, rebindable in
  Settings → Keybinds). Opens the floating bar; Enter sends, Escape or blur
  dismisses. Focus stays on the text input; the destination buttons are
  Tab-reachable after it.
- **Destination** — a segmented choice in the bar: **Butler chat** (the existing
  chat, default) or **New session** (always creates a fresh Butler-titled chat,
  never reuses the oldest). The choice is kept in the desktop's localStorage
  across openings; the success toast names it.
- **Click the pet** — opt-in only, off by default. Turn it on with the
  "Click the pet to ask the butler" switch on the `/butler` pane. The switch
  writes `summon.pet_click_claim` in the plugin's
  `plugin-data/butler/config.json`; the engine reads it fresh, so it hot-reloads
  without a gateway restart. The receiver only acts on the in-window pet canvas;
  a popped-out pet window cannot receive the click, so use ⌘⇧A there.
- **Existing surfaces** — the status-bar chip, sidebar page, and palette are
  unchanged.

On send the chat is focused (or created if it does not exist yet), and the
butler's companion pet double appears by the bar before waving off. With no
petdex pet installed the double is skipped; the bar and the send still work. If
the gateway cannot inject the turn (gateway injection is not allowed or
unavailable), the bar falls back to posting `/butler <request>` and says plainly
that the chat could not host the full conversation.

Agent-side tools and commands:

- `/butler <request>` — inject the request into the current chat as a real agent
  turn and return a one-line acknowledgement; the agent's guide plan (or its one
  clarifying question) then appears as a normal chat reply

- `butler_guide_start` — start a guide from a JSON plan of steps
- `butler_guide_status` — poll the live state of a running guide
- `butler_capture` — one foreground Accessibility read (element text and boxes)
- `butler_stop` — cancel the running guide and tear down the overlay

CLI equivalents from a terminal:

    hermes butler capture
    hermes butler status
    hermes butler doctor [--ask]

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
2. Overlay rendering runs in a spawned local companion process, one per guide,
   driven by a JSON-over-stdin protocol. It exits with the guide.
3. The plugin reads the foreground app via the Accessibility framework and,
   where needed, Apple Vision OCR. It reads element text and geometry boxes
   only. No screenshots are stored or transmitted. Element text goes to the
   chat's own model provider, same as any other tool output in the session.
4. Guide plans, live state, and transcripts are small JSON files under
   `plugin-data/butler` (`guides/*.json`, `state.json`); nothing else is
   written outside the plugin's own data directory.
5. No network destination other than the model provider. No telemetry, no
   auto-updater, no phoning home.
6. Guides fail closed under unattended runs. From cron or a headless context,
   a guide refuses rather than prompt, so a follow-mode companion can never
   become a background screenreader.
7. Butler never writes to other tools' secrets, workspace files, or system
   state, and never types or clicks for the user. It is guide-only by design.
8. The opt-in pet-click receiver is a narrow reach-in into the Hermes desktop
   window, not into any app or the screen. When enabled, it reads the position
   and size of the petdex pet canvas (`canvas[aria-label$=" pet"]`), places a
   transparent fixed overlay over it that follows the canvas each frame, and
   installs document-level capture-phase `pointerdown` listeners to notice a
   click on that canvas. It never paints into the canvas, never edits Hermes
   core files, and never types or clicks. It is **off by default**; turning the
   `/butler` switch off removes the overlay and listeners immediately. If a
   Hermes desktop update changes the canvas markup or label, the receiver
   quietly stops matching (no crash, no console spam) until the selector is
   re-glued. This receiver needs no Accessibility, Screen Recording, or other
   TCC grant.

## Platform

macOS 13 or later in v0.1. Windows and Linux are not yet supported: capture and
overlay are swappable modules, so the guide logic carries over when those
platforms land. Requires Hermes >= 0.22.5.

## Test it locally

1. `hermes butler doctor --ask` — grants the two TCC panes; the final checkbox
   and app restart are manual.
2. `hermes butler capture` — should print element text and boxes for the
   current foreground app.
3. Type `/butler open System Settings` in any chat; the injector acknowledges in
   chat, then the agent starts the guide (rings, bubbles, progress) rather than
   printing a plan.
4. Open the Butler pane from the sidebar: check its status summary, and turn on
   the pet-click switch if you want clicking the pet to open the bar.

Desktop-half tests run with `npm test` (Node's built-in test runner); the
Python side runs with `python -m pytest`.
