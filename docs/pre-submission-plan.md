# Butler: pre-submission polish, test matrix, demo set

Written against the tree at `/Users/tenzi/.hermes/plugins/butler` (v0.2.0) on 2026-11-11.
Verified in this session: `pytest -q` → 201 passed; `npm test` → 59 passed; `hermes butler doctor` on the
author's machine returns `accessibility_granted: true`, `screen_recording_granted: false`,
`overlay: "present"`.

---

## A. Polish backlog

### P0 — must fix before submitting

| # | Symptom | Fix (one line) | Effort |
|---|---|---|---|
| A1 | README documents WALK only. There is no LOOK section and no WATCH section, so two of the three verbs a judge will be told about have no instructions. | Add `## Look` and `## Watch` sections with the exact gestures and the tool names. | S |
| A2 | README's `## Screenshots` embeds `docs/img/hat.png`, `ring.png`, `panel.png`. `docs/img/` does not exist, so the submission README shows three broken images. | Either capture the three PNGs into `docs/img/` or delete the section. | S |
| A3 | `GUIDE_START_SCHEMA` requires `find.label` on every step and documents only `expect.app/visible/focus`. The bundled `butler:follow` skill tells the agent a launch step is `"find": {}` and pane verification is `expect.window`. The schema and the skill disagree, so the documented first step of every guide is off-schema. | Make `find` optional, drop the `label` requirement, add `window` and `app_frontmost` to `expect`. | S |
| A4 | `plugin.yaml` declares `guide_poll_seconds`, `guide_step_timeout_seconds`, `overlay_enabled`, `ocr_fallback_enabled`. No code reads any of them; the engine reads `poll_seconds` / `step_timeout_seconds` / `stall_auto_stop_seconds` from `plugin-data/butler/config.json`. Setting `overlay_enabled: false` still draws rings. | Rename the schema keys to the ones the engine reads, and delete `overlay_enabled` / `ocr_fallback_enabled` unless implemented. | S |
| A5 | Doctor reports "Accessibility not granted" whenever the companion cannot answer, including when `swiftc` is missing or compilation failed (`trusted()` returns False on any transport failure). A judge Mac with no Xcode Command Line Tools is told to grant Accessibility, which can never fix it. | Report `out["overlay"]` first; when it is not `present`, emit "install Xcode Command Line Tools (`xcode-select --install`)" and suppress the grant fix. | S |
| A6 | WATCH leaves no persistent mark. The pet line is re-said only when the event count changes, each say expires at 9 s, the pane has no watch row, `butler_watch_stop` is silent and an app quit just drops the line. After 9 quiet seconds a judge cannot tell a watch is running. | Add a pane row (`Watch · app · N changes · elapsed` + Stop button) and speak one "watch ended: reason" line on stop or app quit. | S for the row, M for a hat dot |
| A7 | LOOK is reachable only from the hat badge, and the hat exists only when the pet is popped out. The in-app pet handles left-click only (opens the ask bar). A judge with a docked pet has no LOOK gesture at all. | Document "pop the pet out first" in README and in the demo script; or add right-click to the in-app pet canvas. | S docs, M code |

### P1 — fix if time

| # | Symptom | Fix | Effort |
|---|---|---|---|
| A8 | README claims 104 Python tests and 38 desktop tests. Actual: 201 and 59. | Update the two numbers. | S |
| A9 | The author's `plugin-data/butler/config.json` sets `step_timeout_seconds: 600`, so stalls appear after 10 min locally and after 120 s for a judge. Every stall demo recorded on this machine misrepresents the shipped default. | Delete the local override before recording; demo with shipped defaults (poll 1.5 s, timeout 120 s, auto-stop 180 s). | S |
| A10 | Quick start never mentions Xcode Command Line Tools, the only hard prerequisite (`swiftc` compiles the companion on first use), and never says what the user sees after denying Accessibility. | Add a Prerequisites block and a one-paragraph "if you denied the grant" recovery path. | S |
| A11 | `capture_failed` surfaces the raw companion string ("companion did not return a capture within 3s") with no instruction. | Map it to one user-facing sentence plus the doctor command. | S |
| A12 | Uninstall section says disable/remove stops the ring and hat, but leaves `desktop-plugins/butler/` and `plugin-data/butler/` and does not tell the user to restart the desktop app for the badge to disappear. | Rewrite the uninstall block as four ordered steps including the app restart and the two directories. | S |
| A13 | Watch resolves the app by `localizedName` only. On a German or French Mac, `butler_watch_start(app="System Settings")` fails; the error does not say the name is localized. | Accept a bundle id or match case-insensitively against localized names, and say "no running app named X; it may be localized" on failure. | M |
| A14 | `butler_capture` schema advertises `depth` default 10; the engine's real walk is 12 shallow then 24 deep. An agent reading the schema plans for depth 10. | Correct the description. | S |
| A15 | No in-product way to run doctor, and no first-run check that the companion built. | Have the `/butler` pane show the one-line doctor verdict at the top. | M |

### P2 — post-hackathon

- Exercise multi-display and Spaces placement for real; the ring conversion code exists but has never run on two monitors.
- One companion server instead of one per plugin-hosting Python process.
- Watch conditions beyond substring (role, value-only, negation).
- Per-guide history view; the `guides/*.json` files already carry everything needed.
- Localized step labels: plan against the AX label the capture returned, never an English guess.

---

## B. UX test matrix

All state paths below are relative to `~/.hermes/plugin-data/butler/`.
Reset between scenarios: `hermes butler status`, then `butler_stop` / `butler_watch_stop`, and delete `state.json` if it looks stale.

| # | Scenario | Setup | Action | Expect | If it fails, check |
|---|---|---|---|---|---|
| 1 | Fresh install, no grant | Remove the companion from System Settings → Accessibility; restart gateway | `hermes butler doctor` | `accessibility_granted: false`, `capture: "missing grant"`, fix names the grant and the subject | `state.json` absent is fine; the doctor JSON is the signal |
| 2 | Grant prompt path | Same as 1 | `hermes butler doctor --ask` | macOS Accessibility dialog appears; System Settings opens on the Accessibility pane; user toggles the companion on | Toggle list entry name vs `accessibility_grant_subject` string |
| 3 | No Xcode CLT | On a second Mac without CLT (or `PATH` without `swiftc`) | `hermes butler doctor` | Says the companion is missing and to install Command Line Tools; does NOT tell the user to grant Accessibility (fails today, item A5) | `overlay` value: `swiftc_not_found` |
| 4 | Pet docked, no pop-out | Pet inside the Hermes window | Right-click the pet | Nothing (documented gap). Left-click opens the ask bar | `chip-events.log` should stay empty; no hat in any window |
| 5 | Pet popped out | Pop the pet out, `summon.pet_click_claim: true` | Look at the sprite's upper right | 26x24 hat badge, ~10 pt clear of the sprite, not the window corner | `chip-events.log`; CGWindow list for an 84x24 window owned by the companion |
| 6 | WALK happy path | System Settings closed | Guide: open Settings, click Network | Double walks; ring on `Network` sidebar row; step 1 verifies on app open, step 2 on `window: "Network"`; checklist ticks; done line | `state.json` → `guide.state.idx`, `verifications[].reason` (`app_open_targeted`, `pane_reached`) |
| 7 | WALK wrong click | Guide from 6, running | Click `Bluetooth` instead of `Network` | Guide stays on step 2 and re-rings; it does not advance | `state.json` `idx` unchanged; `last_capture_app` |
| 8 | WALK stall | Guide on a control that is not on screen | Wait past 120 s (`step_timeout`), then 180 s (`stall_auto_stop`) | Stalls with a note, then goes quiet and stops itself | `state.json` `state.status` transitions `running` → `stalled` → `stopped` |
| 9 | Guide while guide | Run guide 6; start a second guide without `replace` | `butler_guide_start` | Refused with the hint to pass `replace`; the first guide keeps running | `state.json` `guide.id` still the first |
| 10 | Guide replace | Same, with `replace: true` | `butler_guide_start(replace=true)` | First guide stops, second starts, one double, one ring | `guides/*.json` for both ids |
| 11 | LOOK by click | Pet popped out, Finder frontmost with a file selected | Right-click hat, move over the toolbar, click | Crosshair cursor, badge shows `role · label`, then the ask field opens carrying App/Window/Control; answer arrives in the pet bubble, full text in chat | `say.json` (`id`, `text`), `state.json` `say`; app underneath did NOT receive the click |
| 12 | LOOK by drag | Same | Drag the hat to a control and release | Same as 11 on release | Same |
| 13 | LOOK cancel | Same | Arm again, press Esc; then arm and right-click | Cursor returns to arrow, no ask field, no `say.json` write | `say.json` mtime unchanged |
| 14 | LOOK on Electron | VS Code frontmost | Right-click hat, click a sidebar row | Badge resolves the row's label, never `Group` with an empty label | Depth-24 capture of the same window |
| 15 | LOOK over a modal | Guide 6 running, ring visible, then LOOK | Arm LOOK while a ring is on screen | Ask field appears above the ring (`.screenSaver + 1`); Return submits to the field, not the app below | Focus log line `ask-focus active=…` |
| 16 | WATCH hit | Finder on `~/Downloads` | `butler_watch_start(app="Finder", until="invoice")`, then `touch ~/Downloads/invoice.pdf` | Pet says `Finder: invoice.pdf appeared` once; status shows `hit` and the event tail | `watch.jsonl` last lines; `state.json` `watch.hit` |
| 17 | WATCH while WATCH | Scenario 16 active | Start a second watch on TextEdit | Second watch replaces the first, or is refused with a clear message; only one `watch` row in state | `state.json` `watch.app`, `owner_pid` |
| 18 | Watch, app quits | Scenario 16 active | Quit Finder (⌘Q on Finder) | Today: line vanishes silently. Required after A6: one "watch ended: app quit" line and the row marked stopped | `state.json` `watch.stopped.reason`; `watch.jsonl` `watch-stopped` |
| 19 | Watch, no events | Watch an idle app, 2 minutes | Do nothing | Pet should still show the watch is alive (fails today; fixed by A6) | `state.json` `watch.events` at 0 |
| 20 | Watch, app not running | TextEdit closed | `butler_watch_start(app="TextEdit")` | Clear error naming the app; no `watch` row left behind | `state.json` should have no `watch` key |
| 21 | Multi-display | Second display right of main, pet on the second | Guide 6 | Ring lands on the control on that display (coordinates untested) | `state.json` `current_box` vs actual screen position |
| 22 | Spaces | Guide 6, then switch Space | Switch away and back mid-step | Ring follows or hides; it must not sit over the wrong Space's content | Same as 21 |
| 23 | Menu open | Guide with a step that needs a menu item | Open the app's menu bar menu | AX read sees menu items; ring lands on the item | `butler_capture` while the menu is open |
| 24 | Long label | Target a control whose label is > 60 chars | Run the guide | Ring bounds the whole control; bubble truncates rather than overflowing | `state.json` `current_label` |
| 25 | Non-English UI | Set the mac to German, reopen System Settings | Guide planned with English labels | Guide stalls honestly (labels are localized); `butler_capture` shows the localized names | Capture output vs plan `find.label` |
| 26 | Hermes not running | Quit the desktop app, gateway still up | `butler_guide_start` | Guide runs headless (no pet, no ring), state still written; `butler_say` writes `say.json` that nobody speaks | `state.json`, `say.json` `at` timestamp |
| 27 | Uninstall | Plugin installed and enabled | `hermes plugins disable butler`, restart the app | Ring stops, badge disappears, companion exits after its idle watchdog (~10 min); no error in the app | `ps aux | grep ButlerOverlay` |

---

## C. Demo set (six, two per verb)

Every scenario below is reproducible on a Mac that has never seen the repo, needs no network and no
screen-recording grant, and finishes under 90 seconds. Keep every pet line under 90 characters.

### WALK 1 — "It refuses to lie"

- **Setup.** System Settings closed. Guide: step 1 `say: Open System Settings`, `find: {}`, `expect: {app: System Settings}`; step 2 `say: Click Network`, `find.label: Network`, `expect: {window: Network}`.
- **Click-by-click.** Start the guide. When step 2 rings, click **Bluetooth** on purpose. Wait one poll. Then click **Network**.
- **Expected.** The ring stays on `Network` through the wrong click, the checklist does not tick, and only the second click advances it. The pet finishes with `Guide complete · 2 of 2 verified`.
- **Why a viewer cares.** Autoplay demos cannot show a check that failed. This one proves each step is read back from the OS.

### WALK 2 — "It sees inside Electron"

- **Setup.** VS Code open with a folder, guide: step 1 `Click Settings` → `find.label: Settings`, `expect: {window: Settings}`.
- **Click-by-click.** Run the guide. Let it ring the gear, click it.
- **Expected.** Ring lands on the real control inside the Electron web view, not on a container. Step verifies.
- **Why a viewer cares.** Most demos of this kind only work in Apple's own apps. Depth-24 capture is the invisible work.

### LOOK 1 — "Answer where I'm pointing, without leaving the app"

- **Setup.** Pop the pet out. Open a PDF in Preview. Do not open Hermes chat.
- **Click-by-click.** Right-click the hat. Move to the toolbar's **Share** button. Click.
- **Expected.** Crosshair, then a badge reading `Button · Share`. Ask field opens pre-filled with App=Preview, Control=Button/Share. Type "what does this do?". Pet says one line; the full answer is in chat.
- **Why a viewer cares.** The answer lands beside the pet in the app they are already in, and no screenshot was taken.

### LOOK 2 — "Element state, not pixels"

- **Setup.** Same, on a PDF with nothing selected so **Export** is disabled.
- **Click-by-click.** Right-click hat, point at **Export**, click. Ask "why can't I click this?".
- **Expected.** Badge shows the control with `enabled=false`. Answer names the disabled state as the reason.
- **Why a viewer cares.** This is the one thing a screenshot-based tool cannot answer and the Hermes HUD does not read.

### WATCH 1 — "It keeps watch while I work elsewhere"

- **Setup.** Finder window on `~/Downloads`, no file named `invoice.pdf`. Terminal ready.
- **Click-by-click.** `butler_watch_start(app="Finder", until="invoice")`. Switch to Terminal and run `touch ~/Downloads/invoice.pdf`.
- **Expected.** Pet says `Finder: invoice.pdf appeared` once, while Finder is not frontmost. `butler_watch_status` shows the hit and the tail of events.
- **Why a viewer cares.** No click, no screenshot, no polling loop the user has to watch. It speaks from the background.

### WATCH 2 — "Passive, and honest about what it saw"

- **Setup.** Terminal open, idle.
- **Click-by-click.** `butler_watch_start(app="Terminal", until="BUILD OK")`. In another terminal tab run `sleep 8; echo BUILD OK`. While waiting, click around in Finder. Then call `butler_watch_status`.
- **Expected.** The status lists focus/window changes with roles and window titles as they happen, then the hit line. The event tail is readable evidence, not a spinner.
- **Why a viewer cares.** It shows the observation log itself, so the audience can see the mechanism rather than trust a claim.

---

## D. Portability risks

| Risk | What happens on someone else's Mac | Mitigation |
|---|---|---|
| Pet not popped out | No hat, so LOOK and the ask-chip are unreachable; only ⌘⇧A works. On a fresh install this is the default state. | P0 A7. Demo scripts must start with "pop the pet out". |
| Different pet or no pet | `resolve_frames_dir()` falls back to the first directory with a spritesheet, and returns None with no pet, so the double is skipped while guides still run. A different sheet changes frame count and size, so the double can animate at the wrong rate. | Verify one non-default pet sheet before submitting; state that guides work petless. |
| Two displays | Ring coordinates are converted against a primary screen and have never run on a second monitor. The hat anchors to a sprite frame read from the pop-out panel, which may be on the other display. | Run scenario 21; if the ring is offset, document "single display for now" rather than shipping a wrong ring. |
| Docked vs floating pet | Claim chip anchors to the sprite, not the 240x300 window. If a future app build changes the panel shape or the canvas selector, the chip silently disappears. | Scenario 5 checks the 84x24 window; README already promises quiet degradation, keep that true. |
| Permission prompt wording | macOS lists the binary's name, not the prose subject string in `GRANT_SUBJECT`. The doctor tells the user to find "Butler companion - the butler plugin's bundled helper"; System Settings may show `ButlerOverlay` or a path. | Open the pane on a clean Mac, read the actual row name, and rewrite `GRANT_SUBJECT` to match what is on screen. |
| Non-English UI | App names resolve via `localizedName` and control labels are localized, so an English plan cannot verify on a German Mac. | Scenario 25; say plainly that guides are planned from a live capture, so the plan language must match the UI language. |
| Electron trees | Anything shallower than depth 24 sees containers only; the guide then stalls at step 1 with `overlay: "waiting"`. | Scenario 14; keep the deep re-walk, and name it in the pitch as the reason it works outside Apple apps. |
| Apps that expose nothing | Canvas, Java, some Electron apps with accessibility off, and games return no useful AX nodes. Guides stall and OCR is unavailable without Screen Recording. | Say it in the demo: "if the app publishes nothing to Accessibility, Butler says so instead of guessing." |
| Screen Recording absent | True on the author's machine (`screen_recording_granted: false`), so the OCR fallback has never completed end to end. Do not claim it works. | Keep the claim to AX-only; mention OCR as "requires an extra grant, untested in this build". |
| Hermes version drift | Pet click depends on `canvas[aria-label$=" pet"]` and the pop-out being an NSPanel on layer 3 titled "Hermes". A desktop update can break both silently. | Pin `requires_hermes: ">=0.22.5"`, and test on the exact build the judge will install. |
| Stale local config | `plugin-data/butler/config.json` on the author's machine sets `step_timeout_seconds: 600`, so local demos behave differently from a fresh install (120 s). | P1 A9: record with defaults. |
| Leftover state | A `state.json` with a `done` guide from a previous run makes the pane look pre-populated on first open. | Uninstall/reset before recording; delete `state.json` and `guides/*.json`. |
