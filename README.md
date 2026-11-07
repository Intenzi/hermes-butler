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

Agent-side tools:

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
  It covers the board's six hard conventions:
  1. Flat step keys — one flat dict per guide step (`say` + `find.*` /
     `expect.*`); never nested or hierarchical step identifiers.
  2. Restart batching — land a whole changeset and request exactly one
     gateway restart, never dribble one fix per restart.
  3. verify-dsh-rule — a dsh (DeepSeek Harness) completion summary is a
     claim, not a result; verify tests and diffs directly before building
     on or reporting it.
  4. Checklist page shape — the live checklist / closed-details pane
     layout is frozen by design decision; do not regress it.
  5. TCC single-subject design — all macOS screen access lives in the
     plugin's own bundled Swift companion so TCC grants stay stable across
     updates.
  6. Card etiquette — how to write, promote, comment, and complete cards
     on the butler kanban board without stranding parallel runs.

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

## Platform

macOS 13 or later in v0.1. Windows and Linux are not yet supported: capture and
overlay are swappable modules, so the guide logic carries over when those
platforms land. Requires Hermes >= 0.22.5.

## Test it locally

1. `hermes butler doctor --ask` — grants the two TCC panes; the final checkbox
   and app restart are manual.
2. `hermes butler capture` — should print element text and boxes for the
   current foreground app.
3. Open a new chat and ask your pet to spawn a test guide; rings on-screen and
   bubbles-and-progress count as confirmation.
4. `/butler` opens the pane: check its status summary alongside the guide you
   just ran.
