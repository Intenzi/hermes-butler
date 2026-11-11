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
into your apps, and never types or clicks for you. The best part? Butler does this without vision.

<img width="242" height="208" alt="image" src="https://github.com/user-attachments/assets/192283dd-f818-43ed-8589-f1dc39514370" />


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

Three things, all from the same pet.

## Guide
<img width="2054" height="1486" alt="image" src="https://github.com/user-attachments/assets/15e5ffdc-d6d1-46ee-9028-5cadea82ad55" />

**Ask for help with a task.** The pet rings the next thing to click, says one
line about it, and stays there until the screen actually changes. Click the wrong
thing and it waits — it does not pretend you are done and it does not move on to
step two.

## Look
<img width="2452" height="1602" alt="image" src="https://github.com/user-attachments/assets/364931e2-ebab-4ecb-a98c-9b9d868f7826" />


**Point at something and ask about it.** Right-click the hat beside the pet, then
click whatever you are curious about. Butler tells you what that control is, what
it does, and why it might be greyed out — and the click never reaches the app
underneath, so nothing fires by accident.

## Watch
<img width="384" height="242" alt="image" src="https://github.com/user-attachments/assets/48c1908c-bcc3-4885-a4d7-bac8ea5abd5f" />


**Tell it to watch for one thing.** "Tell me when the build finishes." You go and
do something else; when it happens the pet says so, and keeps saying so until you
acknowledge it. Not a notification that scrolls away — a line that waits for you.

All three work the same way underneath: Butler reads the on-screen element data
that macOS already publishes, the same information a screen reader uses. No
pictures, no OCR, no model looking at your screen.


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
  exposes. Open the list, then rerun the watch again.

## Docs

- [docs/demo-script.md](docs/demo-script.md) — the 90-second walkthrough.
- [docs/evidence-where-is-it.md](docs/evidence-where-is-it.md) — the field
  evidence behind the "where do I find X" use case.
- [skills/](skills) — `butler:follow` (when to offer a guide), `butler:usage`
  (when to suggest one at all), `butler:conventions` (contributor rules).
- Tests: `pytest` and `npm test`.
- Screenshots live in `docs/img`.

## License

MIT — see [LICENSE](LICENSE).
