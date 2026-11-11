# Butler

[![Repo](https://img.shields.io/badge/repo-Intenzi%2Fhermes--butler-blue)](https://github.com/Intenzi/hermes-butler)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%2013%2B-lightgrey)](https://github.com/Intenzi/hermes-butler#requirements)
[![Release](https://img.shields.io/badge/release-v0.4.1-blue)](https://github.com/Intenzi/hermes-butler/releases)

Butler turns the pet already sitting in Hermes into an operator. Ask for help in
plain language and it plans a short guide, rings the real control on your screen,
says one line about it, and waits — moving on only when the macOS Accessibility
layer confirms the step actually landed. A click that did not land stalls the
guide instead of being counted. It works with any petdex pet, installs nothing
into your apps, and never types or clicks for you.

## Install

```bash
hermes plugins install butler        # catalog
# dev install instead: hermes plugins install file:///path/to/butler-plugin
hermes gateway restart               # the plugin loads with the gateway
```

Quit and reopen the Hermes desktop app afterwards: the desktop half is copied
into `desktop-plugins/butler/` at startup, and a running app keeps the copy it
started with.

First use downloads the ~1.2 MB universal arm64+x86_64 screen helper from this
repository's attested GitHub release — published by CI with
`actions/attest-build-provenance` — and verifies it against the release's
SHA-256. No compiler needed. If the download fails and Command Line Tools are
installed, Butler compiles the bundled Swift source instead (7–14 s). See
[docs/](docs) for the build and provenance detail.

## What you get

| Verb | What it looks like | What it proves |
|---|---|---|
| **WALK** · `butler_guide_start` | A ring on the real control, a one-line bubble, live progress. | Each step is read back from the Accessibility tree — a shallow walk, then one re-walk to depth 24 for Electron apps — before the guide advances. One guide at a time; a second is refused unless you pass `replace=true`. |
| **DESCRIBE** · `butler_look` | Right-click the hat beside the pet for a menu, or drag it: the screen dims and a badge names the live target. | The ask opens carrying App / Window / Control (role, label, enabled, box) read from the AX layer. |
| **WATCH** · `butler_watch_start` | ⏲ watching line on the pet, and a hit line that stays until you acknowledge it. | One app's AX tree is read in the background; `until` matches emitted change events, `scan: {author, roles}` waits for a genuinely new message from a named author. |

Every door ends in a real agent turn: `[butler summon]`, `[butler look]` and
`[butler watch]` frames are how a request reaches the model. A typed
`watch <app> for …` routes to the watch frame.

## LOOK

Right-click the 26x24 hat beside the popped-out pet for the menu, or drag the
hat to look. A screen-sized overlay takes the click, so clicks do not reach your apps;
the cursor becomes a crosshair, and a badge shows the live target under the
pointer. Release to ask; `Esc` or right-click cancels. The ask field opens with
the App, the Window, and the control already filled in, and the answer is spoken
in one line on the pet while the chat keeps the full explanation.

LOOK needs the pet **popped out** into its own window, because the hat lives
beside it. With the pet inside the Hermes window, clicking the pet opens the ask
field instead.

## WATCH

```
butler_watch_start({"app": "Finder", "until": "invoice"})
butler_watch_status
butler_watch_stop
```

`until` is plain text: the watch speaks once when a label, title, or value in
that app contains it — window titles are the reliable case. `scan: {author,
roles}` is the content mode: it waits for a NEW message from a named author,
matched only against author-role nodes (AXImage / AXButton / AXLink) and only
for signatures that were absent from the arm-time baseline. One watch runs at a
time; a hit is announced once and the line stays on the pet until you clear it
from the hat menu (**Acknowledge watch hit**). The `/butler` pane shows
`⏲ Watching <app> · <N> changes · <mm:ss>` with a **Stop** button.

## Requirements

- macOS 13 or later; Apple silicon or Intel.
- Hermes Agent 0.22.5+ with the desktop app (that is where the pet and the hat live).
- One macOS permission: Accessibility, granted to Butler's bundled helper, plus
  one app restart. `hermes butler doctor --ask` raises the real dialog.

## Safety & privacy

- **Accessibility only.** No screenshots, no OCR, no Screen Recording, no Input
  Monitoring. Butler reads element text and geometry boxes, nothing else.
- **Nothing leaves the machine** except what the chat's own model provider
  already receives. No telemetry, no auto-updater.
- **One download, ever.** The attested helper from this repository's release,
  SHA-256 verified. Refuse it with `BUTLER_NO_DOWNLOAD=1` and it will compile the
  bundled source locally when it can.
- **Guide-only by design.** Butler never types, clicks, or edits your files; it
  shows and verifies. A guide refuses to start under an unattended run, so a
  cron session cannot become a background screenreader.
- **Bounded state.** One guide and one watch at a time; runtime writes stay in
  `plugin-data/butler/` and `desktop-plugins/butler/` (the watch log is capped at
  500 lines).

## Troubleshooting

- **The helper didn't download.** Run `hermes butler doctor`; its JSON says
  whether the overlay is present. Offline or behind a proxy, install Command Line
  Tools (`xcode-select --install`) so Butler compiles the bundled Swift source
  instead, or set `BUTLER_NO_DOWNLOAD=1` to skip the network attempt outright.
- **The guide says "Switch to <app>".** That step happens in another app and
  verification failed while yours was frontmost. Bring that app forward and the
  guide resumes; if the control moved, it stalls rather than counting the step.
- **The watch says baseline 0.** Nothing matched the author-role nodes when the
  watch armed — the view had not rendered yet, or the name does not match what AX
  exposes. Open the list, then arm the watch again.

## Docs

- [docs/demo-script.md](docs/demo-script.md) — the 90-second walkthrough.
- [docs/evidence-where-is-it.md](docs/evidence-where-is-it.md) — the field
  evidence behind the "where do I find X" use case.
- [skills/](skills) — `butler:follow` (when to offer a guide), `butler:usage`
  (when to suggest one at all), `butler:conventions` (contributor rules).
- Tests: `pytest` and `npm test`.

## License

MIT — see [LICENSE](LICENSE).
