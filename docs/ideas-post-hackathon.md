# After the hackathon: 15 directions for Butler

Status: brainstorm, nothing here is implemented. Written against 0.2.0
(4 tools, one Swift companion, AX capture with an OCR fallback).

Constraint every card below holds to: Accessibility only (Screen Recording stays
optional and unused by most), no per-step model calls, no mascot-specific
behaviour, graceful degrade when the grant is missing.

---

## 1. EYES

### Read the open URL
- What the user sees or does: When the butler rings something on a web page, the guide knows which page it is on instead of guessing.
- Mechanism: one extra attribute in the existing capture walk — `AXDocument` on the focused web area and on document-based windows — returned as `doc` beside `window` in the snapshot `readAXFields` already builds; a new `expect.doc` key for verification. No OCR.
- Cost: compute Low (one attribute per window, not per element); permissions Low (Accessibility, already granted); code Low (one attribute read, one snapshot field, one expect key).
- Why it is not a gimmick: the most common wrong plan is a plan for a page the user has already left, and this answers it from the OS instead of asking the user where they are.

### Ring only what's live
- What the user sees or does: The ring stops landing on a greyed-out control; when a control cannot be used, the butler says it is disabled instead of waiting for a click that will not work.
- Mechanism: read `AXEnabled` (and `AXHelp`) for the matched element in the same walk that produces role/label/box; `_evaluate_step` gains a "found, not actionable" verdict and suppresses `publish()`. Builds on `best_match` and the ring op.
- Cost: compute Low (attribute copy on visited elements only, no extra walk); permissions Low; code Low (one attribute, one verdict reason, one line of overlay copy).
- Why it is not a gimmick: it turns a 120s stall into a diagnosis, and saves the click the user would otherwise spend on a dead button.

### Watch for what appears
- What the user sees or does: The moment a sheet or new window opens, the butler reacts, instead of noticing up to a poll interval later.
- Mechanism: extend the AXObserver that already tracks `AXWindowMoved` and `AXFocusedWindowChanged` with `AXCreated` / `AXUIElementDestroyed` / `AXValueChanged`, coalesced through the same 40ms placement timer; the engine re-reads state on the hint instead of waiting for the 1.5s poll.
- Cost: compute Low-Med (notification traffic on busy apps needs the same coalescing discipline as the drag case; still zero when idle); permissions Low; code Med (notification lifetime across window churn is the fiddly part).
- Why it is not a gimmick: it deletes the "did my click do anything?" pause for both the user and step verification, which otherwise costs a full poll period on every action.

## 2. HANDS

### Press the ringed control
- What the user sees or does: A "do it" affordance on the badge; the user presses it and the highlighted button gets pressed for them.
- Mechanism: `AXUIElementPerformAction(kAXPressAction)` on the element the engine already resolved and is currently ringing — no synthetic mouse, no coordinates, no keystrokes. Only the ringed element, only on that gesture, followed by the same expect check the poll already runs.
- Cost: compute Low (one action plus a normal poll); permissions Low (AXPress rides the Accessibility grant; no Input Monitoring, no CGEvent); code Med (op plumbing across the companion protocol, plus the vanished-element path).
- Why it is not a gimmick: it finishes the guides that are pure navigation, and unlike coordinate clicking it either works or reports that the element refused.

### Fill the ringed field
- What the user sees or does: The user types "put the invoice number in there" in the ask field and the number lands in the highlighted text box, correctly.
- Mechanism: `AXSetValue` on the ringed text field, then read `AXValue` back as verification through a new `expect.value` key that reuses the same snapshot; the text arrives through the ask-field spool that already exists.
- Cost: compute Low; permissions Low; code Med (some fields reject AXSetValue and need commit, and read-back has to be exact or it is a lie).
- Why it is not a gimmick: form filling is most of what people actually want automated, and read-back catches a wrong fill at the field instead of at submit.

### Walk a menu path
- What the user sees or does: The user says "export this as PDF" and the butler presses File, then Export, then PDF like a person would.
- Mechanism: resolve each level through the menu bar's AX children (`AXMenuBar` -> `AXMenuBarItem` -> `AXMenu` -> `AXMenuItem`), pressing between levels and re-reading the tree each time because menus only exist while open; the ring follows the current level so the user sees where it will land.
- Cost: compute Low (a few small reads per level, no screenshots); permissions Low; code Med (menu timing is the whole problem — the tree is only there between press and dismissal).
- Why it is not a gimmick: menu paths are where macOS apps hide most of their features, and without this the agent ends up telling the user to "look for the export option".

## 3. MEMORY

### Remember where things were
- What the user sees or does: The second time they run a guide in the same app, the first step rings immediately instead of after a fresh plan.
- Mechanism: the route memo already specified in `docs/deferred-steps.md` — keyed on bundle id, window signature and step intent, storing the resolved find/expect from the last successful run, invalidated on app version change, resolved locally by the exact -> fuzzy -> OCR -> title ladder.
- Cost: compute Low (one file read at step start; no captures stored, only labels and boxes); permissions Low; code Med (keying and invalidation are the entire job).
- Why it is not a gimmick: it is the only card here that makes the agent's work cheaper rather than the user's, turning a one-call plan into zero calls.

### Recipes with a last-run date
- What the user sees or does: A list of guides that worked, newest first, each with its app and whether it still passes, re-runnable in one click.
- Mechanism: `guides/<id>.json` already holds steps plus the verification trace; promote `done` sessions into an index keyed by (bundle id, app version, title) and surface it through the existing `/state` row and the pane checklist. Re-run is the same engine with a saved step list, so no planning call.
- Cost: compute Low; permissions Low; code Low-Med (index plus pane list; the files are already correct).
- Why it is not a gimmick: week two costs nothing in model calls and week eight still has the procedure after the user has forgotten it.

### Remember how it broke
- What the user sees or does: When a step fails the same way twice, the butler says it failed this way before and goes straight to what worked, or gives up once instead of looping.
- Mechanism: append a failure fingerprint per step — (reason, frontmost app, window title, label actually present) — to the session file; the engine checks it before spending an escalation, and a repeat is reported with the prior attempt attached.
- Cost: compute Low (uses the snapshot already taken at failure, no extra capture); permissions Low; code Low-Med (fingerprint granularity is the risk: too exact never matches, too loose mislabels).
- Why it is not a gimmick: it stops the second occurrence of a known problem from costing another round trip, which is the only reason to have memory at all.

## 4. PRESENCE

### Badge carries the count
- What the user sees or does: The badge next to the pet reads "3/7" while a guide runs, and shows a hollow pip when it is waiting on the user rather than on itself.
- Mechanism: extend the existing 26x24 badge draw into a small state slab fed by the same guide dict the ring uses (idx, total, status); the sprite is untouched, so any petdex pet works.
- Cost: compute Low (redraw on state change only, zero when idle); permissions Low; code Low-Med (text metrics in a 26px box is the annoying part).
- Why it is not a gimmick: it answers "is it working or is it waiting on me?" at a glance, without opening the pane or scrolling a chat.

### Say why, next to the thing
- What the user sees or does: When a step cannot proceed, one line sits beside the control it is waiting on ("sign in first"), where the user is already looking.
- Mechanism: the ring window already knows the target box and the companion already owns a text-capable window (the 280x34 ask field); add a non-interactive caption window positioned by the same placement function and driven by `state.note`, hard-limited to one line.
- Cost: compute Low; permissions Low; code Med (a third window class, collision handling with the ring, and it must never take focus or clicks).
- Why it is not a gimmick: the stall is the exact moment the user is looking at the screen and not at the chat, so a reason stored anywhere else is a reason that gets missed.

### End with a receipt
- What the user sees or does: When the guide finishes, the pane shows one line per step of what was actually verified, and the pet goes quiet.
- Mechanism: `verifications[]` already records (step, reason, timestamp); render it as a compact receipt in the pane and keep it in the session file.
- Cost: compute Low; permissions Low; code Low (the data exists; this is rendering).
- Why it is not a gimmick: it is the difference between "the pet said done" and a record the user can check a week later.

## 5. ECOSYSTEM

### Point-at-a-box API
- What the user sees or does: Other plugins can highlight a control in their own walkthroughs and the same ring appears, with no second overlay on screen.
- Mechanism: the plugin already serves a local router (`/state`, `/config`, `/summon/*`); add `POST /ring` (a box, or app plus label through `best_match`) and `POST /capture`, guarded by a token file in the plugin data dir and backed by the same companion.
- Cost: compute Low; permissions Low (one grant subject, unchanged); code Med (API surface, auth, and a versioned response shape other plugins can depend on).
- Why it is not a gimmick: every plugin that wants to show the user where to click either cannot or duplicates the Swift layer, so this turns the overlay into shared infrastructure.

### Publish what was verified
- What the user sees or does: Other plugins hear "step 4 verified in Acme, window Invoices" while a guide runs, so a time-tracking or workstream tool logs real progress without watching the screen.
- Mechanism: an append-only local event line (or a tiny unix socket) emitted where the engine already writes `state.json` — guide_started, step_verified, stalled, done — carrying app, window, label, timestamp. Verified transitions only, never raw clicks.
- Cost: compute Low (one write per transition, not per poll); permissions Low; code Low-Med.
- Why it is not a gimmick: it is the only honest record of what the user did on this machine that any plugin can get without Screen Recording or input monitoring.

### Guides travel as files
- What the user sees or does: The user exports a finished guide as one file, a teammate imports it, and gets the same walkthrough with a warning if their app version differs.
- Mechanism: `guides/<id>.json` is already the whole procedure; export strips local ids and adds (bundle id, app version, window signature) so import can warn rather than fail mysteriously.
- Cost: compute Low; permissions Low; code Low (the schema exists; this is a header and a sanity check).
- Why it is not a gimmick: onboarding procedures are the most repeated thing in any team and today they live in screenshots that go stale silently.

---

## Killed

- Per-step model verification ("just look at the screenshot each step"): the AX read is deterministic and free and already has a fallback ladder; a model call per step adds seconds of latency and a second source of truth that can disagree with the layer doing the work.
- Agent-driven mouse puppeteering (drags and coordinate clicks): nothing to verify afterwards, it breaks on the first window move, and the user completes the drag faster than the agent plans it.
- Proactive "you look stuck" nudges: no local signal separates thinking from stuck, so it fires on people who are reading and interrupts the one thing the overlay is supposed to avoid.

## Sequencing

Ship first, in this order:

1. **Ring only what's live** — smallest diff, stops the ring lying, no new UI, and it makes every later card's failures legible.
2. **Remember where things were** — run two is the whole thesis, and the design already exists in `docs/deferred-steps.md`.
3. **Press the ringed control** — the first hand, bounded by the ring and an explicit gesture, and the difference between an instructional guide and a finishable one.

If there is time for a fourth, **Badge carries the count** is the cheapest visible change.

Biggest trap: **unattended autopilot** — chaining "press the ringed control" into "run the whole guide by itself". Unattended runs are refused today for good reason; every app quirk then becomes a butler bug, the demo is excellent, and the support surface is unbounded. The line to hold: a human gesture starts each action, a recipe aborts at the first mismatch, and no retry loop ever exists.
