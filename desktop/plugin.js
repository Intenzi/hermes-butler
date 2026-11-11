/**
 * Butler — guide-state pane for the Hermes desktop.
 *
 * Backend: ~/.hermes/plugins/butler/dashboard/plugin_api.py, mounted at
 * /api/plugins/butler/. The pane polls /state for the live guide and /history
 * for finished ones. The status bar carries a small Butler launcher that
 * creates or focuses the "Butler" chat.
 *
 * SDK-first surface (routes, nav, palette, status bar, keybinds, host sessions,
 * composer submit). The one deliberate reach-in is the always-on pet-click
 * receiver: it only READS the pet canvas rect and observes pointerdown; it
 * never paints into the canvas and is torn down with the plugin.
 * Bubble rendering stays on the Python side via ctx.pet.say.
 */
import {
  Button,
  Codicon,
  EmptyState,
  KEYBINDS_AREA,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  TITLEBAR_AREAS,
  cn,
  host,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useRef, useState } from 'react'

const ID = 'butler'
const ROUTE = '/butler'
const REFRESH_MS = 1500
const HISTORY_PAGE = 8
const BUTLER_TITLE = 'Butler'
const SUMMON_PLACEHOLDER = 'Ask the butler for a guide…'
// The in-place open re-renders the primary and the Butler composer mounts a
// few frames later; a zero-delay first submit loses that mount race outright.
// Give it a real grace, then keep re-deriving the live address for six seconds.
const SUMMON_SEND_GRACE_MS = 300
const SUMMON_SEND_RETRY_MS = 150
const SUMMON_SEND_TIMEOUT_MS = 6000
// A full session.list per 150 ms retry would hammer the gateway. Re-derive
// between attempts, but at most this often.
const SUMMON_LIVE_REFRESH_MS = 600
const CONFIG_REFRESH_MS = 5000
// Documented legacy locator for the petdex pet (the core pet canvas). Read-only:
// we never paint into it and never edit Hermes core to add a hook. The bare
// "pet" form is what the core emits when the pet has no displayName.
const PET_CANVAS_SELECTORS = ['canvas[aria-label$=" pet"]', 'canvas[aria-label="pet"]']
const PET_STATUS_REFRESH_MS = 2000
// The host clamps a pet line's ttl to 30 s, so an ambient watch re-says its
// count every 25 s: the pet must never look idle for half a minute while it is
// the only surface a background watch has.
const WATCH_KEEPALIVE_MS = 25000
const WATCH_TTL_MS = 30000
// A watch that ended (the app quit, the user stopped it) gets one line, long
// enough to read once, then the pane row is gone.
const WATCH_ENDED_TTL_MS = 9000
// The click-flash is a confirmation, not a readout: keep it brief.
const SUMMON_FLASH_MS = 1500
// A fresh session frees the shared display title by renaming the prior chat.
// Identity is the id; the sentence after it is only how the chat reads.
const BUTLER_PREVIOUS_TITLE = 'Butler (previous)'
// Identity is the id, never the title: the gateway regenerates a chat's title
// from its content, so a chat we titled "Butler" is renamed once it has
// content. Keep the ordered ids the butler has actually used and route by them;
// a title match survives only as the legacy fallback below.
const TRACKED_CHAT_IDS_KEY = 'butler.chat.ids'
// Only the newest LIVE id is ever used. The two older entries exist for one
// case: the user deletes the current butler chat, and falling back to the
// previous one keeps continuity instead of minting a new chat behind their
// back. More than that is untested surface, not resilience.
const TRACKED_CHAT_IDS_LIMIT = 3
// One page of session.list. Hitting this page boundary means the response may
// be truncated, so absence from it is not confirmed absence.
const SESSION_LIST_LIMIT = 200
const SUMMON_DESTINATION_KEY = 'butler.summon.destination'
const SUMMON_EXISTING = 'existing'
const SUMMON_NEW = 'new'

let rest = null
let petCtx = null
let lastSaid = null
// The say.json id the pet last spoke, and when the answer's ttl runs out. The
// answer and the guide status line share one channel: a new id speaks over it,
// the status line stays quiet for the ttl, then resumes on its own.
let lastSpokenSayId = null
let answerUntil = 0
// When the shared status line last actually spoke, and the last ended-watch
// announcement. Both are module scope: the module speech beat and the pane's
// beat must not double-speak a kept-alive watch or a "Watch ended" line.
let lastSaidAt = 0
let lastWatchEndedKey = null
let launcherBusy = false
let launcherTargetId = null
// Runtime/stored aliases learned from session.list or an open-session result.
// The submit address is the stored id, but a live surface may answer only for
// the runtime id it was resumed under.
let launcherTargetAliases = []
// smoke-test counters so the page can PROVE the plugin side spoke (or not):
// petSayTicks increments on every say attempt, petLastTick stores wall-clock.
let petSayTicks = 0
let petLastTick = null

// Summon door state. The bar component is the only visible surface; the
// keybind and the installed-by-default pet-click listener just publish "open".
let summonOpen = false
let summonOrigin = 'shortcut'
// Increments on every pet-origin open; the pane row flashes when it changes.
let petClickSeq = 0
const summonListeners = new Set()
let petClickClaim = false
let petClickWatcher = null
let claimConfigTimer = null
// Module-scope consumer for the companion chip's summon row. The hook inside
// SummonBar only runs while that component is mounted; the chip click path must
// not depend on that, so the pending watcher lives here and the hook is only a
// fallback. lastPendingId dedupes a row that survives one poll before the ack.
let summonPendingTimer = null
let lastPendingId = null
// Has the in-window core pet canvas ever been found while the receiver was on?
// The pane chip uses this to say "armed" only when there is something to click.
let petCanvasSeen = false
// Destination picker state; localStorage is the cross-opening source of truth.
let summonDestination = null

function h(type, props, ...children) {
  let key = undefined
  const p = {}
  if (props) { for (const [k, v] of Object.entries(props)) { if (k === 'key') { key = v } else { p[k] = v } } }
  const flat = children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false)
  if (flat.length === 1) { p.children = flat[0] } else if (flat.length > 1) { p.children = flat }
  return flat.length > 1 ? jsxs(type, p, key) : jsx(type, p, key)
}

function asArray(value) {
  return Array.isArray(value) ? value : []
}

function asText(value) {
  if (value === null || value === undefined) return null
  const text = String(value).trim()
  return text || null
}

function asNumber(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

function timestampValue(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 100_000_000_000 ? value / 1000 : value
  }
  if (typeof value === 'string') {
    const numeric = Number(value)
    if (Number.isFinite(numeric)) return numeric > 100_000_000_000 ? numeric / 1000 : numeric
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed / 1000 : 0
  }
  return 0
}

function guideStartedAt(row) {
  return timestampValue(row && (row.started_at ?? row.created_at ?? row.updated_at
    ?? row.last_active_at ?? row.timestamp))
}

function sessionStartedAt(row) {
  return timestampValue(row && (row.started_at ?? row.created_at ?? row.updated_at
    ?? row.last_active_at ?? row.timestamp))
}

function guideStatus(guide) {
  const state = guide && guide.state && typeof guide.state === 'object' ? guide.state : {}
  return asText(state.status) || asText(guide && guide.status) || 'running'
}

function historyRow(guide) {
  const state = guide && guide.state && typeof guide.state === 'object' ? guide.state : {}
  return {
    id: asText(guide && guide.id) || `${asText(guide && guide.title) || 'guide'}-${guideStartedAt(guide)}`,
    title: asText(guide && guide.title) || 'Guide',
    status: asText(state.status) || asText(guide && guide.status) || 'done',
    steps: asArray(guide && guide.steps),
    verifications: asArray(guide && guide.verifications),
    started_at: guide && guide.started_at
  }
}

function newestFirst(rows) {
  return rows.slice().sort((a, b) => guideStartedAt(b) - guideStartedAt(a))
}

/** One checklist row: box (checked / current / pending) + step say-line. */
function TaskRow({ index = 0, text, boxState, done }) {
  const boxColor = done ? 'var(--ui-green, #6fd39a)'
    : boxState === 'current' ? 'var(--ui-accent-local, #9aa3ea)' : 'rgba(138,143,152,.65)'
  const textColor = done ? 'rgba(138,143,152,.95)' : '#eceef1'
  return h('div', {
    style: {
      display: 'flex', alignItems: 'center', gap: 9,
      padding: '7px 10px', borderRadius: 9,
      background: boxState === 'current' ? 'rgba(122,134,255,.08)' : 'rgba(255,255,255,.03)',
      border: boxState === 'current' ? '1px solid rgba(122,134,255,.35)' : '1px solid rgba(255,255,255,.06)'
    }
  },
    h('span', {
      style: {
        flex: 'none', width: 18, height: 18, borderRadius: 5,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 12, fontWeight: 700, color: boxColor,
        border: `1.5px solid ${boxColor}`
      }
    }, done ? '✓' : ''),
    h('span', { style: { fontSize: 12.5, color: textColor, lineHeight: 1.35 } },
      `Step ${index + 1}: ${asText(text) || ''}`)
  )
}

/** Vertical task checklist: checked boxes, current step highlighted. */
function StepsChecklist({ steps = [], verified = [], currentIdx = 0, live = false }) {
  const doneSet = new Set(asArray(verified).map(v => Number(v && typeof v === 'object' ? v.step : v)))
  return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 } },
    asArray(steps).map((s, i) => h(TaskRow, {
      key: i,
      index: i,
      text: asText(s && s.say),
      done: doneSet.has(i),
      boxState: doneSet.has(i) ? 'done' : (live && i === currentIdx ? 'current' : 'pending')
    }))
  )
}

/** Collapsed dropdown for one finished guide; expanding shows its checklist. */
function CompletedGuide({ guide, verified }) {
  const rows = asArray(verified)
  const title = asText(guide && guide.title) || 'Guide'
  const status = asText(guide && guide.status) || 'done'
  const steps = asArray(guide && guide.steps)
  return h('details', {
    style: {
      marginTop: 8, borderRadius: 10, border: '1px solid rgba(255,255,255,.08)',
      background: 'rgba(255,255,255,.03)', overflow: 'hidden'
    }
  },
    h('summary', {
      style: {
        cursor: 'pointer', listStyle: 'none', outline: 'none',
        padding: '8px 12px', fontSize: 12.5, color: '#8a8f98',
        display: 'flex', alignItems: 'center', gap: 8
      }
    },
      h('span', { style: { fontSize: 9, color: '#6f7784' } }, '▸'),
      h('span', { style: { color: '#c7cbd2', fontWeight: 500, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } },
        title),
      h('span', {
        style: { fontSize: 10.5, padding: '1px 8px', borderRadius: 99, border: '1px solid rgba(111,211,154,.4)', color: '#6fd39a' }
      }, `${rows.length}/${steps.length} verified`),
      h('span', { style: { fontSize: 10, color: '#6f7784' } }, status)
    ),
    h('div', { style: { padding: '0 12px 10px' } },
      h(StepsChecklist, { steps, verified: rows, currentIdx: Infinity, live: false })
    )
  )
}

/** Finished guides: newest first, eight collapsed dropdowns, then "show more". */
function HistoryList({ guides }) {
  const [limit, setLimit] = useState(HISTORY_PAGE)
  const rows = newestFirst(asArray(guides))
  const visible = rows.slice(0, limit)
  const remaining = Math.max(0, rows.length - visible.length)
  return h('div', { style: { display: 'flex', flexDirection: 'column' } },
    h('div', { style: { fontSize: 14, fontWeight: 600, color: '#eceef1', marginTop: 2 } },
      'No butler task running'),
    h('div', { style: { fontSize: 11.5, color: '#8a8f98', marginTop: 2 } },
      rows.length > 0
        ? `${rows.length} finished guide${rows.length === 1 ? '' : 's'} — expand one to review its checklist.`
        : 'Finished guides will appear here.'),
    visible.map((guide, index) => h(CompletedGuide, {
      key: guide.id || index,
      guide,
      verified: guide.verifications
    })),
    remaining > 0 && h('button', {
      type: 'button',
      onClick: () => setLimit(current => current + HISTORY_PAGE),
      style: {
        marginTop: 8, alignSelf: 'flex-start', cursor: 'pointer',
        fontSize: 11.5, color: '#9aa3ea', background: 'transparent',
        border: '1px solid rgba(122,134,255,.35)', borderRadius: 7,
        padding: '4px 10px'
      }
    }, `show ${remaining} more`)
  )
}

// ---------------------------------------------------------------- launcher

function exactButlerTitle(row) {
  const title = asText(row && row.title)
  return title === BUTLER_TITLE ? row : null
}

/**
 * The most recent chat still titled exactly "Butler".
 *
 * Several can exist: an older build, a failed rename, or a user who renamed one
 * back. The chat people mean by "the Butler chat" is the newest one, so it wins
 * over the oldest — a fixed target drifts further from their work every day.
 */
function mostRecentSession(rows) {
  const matching = asArray(rows).map(exactButlerTitle).filter(Boolean)
  matching.sort((a, b) => sessionStartedAt(b) - sessionStartedAt(a))
  return matching[0] || null
}

/**
 * Every session the host will list; `null` when the list could not be read.
 *
 * The distinction matters for the tracked ids: an absent session may be pruned
 * from the list, but a failed `session.list` must never be read as "all gone".
 */
async function listSessions() {
  if (!host || typeof host.request !== 'function') return null
  try {
    const result = await host.request('session.list', { limit: SESSION_LIST_LIMIT })
    if (Array.isArray(result)) return result
    const sessions = result && result.sessions
    // A success-shaped response that carries no rows is not an empty inventory;
    // treating it as one would prune every tracked id.
    return Array.isArray(sessions) ? sessions : null
  } catch (err) {
    return null
  }
}

/**
 * Full session list, title filter deliberately absent.
 *
 * A renamed chat is still the Butler chat; only the legacy fallback in
 * `legacyButlerChat` may look at titles.
 */
async function listButlerSessions() {
  return listSessions()
}

function normalizeTrackedChatIds(value) {
  const ids = []
  for (const entry of asArray(value)) {
    const id = asText(entry)
    if (id && !ids.includes(id)) ids.push(id)
    if (ids.length >= TRACKED_CHAT_IDS_LIMIT) break
  }
  return ids
}

/** Ordered Butler chat ids, newest first. A denied/unreadable store reads empty. */
function readTrackedChatIds() {
  try {
    const store = localStore()
    if (!store) return []
    const raw = store.getItem(TRACKED_CHAT_IDS_KEY)
    if (!raw) return []
    return normalizeTrackedChatIds(JSON.parse(raw))
  } catch (err) {
    return []
  }
}

function writeTrackedChatIds(ids) {
  const next = normalizeTrackedChatIds(ids)
  try {
    const store = localStore()
    if (store) store.setItem(TRACKED_CHAT_IDS_KEY, JSON.stringify(next))
  } catch (err) {
    // A storage-denied renderer still routes for this opening.
  }
  return next
}

/** Prepend ids (newest first), dedupe, cap; returns the stored order. */
function rememberChatIds(...values) {
  const fresh = uniqueIds(...values)
  if (!fresh.length) return readTrackedChatIds()
  return writeTrackedChatIds(uniqueIds(fresh, readTrackedChatIds()))
}

function sessionTargetId(row) {
  return asText(row && (row.resolved_id || row.stored_session_id
    || row.id || row.session_id))
}

/** Profile the live gateway is routed to, as the gateway names it in a key. */
function hostProfileName() {
  try {
    const state = host && host.state
    for (const atom of [state && state.focusedSessionProfile, state && state.profile]) {
      if (atom && typeof atom.get === 'function') {
        const value = asText(atom.get())
        if (value) return value
      }
    }
  } catch (err) {
    // An older host without host.state simply skips key verification.
  }
  return null
}

/** `agent:<ns>` uses `main` for the default profile and `main~` for a literal main. */
function sessionKeyNamespace(profile) {
  const name = asText(profile)
  if (!name || name === 'default') return 'main'
  return name === 'main' ? 'main~' : name
}

/** The key the gateway must report for a local DM routed to this exact session. */
function expectedSummonSessionKey(profile, sessionId) {
  const id = asText(sessionId)
  if (!id) return null
  return `agent:${sessionKeyNamespace(profile)}:local:dm:${id}`
}

/** A collision rename that keeps the sentence readable and unique per old id. */
function previousButlerTitle(index = 0) {
  if (index <= 0) return BUTLER_PREVIOUS_TITLE
  return `${BUTLER_PREVIOUS_TITLE.slice(0, -1)} ${index + 1})`
}

/**
 * Set a display title, tolerating the canonical gateway's revision fence.
 * Metadata edits travel as `session.mutate` and need a revision the renderer
 * learns from a resume snapshot. A quiet resume (no navigation, no messages)
 * primes it for a chat that is not on screen; it is only attempted on failure.
 */
async function renameButlerTitle(sessionId, title) {
  const id = asText(sessionId)
  if (!id || !host || typeof host.request !== 'function') return false
  try {
    await host.request('session.title', { session_id: id, title })
    return true
  } catch (err) {
    // Fall through to a quiet revision prime.
  }
  try {
    await host.request('session.resume', { session_id: id, omit_messages: true })
    await host.request('session.title', { session_id: id, title })
    return true
  } catch (err) {
    return false
  }
}

/** Every id a live session row might answer to (stored and runtime). */
function rowTargetIds(row) {
  return uniqueIds(
    row && row.resolved_id,
    row && row.session_id,
    row && row.stored_session_id,
    row && row.id
  )
}

/** Ids handed back by a host.openSession implementation, if it returns one. */
function openResultIds(result) {
  if (!result || result === true) return []
  if (typeof result === 'string') return uniqueIds(result)
  if (typeof result !== 'object') return []
  return uniqueIds(
    result.resolved_id,
    result.resolvedId,
    result.session_id,
    result.sessionId,
    result.stored_session_id,
    result.storedSessionId,
    result.runtime_id,
    result.runtimeId,
    result.id
  )
}

function uniqueIds(...values) {
  const ids = []
  const add = value => {
    if (Array.isArray(value)) {
      for (const entry of value) add(entry)
      return
    }
    const id = asText(value)
    if (id && !ids.includes(id)) ids.push(id)
  }
  for (const value of values) add(value)
  return ids
}

function addTargetIds(list, values) {
  for (const id of uniqueIds(values)) {
    if (!list.includes(id)) list.push(id)
  }
}

/**
 * The newest tracked id that still exists in `session.list`.
 *
 * A tracked id is pruned only when the list was actually read and does not
 * contain it in any id form. On a failed list the stored order is returned
 * untouched and its newest id is still used, so a transient gateway hiccup
 * cannot mint a duplicate Butler chat.
 */
function trackedButlerChat(rows) {
  const stored = readTrackedChatIds()
  if (!stored.length) return null
  if (!Array.isArray(rows)) {
    return { target: stored[0], ids: [stored[0]], row: null, verified: false }
  }
  const surviving = stored.filter(id => rows.some(row => rowTargetIds(row).includes(id)))
  // A full page may be truncated; absence from it does not confirm absence from
  // the account, so only prune when the whole list came back.
  if (rows.length < SESSION_LIST_LIMIT && surviving.length !== stored.length) {
    writeTrackedChatIds(surviving)
  }
  const target = surviving[0]
  if (!target) return null
  const row = rows.find(entry => rowTargetIds(entry).includes(target)) || null
  return { target, ids: uniqueIds(target, rowTargetIds(row)), row, verified: true }
}

/** Legacy fallback: a chat still titled exactly Butler, remembered once found. */
function legacyButlerChat(rows) {
  const row = mostRecentSession(rows)
  const target = sessionTargetId(row)
  if (!target) return null
  // Only a row the session list actually returned can be remembered: a title
  // match is never trusted on its own.
  rememberChatIds(target)
  return { target, ids: uniqueIds(target, rowTargetIds(row)), row, verified: true }
}

function launcherState(ok, reason = null, error = null) {
  return {
    ok,
    target: asText(launcherTargetId),
    ids: uniqueIds(launcherTargetAliases),
    reason,
    error
  }
}

function setLauncherTarget(target, ids = []) {
  launcherTargetId = asText(target)
  launcherTargetAliases = uniqueIds(ids, launcherTargetId)
}

async function focusSession(row) {
  if (!row) return launcherState(false, 'no_session')
  const storedId = sessionTargetId(row)
  if (!storedId) return launcherState(false, 'no_session')
  let lastError = null
  if (host && typeof host.openSession === 'function') {
    try {
      const opened = await host.openSession(storedId, { intent: 'in-place' })
      setLauncherTarget(storedId, uniqueIds(rowTargetIds(row), openResultIds(opened)))
      rememberChatIds(storedId)
      return launcherState(true)
    } catch (err) {
      lastError = err
    }
  }
  if (!host || typeof host.request !== 'function') {
    return launcherState(false, 'session_focus_failed', lastError)
  }
  try {
    const activated = await host.request('session.activate', {
      session_id: asText(row.session_id) || asText(row.id) || storedId
    })
    setLauncherTarget(storedId, uniqueIds(rowTargetIds(row), openResultIds(activated)))
    rememberChatIds(storedId)
    return launcherState(true)
  } catch (err) {
    return launcherState(false, 'session_focus_failed', err)
  }
}

/**
 * Label a freshly minted id as the one "Butler" chat: release the display
 * title from every prior titled chat, then claim it for the new id.
 *
 * `session.create` with a title is title-as-identity on the gateway (it
 * resolves the EXISTING chat instead of minting one), so a new chat is always
 * created untitled and titled only afterwards. Renames are cosmetic: ids
 * route, titles label, and a refused rename never blocks a send.
 */
async function claimButlerTitle(target, runtimeId, previousSessions = []) {
  // Only chats still carrying the exact title need it released; the full
  // session list is passed in, and renaming every unrelated chat would be a
  // destructive surprise.
  const previous = asArray(previousSessions)
    .filter(row => exactButlerTitle(row))
    .slice()
    .sort((a, b) => sessionStartedAt(a) - sessionStartedAt(b))
  for (let index = 0; index < previous.length; index += 1) {
    const oldId = sessionTargetId(previous[index])
    if (!oldId || oldId === target) continue
    await renameButlerTitle(oldId, previousButlerTitle(index))
  }
  if (runtimeId) {
    // The untitled chat is still a valid destination if this fails; the title
    // is cosmetic, the id is the route.
    await renameButlerTitle(runtimeId, BUTLER_TITLE)
  }
}

/**
 * Mint a genuinely new chat untitled, claim the display title for it, and
 * remember the id that actually routes the send. Throws on transport failure;
 * marks a gateway reuse with `code = 'reused'` so callers can tell it apart.
 */
async function mintButlerChat(previousSessions = []) {
  const created = await host.request('session.create', {})
  const runtimeId = asText(created && created.session_id)
  const storedId = asText(created && created.stored_session_id) || runtimeId
  const target = storedId || runtimeId
  if (!target) throw new Error('session.create returned no session id')
  const previous = asArray(previousSessions)
    .slice()
    .sort((a, b) => sessionStartedAt(a) - sessionStartedAt(b))
  if (previous.some(row => sessionTargetId(row) === target)) {
    // Defensive: the whole roster is passed in, so an untitled create must never
    // answer with any existing chat — before it can be retitled or tracked.
    const error = new Error('session.create reused an existing Butler session')
    error.code = 'reused'
    throw error
  }
  // Free 'Butler' from every prior titled chat before claiming it. A failed
  // rename is not a send failure: the fresh id still owns the route.
  await claimButlerTitle(target, runtimeId, previous)
  rememberChatIds(target)
  return { created, runtimeId, storedId, target, previous }
}

/**
 * Mint a genuinely new chat, then label it and focus it.
 *
 * `session.create` with a `title` is title-as-identity on the gateway: it
 * resolves and returns the EXISTING chat holding that title instead of minting
 * one. That is correct for a forever-chat, wrong for "New session". So create
 * UNTITLED (the id is fresh), free the display title from the old chats, and
 * only then set 'Butler' on the new id. Titles are labels; ids route the send.
 */
async function createButlerSession(previousSessions = []) {
  if (!host || typeof host.request !== 'function') {
    return launcherState(false, 'session_create_unavailable')
  }
  let minted = null
  try {
    minted = await mintButlerChat(previousSessions)
  } catch (err) {
    if (err && err.code === 'reused') {
      return launcherState(false, 'session_create_reused')
    }
    return launcherState(false, 'session_create_failed', err)
  }
  const { created, runtimeId, target } = minted
  let lastError = null
  if (host && typeof host.openSession === 'function') {
    try {
      const opened = await host.openSession(target, { intent: 'in-place' })
      setLauncherTarget(target, uniqueIds(
        target, runtimeId, openResultIds(created), openResultIds(opened)))
      return launcherState(true)
    } catch (err) {
      lastError = err
    }
  }
  if (runtimeId && host && typeof host.request === 'function') {
    try {
      const activated = await host.request('session.activate', { session_id: runtimeId })
      setLauncherTarget(target, uniqueIds(
        target, runtimeId, openResultIds(created), openResultIds(activated)))
      return launcherState(true)
    } catch (err) {
      return launcherState(false, 'session_focus_failed', err)
    }
  }
  return launcherState(false, 'session_focus_failed', lastError)
}

function notifyLauncherFailure() {
  try {
    host && host.notify && host.notify({
      kind: 'error',
      title: 'Butler',
      message: 'Could not create or focus the Butler chat on this desktop build.'
    })
  } catch (err) {
    // A missing toast surface must not break the click.
  }
}

function notifySummonFailure(message) {
  try {
    host && host.notify && host.notify({
      kind: 'error',
      title: 'Butler',
      message: message || 'Could not send that request to the Butler chat on this desktop build.'
    })
  } catch (err) {
    // A missing toast surface must not break the summon.
  }
}

function notifySummonSuccess(result) {
  const destination = result && result.destination
  const fresh = destination === SUMMON_NEW
  let message = fresh ? 'Summoned → fresh Butler session' : 'Summoned → Butler chat (existing)'
  let kind = 'success'
  if (result && result.fallback) {
    // Injection was denied/unavailable, so the chip path carried the request.
    // Say so: the user must not read this as the full agent conversation.
    kind = 'warning'
    const reason = asText(result.injection_reason) || 'injection_unavailable'
    const reasonLabel = reason === 'injection_denied'
      ? 'was denied'
      : reason === 'unroutable_session'
        ? 'could not route this session (session not routable through the gateway)'
        : 'was unavailable'
    message += ` — gateway injection ${reasonLabel}`
      + ' (' + reason + '); sent /butler instead, so this chat could not host the full conversation.'
    const detail = asText(result.injection_detail)
    if (detail) message += ` — ${detail}`
  }
  try {
    host && host.notify && host.notify({ kind, title: 'Butler', message })
  } catch (err) {
    // A missing toast surface must not break the send.
  }
}

/**
 * Focus the tracked Butler chat, or create one.
 *
 * Resolution is id-first so a gateway-renamed chat is still found; the exact
 * title match is only the legacy fallback for a desktop that never tracked an
 * id. If focusing fails, surface the reason instead of quietly minting a
 * different session behind the user's back.
 */
async function openButlerSession() {
  if (launcherBusy) return launcherState(false, 'busy')
  launcherBusy = true
  launcherTargetId = null
  launcherTargetAliases = []
  try {
    const rows = await listSessions()
    const recent = trackedButlerChat(rows) || legacyButlerChat(rows)
    if (recent) {
      // A failed session.list still yields the newest tracked id (with no row
      // to verify), so focus can be addressed by that id alone.
      const focused = await focusSession(recent.row || { id: recent.target })
      if (!focused.ok) notifyLauncherFailure()
      return focused
    }

    let created
    try {
      created = await createButlerSession(asArray(rows))
    } catch (err) {
      created = launcherState(false, 'session_create_failed', err)
    }
    if (!created || !created.ok) notifyLauncherFailure()
    return created || launcherState(false, 'session_create_failed')
  } finally {
    launcherBusy = false
  }
}

// ------------------------------------------------------------ summon door

function wait(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

// ------------------------------------------------------- summon destination

function localStore() {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch (err) {
    return null
  }
}

function readSummonDestination() {
  try {
    const store = localStore()
    return store && store.getItem(SUMMON_DESTINATION_KEY) === SUMMON_NEW
      ? SUMMON_NEW : SUMMON_EXISTING
  } catch (err) {
    return SUMMON_EXISTING
  }
}

function getSummonDestination() {
  if (summonDestination === null) summonDestination = readSummonDestination()
  return summonDestination
}

function setSummonDestination(value) {
  summonDestination = value === SUMMON_NEW ? SUMMON_NEW : SUMMON_EXISTING
  try {
    const store = localStore()
    if (store) store.setItem(SUMMON_DESTINATION_KEY, summonDestination)
  } catch (err) {
    // A storage-denied renderer still keeps the choice for this session.
  }
  return summonDestination
}

/** The bar always sends the slash form; plain chat still sends plain turns. */
function butlerCommandText(value) {
  const text = asText(value) || ''
  return /^\s*\//.test(text) ? text : `/butler ${text}`
}

function petCanvasNode() {
  if (typeof document === 'undefined') return null
  for (const selector of PET_CANVAS_SELECTORS) {
    try {
      const canvas = document.querySelector(selector)
      if (canvas) return canvas
    } catch (err) {
      // Try the next documented spelling.
    }
  }
  return null
}

/**
 * The pane's persistent two-field readout. The chip is on for an installed
 * plugin; `petFound` is the in-app canvas query and `flash` is the brief
 * confirmation after a claimed click opened the bar. It never becomes a
 * multi-line nag and it has no switch semantics.
 */
function petStatusRowText({ claim = true, petFound = false, flash = false } = {}) {
  const row = `chip: ${claim ? 'on' : 'off'} · pet (in-app): ${petFound ? 'found' : 'not found'}`
  return flash ? `${row} · → bar opened` : row
}

/** The shared publish payload: open state plus the pet-click confirmation seq. */
function summonBarState() {
  return { open: summonOpen, origin: summonOrigin, petClickSeq }
}

function subscribeSummonBar(listener) {
  summonListeners.add(listener)
  try {
    listener(summonBarState())
  } catch (err) {
    // A broken listener must not break the summon.
  }
  return () => { summonListeners.delete(listener) }
}

function publishSummonBar() {
  const state = summonBarState()
  for (const listener of [...summonListeners]) {
    try {
      listener(state)
    } catch (err) {
      // Keep the remaining listeners alive.
    }
  }
}

function openSummonBar(origin = 'shortcut') {
  if (origin === 'pet') petClickSeq += 1
  summonOpen = true
  summonOrigin = origin
  publishSummonBar()
}

function dismissSummonBar() {
  summonOpen = false
  publishSummonBar()
}

function configPetClickClaim(data) {
  // Installed means on. Only an explicit false in the response (the stored
  // escape hatch) turns the chip off; an older host that omits the key keeps it
  // on rather than silently hiding the chip.
  if (!data || typeof data !== 'object') return true
  if (data.summon && typeof data.summon === 'object'
    && typeof data.summon.pet_click_claim === 'boolean') {
    return data.summon.pet_click_claim
  }
  if (typeof data['summon.pet_click_claim'] === 'boolean') {
    return data['summon.pet_click_claim']
  }
  if (typeof data.pet_click_claim === 'boolean') return data.pet_click_claim
  return true
}

function startPetClickWatcher() {
  if (petClickWatcher || typeof document === 'undefined' || !document.body) return
  let overlay = null
  let frame = null
  let disposed = false
  let lastRect = null
  try {
    overlay = document.createElement('div')
    overlay.setAttribute('data-butler-pet-claim', '')
    overlay.setAttribute('aria-hidden', 'true')
    Object.assign(overlay.style, {
      position: 'fixed', pointerEvents: 'none', zIndex: '2147483000',
      left: '0px', top: '0px', width: '0px', height: '0px',
      opacity: '0', borderRadius: '10px'
    })
    document.body.appendChild(overlay)
  } catch (err) {
    overlay = null
  }

  const follow = () => {
    if (disposed) return
    const canvas = petCanvasNode()
    if (!canvas || !overlay) {
      if (overlay) overlay.style.opacity = '0'
      frame = window.requestAnimationFrame(follow)
      return
    }
    try {
      const rect = canvas.getBoundingClientRect()
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        overlay.style.opacity = '0'
      } else {
        petCanvasSeen = true
        const box = { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
        if (!lastRect || Math.abs(lastRect.left - box.left) > 0.5
          || Math.abs(lastRect.top - box.top) > 0.5
          || Math.abs(lastRect.width - box.width) > 0.5
          || Math.abs(lastRect.height - box.height) > 0.5) {
          overlay.style.left = `${box.left}px`
          overlay.style.top = `${box.top}px`
          overlay.style.width = `${box.width}px`
          overlay.style.height = `${box.height}px`
          lastRect = box
        }
        overlay.style.opacity = '1'
      }
    } catch (err) {
      if (overlay) overlay.style.opacity = '0'
    }
    frame = window.requestAnimationFrame(follow)
  }

  const onPointerDown = (event) => {
    if (!petClickClaim || disposed) return
    try {
      if (event && typeof event.button === 'number' && event.button !== 0) return
      const canvas = petCanvasNode()
      if (!canvas) return
      petCanvasSeen = true
      const rect = canvas.getBoundingClientRect()
      if (!rect || rect.width <= 0 || rect.height <= 0) return
      const x = Number(event && event.clientX)
      const y = Number(event && event.clientY)
      if (!Number.isFinite(x) || !Number.isFinite(y)) return
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        openSummonBar('pet')
      }
    } catch (err) {
      // Never let the reach-in listener break the pet's own click handling.
    }
  }

  try {
    document.addEventListener('pointerdown', onPointerDown, true)
  } catch (err) {
    // A DOM without capture-phase events still leaves the shortcut working.
  }
  frame = window.requestAnimationFrame(follow)

  petClickWatcher = {
    dispose() {
      if (disposed) return
      disposed = true
      try { document.removeEventListener('pointerdown', onPointerDown, true) } catch (err) { }
      try { if (frame !== null) window.cancelAnimationFrame(frame) } catch (err) { }
      frame = null
      try { if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay) } catch (err) { }
      overlay = null
      petClickWatcher = null
    }
  }
}

function stopPetClickWatcher() {
  if (petClickWatcher) petClickWatcher.dispose()
}

function setPetClickClaim(enabled) {
  const next = Boolean(enabled)
  if (!next) {
    petClickClaim = false
    petCanvasSeen = false
    stopPetClickWatcher()
    return
  }
  petClickClaim = true
  startPetClickWatcher()
}

async function syncCompanionClaim(enabled) {
  if (!rest || typeof rest !== 'function') return false
  try {
    const result = await rest('/summon/claim', {
      method: 'POST',
      body: { enabled: Boolean(enabled) }
    })
    return Boolean(result && result.enabled === Boolean(enabled))
  } catch (err) {
    // Companion unavailable is a degrade: the in-window listener and the
    // shortcut still work, so this is not a toggle failure.
    return false
  }
}

async function syncPetClickClaim() {
  if (!rest || typeof rest !== 'function') return
  try {
    const result = await rest('/config')
    // Missing/legacy responses resolve to on; only a stored false disarms.
    const enabled = configPetClickClaim(result)
    setPetClickClaim(enabled)
    // Keep the pop-out chip on the same config value, and refresh its process
    // heartbeat so the companion's idle watchdog does not retire it.
    await syncCompanionClaim(enabled)
  } catch (err) {
    // Backend not mounted yet; register already armed the chip directly.
  }
}

/** Poll the config even on older hosts whose ctx lacks a tracked setInterval. */
function startClaimConfigPolling(ctx) {
  const tick = () => { syncPetClickClaim().catch(() => {}) }
  try {
    if (ctx && typeof ctx.setInterval === 'function') {
      ctx.setInterval(tick, CONFIG_REFRESH_MS)
      return
    }
  } catch (err) {
    // Fall through to the window timer below.
  }
  try {
    claimConfigTimer = window.setInterval(tick, CONFIG_REFRESH_MS)
  } catch (err) {
    // No timer surface: register already armed the chip directly.
  }
}

/**
 * The Butler chat id an in-place chip send should target. Never focuses a
 * window: the id is the whole answer, and that is the point of the field.
 *
 * "recent" resolves id-first — newest tracked id that still exists, then the
 * legacy exact-title chat, then a fresh untitled chat. A title is only a label
 * now; the gateway renames chats from their content. "new" ALWAYS mints a fresh
 * chat and never falls back to an existing one: when creation fails the caller
 * reports `session_create_failed` and nothing is sent.
 */
async function resolveChipSendTarget(destination = 'recent') {
  const wantsNew = destination === 'new'
  if (!host || typeof host.request !== 'function') return null
  try {
    const rows = await listSessions()
    if (wantsNew) {
      // Mint untitled first, exactly like createButlerSession: a titled create
      // would hand back the existing Butler chat instead of a fresh one.
      const minted = await mintButlerChat(asArray(rows))
      return minted.target
    }
    const recent = trackedButlerChat(rows) || legacyButlerChat(rows)
    if (recent) return recent.target
    const minted = await mintButlerChat(asArray(rows))
    return minted.target
  } catch (err) {
    // Creation/routing failed: report no session rather than a half-send.
    return null
  }
}

function notifyChipSendSuccess(destination = 'recent') {
  try {
    host && host.notify && host.notify({
      kind: 'success',
      title: 'Butler',
      message: destination === 'new' ? 'Started a new Butler chat' : 'Sent to Butler'
    })
  } catch (err) {
    // A missing toast surface must not break the send.
  }
}

function notifyChipSendFailure(result) {
  const reason = asText(result && result.reason) || 'send_failed'
  const label = SUMMON_REASON_LABELS[reason] || reason
  let message = `Could not send to Butler: ${label}`
  if (label !== reason) message += ` (${reason})`
  const detail = asText(result && result.detail) || summonErrorText(result && result.error)
  if (detail && detail !== label && detail !== reason) message += ` — ${detail}`
  try {
    host && host.notify && host.notify({ kind: 'error', title: 'Butler', message })
  } catch (err) {
    // A missing toast surface must not break the poll.
  }
}

/**
 * The turn a LOOK row posts to the Butler chat: the element the user pointed at
 * (context first) and their question (second). Every part the companion could
 * not read is omitted, never rendered as "undefined".
 */
function lookPendingText(pending) {
  const row = pending && typeof pending === 'object' ? pending : {}
  const target = row.target && typeof row.target === 'object' ? row.target : {}
  const parts = []
  const app = asText(target.app)
  if (app) parts.push(`App: ${app}`)
  const windowName = asText(target.window)
  if (windowName) parts.push(`Window: ${windowName}`)
  const control = []
  const role = asText(target.role)
  if (role) control.push(role)
  const label = asText(target.label)
  if (label) control.push(`"${label}"`)
  if (typeof target.enabled === 'boolean') {
    control.push(target.enabled ? '(enabled)' : '(disabled)')
  }
  const box = Array.isArray(target.box) && target.box.length === 4
    ? target.box.map(value => {
        const number = Number(value)
        return Number.isFinite(number) ? String(Math.round(number)) : null
      })
    : null
  if (box && box.every(value => value !== null)) {
    control.push(`at ${box.join(',')}`)
  }
  if (control.length) parts.push(`Control: ${control.join(' ')}`)
  const head = parts.length ? `[butler look] ${parts.join(' · ')}` : '[butler look]'
  return `${head}\nQuestion: ${asText(row.text) || ''}`
}

function notifyLookAnswered() {
  try {
    host && host.notify && host.notify({
      kind: 'success',
      title: 'Butler',
      message: 'Asked about that control'
    })
  } catch (err) {
    // A missing toast surface must not break the send.
  }
}

async function sendPendingSummonText(text, destination = 'recent', options = {}) {
  const target = await resolveChipSendTarget(destination)
  if (!target) {
    // "new" was asked for: never report a send that did not happen.
    return {
      ok: false,
      reason: destination === 'new' ? 'session_create_failed' : 'no_session'
    }
  }
  const body = { text, session_id: target }
  // Only the LOOK door stamps an origin: the route then frames the turn with
  // the "answer on the pet" instruction. Plain ask-field rows stay unchanged.
  const origin = asText(options && options.origin)
  if (origin) body.origin = origin
  try {
    const response = await rest('/summon/send', {
      method: 'POST',
      body
    })
    if (response && response.ok === true) {
      // A successful send proves the chat is in use: it is now the newest
      // tracked id even if a title match, not the tracked list, resolved it.
      rememberChatIds(target)
      return { ok: true, session_id: target, destination }
    }
    return {
      ok: false,
      reason: asText(response && response.reason) || 'send_failed',
      detail: asText(response && response.detail),
      error: response && response.error
    }
  } catch (err) {
    return { ok: false, reason: 'send_failed', error: err }
  }
}

function ackSummonPending(pendingId) {
  // Fire and forget: a failed ack never stops the poll and the row is already
  // deduped, so the same action cannot run twice for this id.
  try {
    const ack = rest('/summon/ack', { method: 'POST', body: { id: pendingId } })
    if (ack && typeof ack.catch === 'function') ack.catch(() => {})
  } catch (err) {
    // Swallow: the next /state tick still reports the row unconsumed.
  }
}

/**
 * Consume one companion summon row from /state.
 *
 * A row WITH text is the local ask field: the text is posted straight to the
 * Butler chat and the row acked — the bar never opens and Hermes is never
 * raised. Its `destination` ("new" or the default "recent") picks the chat.
 * A row with `origin === "look"` is the ⌘⇧L door: the same in-place send, but
 * the injected text names the control and app under the cursor first.
 * A row WITHOUT text is the legacy chip/marker click, which keeps opening the
 * bar exactly as before.
 *
 * This runs at module scope because the SummonBar hook is unmounted on hosts
 * that never painted the titlebar slot (the live failure: the row sat for 12 s).
 * Returns true only for an id this module has not consumed yet.
 */
function pollSummonPending() {
  if (typeof rest !== 'function') return Promise.resolve(false)
  return Promise.resolve()
    .then(() => rest('/state'))
    .then(data => {
      const pending = data && data.summon_pending
      const pendingId = asText(pending && pending.id)
      if (!pendingId || pendingId === lastPendingId) return false
      lastPendingId = pendingId
      const text = asText(pending && pending.text)
      if (!text) {
        try {
          openSummonBar('pet')
        } catch (err) {
          // The click still must not keep the row alive.
        }
        ackSummonPending(pendingId)
        return true
      }
      // The ask field chooses per send: only an exact "new" mints a chat.
      const destination = asText(pending && pending.destination) === 'new'
        ? 'new' : 'recent'
      // A LOOK row is a text row with an element attached: the bar never opens,
      // and the injected turn leads with the control the user pointed at.
      const isLook = asText(pending && pending.origin) === 'look'
      const outgoing = isLook ? lookPendingText(pending) : text
      // Send in place. A failed send still acks: the text was typed once, and
      // retrying silently is worse than telling the user exactly why it failed.
      return sendPendingSummonText(outgoing, destination, isLook ? { origin: 'look' } : {})
        .then(result => {
          if (result && result.ok) {
            if (isLook) {
              notifyLookAnswered()
            } else {
              notifyChipSendSuccess(destination)
            }
          } else {
            notifyChipSendFailure(result)
          }
        })
        .catch(err => notifyChipSendFailure({ reason: 'send_failed', error: err }))
        .then(() => {
          ackSummonPending(pendingId)
          return true
        })
    })
    .catch(() => false)
}

/** Watch the summon row even when the SummonBar hook never mounts. */
function startSummonPendingPolling(ctx) {
  const tick = () => { pollSummonPending().catch(() => {}) }
  try {
    if (ctx && typeof ctx.setInterval === 'function') {
      ctx.setInterval(tick, REFRESH_MS)
      return
    }
  } catch (err) {
    // Fall through to the window timer below.
  }
  try {
    summonPendingTimer = window.setInterval(tick, REFRESH_MS)
  } catch (err) {
    // No timer surface: the in-window listener and the shortcut still work.
  }
}

/**
 * Send a summon message to the Butler chat through the SDK composer door.
 * `host.composer.submit(sessionId, text)` is a real programmatic send (as if
 * the user typed + pressed Enter), not a draft seat. The exact session's
 * composer must be mounted, so right after a focus/create we give the app a
 * real mount grace, then retry for six seconds while re-deriving the address
 * from the live session list. If no mounted surface ever claims the send, the
 * text is seated as a draft and the caller gets the exact failure reason.
 */
async function refreshSummonTarget(fallback) {
  const rows = await listSessions()
  const wanted = asText(fallback)
  // Prefer the exact row the retry is addressing: ids are identity, so a
  // renamed chat still answers here.
  if (Array.isArray(rows) && wanted) {
    const row = rows.find(entry => rowTargetIds(entry).includes(wanted))
    if (row) {
      const target = sessionTargetId(row) || wanted
      return { target, ids: uniqueIds(wanted, rowTargetIds(row)) }
    }
  }
  const tracked = trackedButlerChat(rows)
  if (tracked) return { target: tracked.target, ids: tracked.ids }
  const row = mostRecentSession(rows)
  if (!row) return null
  const ids = rowTargetIds(row)
  const target = sessionTargetId(row) || wanted || ids[0]
  if (!target && !ids.length) return null
  return { target, ids }
}

function timingOption(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? number : fallback
}

async function sendSummonText(target, text, options = {}) {
  const targetId = asText(target)
  if (!targetId) {
    return { ok: false, reason: 'no_session', seated: false, error: null, session: null, attempts: 0 }
  }
  if (!host || !host.composer) {
    return { ok: false, reason: 'no_composer', seated: false, error: null, session: targetId, attempts: 0 }
  }

  const now = typeof options.now === 'function' ? options.now : () => Date.now()
  const delay = typeof options.wait === 'function' ? options.wait : wait
  const graceMs = timingOption(options.graceMs, SUMMON_SEND_GRACE_MS)
  const retryMs = timingOption(options.retryMs, SUMMON_SEND_RETRY_MS)
  const timeoutMs = timingOption(options.timeoutMs, SUMMON_SEND_TIMEOUT_MS)
  const refreshMs = timingOption(options.refreshMs, SUMMON_LIVE_REFRESH_MS)
  const canRefresh = options.refresh !== false
  const canSubmit = typeof host.composer.submit === 'function'
  const canDraft = typeof host.composer.setDraft === 'function'
  const candidates = uniqueIds(
    targetId,
    targetId === launcherTargetId ? launcherTargetAliases : []
  )
  let currentTarget = targetId
  let nextRefreshAt = now()
  let lastError = null
  let attempts = 0
  const startedAt = now()
  const firstAttemptAt = startedAt + graceMs
  const deadline = startedAt + timeoutMs

  while (canSubmit) {
    const current = now()
    if (current < firstAttemptAt) {
      if (firstAttemptAt >= deadline) break
      await delay(firstAttemptAt - current)
      continue
    }
    if (current >= deadline) break

    attempts += 1
    for (const id of [...candidates]) {
      try {
        if (host.composer.submit(id, text)) {
          if (targetId === launcherTargetId) launcherTargetId = id
          return { ok: true, send: 'composer.submit', session: id, attempts }
        }
      } catch (err) {
        lastError = err
      }
    }

    if (now() >= deadline) break

    // A stored id can be stale while the mounted surface answers for the
    // runtime id it resumed under. Re-read the live row between attempts.
    if (canRefresh && now() >= nextRefreshAt) {
      const live = await refreshSummonTarget(currentTarget)
      if (live) {
        addTargetIds(candidates, live.ids)
        addTargetIds(candidates, live.target)
        currentTarget = live.target || currentTarget
        if (targetId === launcherTargetId) launcherTargetId = currentTarget
      }
      nextRefreshAt = now() + refreshMs
    }

    const nextAttemptAt = Math.min(deadline, now() + retryMs)
    if (nextAttemptAt <= now()) break
    await delay(nextAttemptAt - now())
  }

  // No submit verb (older host) or no mounted surface ever claimed it. Seat
  // the text as the honest fallback and say exactly why the send failed.
  let reason = canSubmit ? 'no_visible_composer' : 'no_programmatic_send'
  let seated = false
  if (canDraft) {
    let draftThrew = false
    for (const id of candidates) {
      try {
        if (await host.composer.setDraft(id, text)) {
          seated = true
          currentTarget = id
          if (targetId === launcherTargetId) launcherTargetId = id
          break
        }
      } catch (err) {
        draftThrew = true
        lastError = err
      }
    }
    if (draftThrew && !seated) reason = 'draft_failed'
  }
  return {
    ok: false,
    seated,
    reason,
    error: lastError,
    session: currentTarget,
    attempts
  }
}

async function spawnSummonDouble() {
  if (!rest || typeof rest !== 'function') return false
  try {
    const result = await rest('/summon/pet', { method: 'POST' })
    return Boolean(result && result.pet)
  } catch (err) {
    // No companion is a petless/overlay-missing path, not a send failure.
    return false
  }
}

const SUMMON_REASON_LABELS = {
  no_session: 'no session',
  no_composer: 'no composer',
  no_visible_composer: 'no visible composer',
  no_programmatic_send: 'no_programmatic_send',
  draft_failed: 'draft_failed',
  injection_denied: 'gateway injection denied',
  injection_unavailable: 'gateway injection unavailable',
  unroutable_session: 'session not routable through the gateway',
  session_create_failed: 'could not create a fresh Butler session',
  session_create_unavailable: 'session creation unsupported',
  session_create_reused: 'the gateway reused an existing Butler session',
  session_key_mismatch: 'the gateway delivered to the wrong session',
  session_focus_failed: 'could not focus the Butler session'
}

function summonErrorText(error) {
  if (!error) return ''
  if (typeof error === 'string') return asText(error) || ''
  return asText(error.message) || asText(error) || ''
}

/** Exact, human-readable failure toast for the bar (never a dead-silent send). */
function summonFailureMessage(result) {
  const reason = asText(result && result.reason) || 'failed'
  const label = SUMMON_REASON_LABELS[reason] || reason
  let message = `Could not summon: ${label}`
  if (label !== reason) message += ` (${reason})`
  const error = summonErrorText(result && result.error) || summonErrorText(result && result.injection_error)
  if (error) message += ` — ${error}`
  const detail = asText(result && result.detail)
  if (detail && detail !== error) message += ` — ${detail}`
  const expectedKey = asText(result && result.expected_session_key)
  const actualKey = asText(result && result.session_key)
  if (expectedKey && !detail) {
    message += ` — gateway session key ${actualKey || 'unknown'}; expected ${expectedKey}`
  }
  const injectionReason = asText(result && result.injection_reason)
  if (injectionReason) {
    message += `. Full-conversation injection was unavailable (${injectionReason}); /butler fallback also failed`
  }
  if (result && result.seated) {
    message += '. Draft seat: seated. Press Enter in the Butler composer to send it.'
  } else {
    message += '. Draft seat: not seated.'
  }
  return message
}

/**
 * Primary send: ask the gateway to inject a real agent turn into the exact
 * session the desktop resolved. Fail-closed contract is
 * `{ok: false, reason: 'injection_denied'}`; any transport failure reads as
 * `injection_unavailable` so the caller can fall back honestly.
 */
async function sendSummonInjection(target, text) {
  const targetId = asText(target)
  if (!targetId) {
    return { ok: false, reason: 'no_session', error: null, session: null }
  }
  if (!rest || typeof rest !== 'function') {
    return { ok: false, reason: 'injection_unavailable', error: null, session: targetId }
  }
  try {
    const response = await rest('/summon/send', {
      method: 'POST',
      body: { text, session_id: targetId }
    })
    if (response && response.ok === true) {
      return {
        ok: true,
        send: 'inject',
        session: asText(response.session) || targetId,
        session_key: asText(response.session_key) || null,
        attempts: 1
      }
    }
    return {
      ok: false,
      reason: asText(response && response.reason) || 'injection_denied',
      error: response && response.error,
      detail: response && response.detail,
      session: targetId
    }
  } catch (err) {
    return { ok: false, reason: 'injection_unavailable', error: err, session: targetId }
  }
}

/**
 * New destination: create the session first, let the focus settle (openSession
 * in-place plus the same 300 ms mount grace the composer path used), then keep
 * the fresh stored/runtime ids. Never falls back to the oldest Butler chat.
 */
async function openFreshButlerSession(options = {}) {
  if (launcherBusy) return launcherState(false, 'busy')
  launcherBusy = true
  launcherTargetId = null
  launcherTargetAliases = []
  try {
    // The old titled chats are only renamed here: their ids stay untouched,
    // and the fresh send below is addressed by the id create returns.
    const previous = await listButlerSessions()
    const created = await createButlerSession(previous)
    if (!created || !created.ok) return created || launcherState(false, 'session_create_failed')
    const delay = typeof options.wait === 'function' ? options.wait : wait
    await delay(timingOption(options.graceMs, SUMMON_SEND_GRACE_MS))
    return created
  } catch (err) {
    return launcherState(false, 'session_create_failed', err)
  } finally {
    launcherBusy = false
  }
}

/** Resolve the chosen Butler chat, inject a real agent turn, then own the visual. */
async function summonButler(text, options = {}) {
  const message = asText(text)
  if (!message) return { ok: false, reason: 'empty', destination: SUMMON_EXISTING }
  const destination = options.destination === SUMMON_NEW
    || (options.destination === undefined && getSummonDestination() === SUMMON_NEW)
    ? SUMMON_NEW : SUMMON_EXISTING
  const opened = destination === SUMMON_NEW
    ? await openFreshButlerSession(options)
    : await openButlerSession()
  if (!opened || !opened.ok) {
    return {
      ok: false,
      reason: (opened && opened.reason) || 'no_session',
      error: opened && opened.error,
      destination,
      phase: 'session'
    }
  }
  const target = asText(opened.target) || asText(launcherTargetId)
  if (!target) return { ok: false, reason: 'no_session', destination, phase: 'session' }

  const injection = await sendSummonInjection(target, message)
  if (injection.ok) {
    if (destination === SUMMON_NEW) {
      // The fresh id is the whole point of this destination. Round 5 proved the
      // gateway reports the routing key; verify it names THIS id, not the old
      // chat the titled-create bug used to resolve.
      const expectedKey = expectedSummonSessionKey(hostProfileName(), target)
      const actualKey = asText(injection.session_key)
      if (expectedKey && actualKey && actualKey !== expectedKey) {
        return {
          ok: false,
          reason: 'session_key_mismatch',
          error: null,
          detail: `gateway routed to ${actualKey}; expected ${expectedKey}`,
          session: target,
          session_key: actualKey,
          expected_session_key: expectedKey,
          destination,
          phase: 'send'
        }
      }
    }
    return { ...injection, destination }
  }

  // The gateway could not host the turn. Preserve the grounded chip ladder:
  // composer.submit('/butler <text>') reaches the slash handler, which reports
  // honestly instead of opening a full conversation. A fresh send must never
  // re-derive the most recent Butler chat during this fallback.
  const fallback = await sendSummonText(target, butlerCommandText(message), {
    ...options,
    refresh: options.refresh !== undefined ? options.refresh : destination !== SUMMON_NEW
  })
  return {
    ...fallback,
    session: fallback.session || target,
    destination,
    fallback: true,
    injection_reason: injection.reason,
    injection_error: injection.error,
    injection_detail: injection.detail
  }
}

/**
 * Bar-facing submit outcome: owns the success toast/double and the honest
 * failure toast. On failure the caller keeps the typed text in the input.
 */
async function summonFromBar(value, options = {}) {
  let result
  try {
    result = await summonButler(value, options)
  } catch (err) {
    result = { ok: false, reason: 'threw', error: err, seated: false, destination: getSummonDestination() }
  }
  if (result.ok) {
    notifySummonSuccess(result)
    await spawnSummonDouble()
    return { result, clearText: true, dismiss: true }
  }
  notifySummonFailure(summonFailureMessage(result))
  return { result, clearText: false, dismiss: false }
}

function LauncherButton({ compact = false }) {
  const onClick = () => { openButlerSession() }
  if (!compact) {
    return h(Button, { variant: 'secondary', size: 'sm', onClick }, 'Butler')
  }
  return h('button', {
    type: 'button',
    title: 'Open or focus the Butler chat',
    onClick,
    style: {
      cursor: 'pointer', fontSize: 10.5, lineHeight: 1.4,
      color: '#c7cbd2', background: 'rgba(255,255,255,.04)',
      border: '1px solid rgba(255,255,255,.10)',
      borderRadius: 5, padding: '1px 7px'
    }
  }, 'Butler')
}

// ---------------------------------------------------------------- summon bar

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max)
}

/**
 * Floating "Ask the butler" bar. It is mounted once by the permanent titlebar
 * slot and only paints while the shortcut / pet click has opened it.
 */
function SummonBar() {
  const [open, setOpen] = useState(false)
  const [origin, setOrigin] = useState('shortcut')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [position, setPosition] = useState(null)
  const [destination, setDestination] = useState(() => getSummonDestination())
  const inputRef = useRef(null)
  const barRef = useRef(null)
  const busyRef = useRef(false)
  busyRef.current = busy

  // The companion chip cannot touch the desktop renderer directly; the engine
  // publishes a one-shot summon_pending row in /state instead. The bar is the
  // always-mounted surface, so it consumes that row and opens the SAME picker.
  const pendingQuery = useQuery({
    queryKey: ['butler', 'summon-pending'],
    queryFn: () => (typeof rest === 'function' ? rest('/state') : Promise.resolve({})),
    refetchInterval: REFRESH_MS,
    refetchOnMount: 'always'
  }) || {}
  const pending = pendingQuery.data && pendingQuery.data.summon_pending
  const pendingId = asText(pending && pending.id)
  const pendingText = asText(pending && pending.text)
  const seenPendingRef = useRef(null)
  useEffect(() => {
    if (!pendingId || seenPendingRef.current === pendingId) return
    // A text row is the in-place ask field. The module poll sends it and acks;
    // opening the bar (or acking here) would both violate "no bar" and race the
    // send away.
    if (pendingText) return
    seenPendingRef.current = pendingId
    openSummonBar('pet')
    if (typeof rest !== 'function') return
    try {
      const ack = rest('/summon/ack', { method: 'POST', body: { id: pendingId } })
      if (ack && typeof ack.catch === 'function') ack.catch(() => {})
    } catch (err) {
      // The row stays unconsumed; a later poll retries the ack.
    }
  }, [pendingId, pendingText])

  useEffect(() => subscribeSummonBar(state => {
    setOpen(state.open)
    setOrigin(state.origin)
  }), [])

  useEffect(() => {
    if (!open) return undefined
    setText('')
    // Re-read the persisted choice on every opening. Focus stays on the text
    // input; the destination buttons are Tab-reachable after it.
    setDestination(getSummonDestination())
    const id = window.setTimeout(() => {
      try { inputRef.current && inputRef.current.focus() } catch (err) { }
    }, 0)
    return () => window.clearTimeout(id)
  }, [open])

  // Follow the pet while the bar is open; otherwise park upper-right. The
  // same read-only locator the pet-click listener uses.
  useEffect(() => {
    if (!open) return undefined
    let frame = null
    let last = ''
    const follow = () => {
      const bar = barRef.current
      const width = bar && bar.offsetWidth ? bar.offsetWidth : 360
      const height = bar && bar.offsetHeight ? bar.offsetHeight : 48
      let left = Math.max(12, window.innerWidth - width - 16)
      let top = 16
      const canvas = petCanvasNode()
      if (canvas) {
        try {
          const rect = canvas.getBoundingClientRect()
          if (rect && rect.width > 0 && rect.height > 0) {
            left = rect.left - width - 14
            if (left < 12) left = rect.right + 14
            left = clamp(left, 12, Math.max(12, window.innerWidth - width - 12))
            top = clamp(rect.top + rect.height / 2 - height / 2,
              12, Math.max(12, window.innerHeight - height - 12))
          }
        } catch (err) {
          // Keep the upper-right seat when the canvas is unreadable.
        }
      }
      const key = `${Math.round(left)}:${Math.round(top)}`
      if (key !== last) {
        last = key
        setPosition({ left: Math.round(left), top: Math.round(top) })
      }
      frame = window.requestAnimationFrame(follow)
    }
    follow()
    return () => { if (frame !== null) window.cancelAnimationFrame(frame) }
  }, [open])

  const close = () => {
    if (busyRef.current) return
    dismissSummonBar()
  }

  const chooseDestination = next => {
    const value = setSummonDestination(next)
    setDestination(value)
  }

  // Blur inside the bar (including Tab onto the destination buttons) keeps it
  // open; only leaving the bar dismisses it. The zero-delay re-check gives a
  // click on a picker button a chance to take focus first.
  const onBarBlur = event => {
    if (busyRef.current) return
    const next = event && event.relatedTarget
    const bar = barRef.current
    if (next && bar && typeof bar.contains === 'function' && bar.contains(next)) return
    window.setTimeout(() => {
      if (busyRef.current) return
      const active = typeof document === 'undefined' ? null : document.activeElement
      if (active && bar && typeof bar.contains === 'function' && bar.contains(active)) return
      dismissSummonBar()
    }, 0)
  }

  const submit = async () => {
    if (busyRef.current) return
    const value = text.trim()
    if (!value) return
    setBusy(true)
    const outcome = await summonFromBar(value, { destination })
    setBusy(false)
    // Failure keeps the typed text in the bar so the user can retry; only a
    // real send clears and dismisses it. The toast names the exact reason.
    if (outcome.clearText) setText('')
    if (outcome.dismiss) dismissSummonBar()
  }

  if (!open) return null

  const placement = position || { left: undefined, top: 16 }
  const destinationButton = (value, label) => h('button', {
    type: 'button',
    disabled: busy,
    'aria-pressed': destination === value,
    onClick: () => chooseDestination(value),
    style: {
      cursor: busy ? 'default' : 'pointer', fontSize: 11, lineHeight: 1.3,
      color: destination === value ? '#eceef1' : '#9aa3ad',
      background: destination === value ? 'rgba(122,134,255,.26)' : 'transparent',
      border: 'none', borderRadius: 6, padding: '4px 9px',
      outline: 'none'
    }
  }, label)

  return h('div', {
    ref: barRef,
    role: 'dialog',
    'aria-label': 'Ask the butler',
    onBlur: onBarBlur,
    onKeyDown: event => {
      if (event.key === 'Escape') {
        event.preventDefault()
        if (!busyRef.current) dismissSummonBar()
      }
    },
    style: {
      position: 'fixed', left: placement.left === undefined ? undefined : placement.left,
      top: placement.top, right: placement.left === undefined ? 16 : undefined,
      width: 380, maxWidth: 'calc(100vw - 24px)',
      zIndex: 9999, display: 'flex', flexDirection: 'column', gap: 7,
      padding: '10px 12px', borderRadius: 12,
      background: 'var(--ui-bg-elevated, #1f2126)',
      border: '1px solid var(--ui-border, rgba(255,255,255,.14))',
      boxShadow: '0 12px 34px rgba(0,0,0,.38)'
    }
  },
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 7 } },
      h('span', { style: { fontSize: 13, fontWeight: 600, color: '#eceef1' } }, 'Ask the butler'),
      h('span', { style: { flex: 1 } }),
      h('span', { style: { fontSize: 10, color: '#8a8f98' } },
        busy ? 'Sending…' : (origin === 'pet' ? 'from your pet' : 'Enter to send · Esc to dismiss'))
    ),
    h('input', {
      ref: inputRef,
      type: 'text',
      value: text,
      placeholder: SUMMON_PLACEHOLDER,
      disabled: busy,
      'aria-label': 'Ask the butler for a guide',
      onChange: event => setText(event.target.value),
      onKeyDown: event => {
        if (event.key === 'Escape') {
          event.preventDefault()
          if (!busyRef.current) dismissSummonBar()
          return
        }
        if (event.key === 'Enter') {
          event.preventDefault()
          submit()
        }
      },
      style: {
        width: '100%', boxSizing: 'border-box', fontSize: 13,
        color: '#eceef1', background: 'rgba(255,255,255,.06)',
        border: '1px solid rgba(255,255,255,.12)', borderRadius: 8,
        padding: '8px 10px', outline: 'none'
      }
    }),
    h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
      h('span', { style: { fontSize: 10.5, color: '#8a8f98' } }, 'Send to'),
      h('div', {
        role: 'group',
        'aria-label': 'Summon destination',
        style: {
          display: 'inline-flex', padding: 2, borderRadius: 8, gap: 2,
          background: 'rgba(255,255,255,.05)',
          border: '1px solid rgba(255,255,255,.09)'
        }
      },
        destinationButton(SUMMON_EXISTING, 'Butler chat'),
        destinationButton(SUMMON_NEW, 'New session')
      )
    )
  )
}

function ButlerChipStatus() {
  const configQuery = useQuery({
    queryKey: ['butler', 'config'],
    queryFn: () => (typeof rest === 'function' ? rest('/config') : Promise.resolve({})),
    refetchInterval: CONFIG_REFRESH_MS,
    refetchOnMount: 'always'
  }) || {}
  // Installed means on unless the stored escape hatch says false.
  const configured = configPetClickClaim(configQuery.data)
  // Re-evaluated on a fixed 2 s clock while the pane is mounted, whether or
  // not the chip is on: "not found" is the honest answer when the pet is
  // popped out into its own OS window.
  const [petFound, setPetFound] = useState(() => Boolean(petCanvasNode()))
  const [flash, setFlash] = useState(false)
  const flashTimerRef = useRef(null)
  const lastClickSeqRef = useRef(null)
  const liveRef = useRef({ claim: true, petFound: false })
  liveRef.current = { claim: configured, petFound }

  useEffect(() => {
    // A null initial query must not disarm the register-time watcher: only a
    // real config response (an explicit false) can turn the chip off.
    if (configQuery.data === null || configQuery.data === undefined) return
    setPetClickClaim(configured)
  }, [configured, configQuery.data])

  useEffect(() => {
    const check = () => setPetFound(Boolean(petCanvasNode()))
    check()
    const id = window.setInterval(check, PET_STATUS_REFRESH_MS)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => subscribeSummonBar(state => {
    const seq = Number(state && state.petClickSeq) || 0
    if (lastClickSeqRef.current === null) {
      lastClickSeqRef.current = seq
      return
    }
    if (seq === lastClickSeqRef.current) return
    lastClickSeqRef.current = seq
    // Only a claimed in-app click gets the confirmation: a companion-chip open
    // with no in-app pet must not pretend the row saw something.
    if (!liveRef.current.claim || !liveRef.current.petFound) return
    setFlash(true)
    if (flashTimerRef.current !== null) window.clearTimeout(flashTimerRef.current)
    flashTimerRef.current = window.setTimeout(() => {
      flashTimerRef.current = null
      setFlash(false)
    }, SUMMON_FLASH_MS)
  }), [])

  useEffect(() => () => {
    if (flashTimerRef.current !== null) {
      window.clearTimeout(flashTimerRef.current)
      flashTimerRef.current = null
    }
  }, [])

  const claimedAndFound = configured && petFound
  const rowText = petStatusRowText({ claim: configured, petFound, flash })
  const rowColor = flash ? '#f5c26b' : (claimedAndFound ? '#6fd39a' : '#8a8f98')
  const rowBorder = flash ? 'rgba(245,194,107,.4)'
    : (claimedAndFound ? 'rgba(111,211,154,.35)' : 'rgba(255,255,255,.09)')
  const rowBackground = flash ? 'rgba(245,194,107,.10)'
    : (claimedAndFound ? 'rgba(111,211,154,.08)' : 'rgba(255,255,255,.03)')

  return h('div', {
    role: 'status',
    'aria-live': 'polite',
    style: {
      alignSelf: 'flex-start', fontSize: 10.5, lineHeight: 1.4,
      padding: '2px 8px', borderRadius: 99,
      color: rowColor, border: `1px solid ${rowBorder}`,
      background: rowBackground
    }
  }, rowText)
}

// ---------------------------------------------------------------- pet speech

/**
 * The pet heartbeat speaks STATUS lines (done / stalled / capture failed)
 * AND, when the user asked us to step says back on, the CURRENT instruction
 * line — "my pet never said anything" was a regression from the says split,
 * so instructions are spoken again. The overlay bubble still shows the same
 * step; that tutorial-ish duplication is accepted (their words: it's a
 * tutorial, saying only once in one place is harder to notice).
 */
function petStatusLine(cur) {
  const status = asText(cur && cur.status)
  const note = asText(cur && cur.note)
  const instruction = asText(cur && cur.instruction)
  const watchLine = watchStatusLine(cur && cur.watch)
  // Precedence, in one place (round 19): a live watch beats every DEAD guide
  // block. `stalled` is only live while no watch is running; done / stopped /
  // abandoned / superseded are never live. Without this, a stale stall note
  // from a guide stopped long ago masks the watch line forever.
  if (status === 'running' && instruction) {
    // 1. A live step the user is being walked through.
    return instruction
  }
  if (status === 'capture_failed') {
    // A live guide that cannot read the screen: keep this above the watch (the
    // guide cannot verify anything until Accessibility comes back).
    return note && note !== instruction
      ? `Capture failed: ${note}`
      : 'Capture failed — butler could not read the screen'
  }
  if (watchLine) {
    // 2. The ambient watch the pet carries while the user is in another app.
    return watchLine
  }
  if (status === 'done') {
    // 3. Terminal guide lines: spoken once by the beat's dedupe.
    const total = asNumber(cur.total)
    const verified = asNumber(cur.verified)
    return `Guide complete · ${verified} of ${total} verified`
  }
  if (status === 'stopped') {
    return note && note !== instruction ? `Butler stopped: ${note}` : 'Butler stopped'
  }
  if (status === 'stalled') {
    // 4. Only when no watch is live (a live watch returned above).
    return note || `Butler stalled on step ${asNumber(cur.currentStep) + 1}`
  }
  // 5. Idle: abandon/superseded/unknown blocks are dead weight and say nothing.
  return null
}

/**
 * The ambient WATCH line the pet carries while a watch is active.
 *
 * WATCH is passive and runs while the user is in another app, so its only
 * surface is the shared pet speech beat: the change count while observing, and
 * the hit label once the condition fired. No new window and no new channel.
 * A stopped watch (app quit, or the user pressed Stop) drops the live line;
 * `watchEndedLine` then says the one ending line and the pane row clears.
 */
function watchStatusLine(watch) {
  const row = watch && typeof watch === 'object' ? watch : null
  // `stopped` is an object in the state row ({"reason","events","at"}), never a
  // string; test its shape, not its stringification ("[object Object]").
  const stopped = row && row.stopped && typeof row.stopped === 'object' ? row.stopped : null
  if (!row || stopped) return null
  const hit = row.hit && typeof row.hit === 'object' ? row.hit : null
  const hitLabel = asText(hit && hit.label)
  if (hitLabel) return `⏲ Watch · hit: ${hitLabel}`
  const app = asText(row.app)
  if (!app) return null
  const changes = asNumber(row.events)
  return `⏲ Watch · ${app} · ${changes} ${changes === 1 ? 'change' : 'changes'}`
}

/** True while `watch` is a LIVE row: an app, and no `stopped` marker. */
function watchIsActive(watch) {
  const row = watch && typeof watch === 'object' ? watch : null
  if (!row) return false
  // `stopped` is an object in the state row ({"reason","events","at"}), never a
  // string; test its truthiness either way.
  if (row.stopped) return false
  return Boolean(asText(row.app))
}

/** One line for a watch that ended: `app-quit` reads as "the app quit". */
function watchEndedLine(watch) {
  const row = watch && typeof watch === 'object' ? watch : null
  const stopped = row && row.stopped && typeof row.stopped === 'object' ? row.stopped : null
  if (!stopped) return null
  const reason = asText(stopped.reason) || 'stopped'
  return `Watch ended — ${reason === 'app-quit' ? 'the app quit' : reason}`
}

/** `mm:ss` since the watch was armed; `00:00` when the start time is unreadable. */
function watchElapsedLabel(watch, now = Date.now()) {
  const started = timestampValue(watch && (watch.started_at ?? watch.since))
  const seconds = started > 0 ? Math.max(0, Math.floor(now / 1000 - started)) : 0
  const mm = String(Math.floor(seconds / 60)).padStart(2, '0')
  const ss = String(seconds % 60).padStart(2, '0')
  return `${mm}:${ss}`
}

/** The pane's watch row text: app, change count, elapsed time. */
function watchPaneText(watch, now = Date.now()) {
  const app = asText(watch && watch.app) || '?'
  const changes = asNumber(watch && watch.events)
  return `⏲ Watching ${app} · ${changes} changes · ${watchElapsedLabel(watch, now)}`
}

/** The pane's Stop button path: the same REST door every other action uses. */
function stopWatch() {
  if (typeof rest !== 'function') return Promise.resolve(false)
  try {
    const pending = rest('/watch/stop', { method: 'POST' })
    if (pending && typeof pending.catch === 'function') {
      return pending.then(result => Boolean(result && result.ok)).catch(() => false)
    }
  } catch (err) {
    return Promise.resolve(false)
  }
  return Promise.resolve(false)
}

/**
 * The one persistent mark a watch leaves in the pane: a row with the app, the
 * change count, elapsed time, and a Stop button. Rendered only while the state
 * carries a live watch row; a stopped one is an ending, not a status.
 */
function WatchRow({ watch }) {
  const live = watchIsActive(watch) ? watch : null
  if (!live) return null
  return h('div', {
    role: 'status',
    style: {
      display: 'flex', alignItems: 'center', gap: 8, alignSelf: 'flex-start',
      fontSize: 11.5, lineHeight: 1.4, padding: '3px 8px', borderRadius: 8,
      color: '#c7cbd2', background: 'rgba(255,255,255,.03)',
      border: '1px solid rgba(255,255,255,.09)'
    }
  },
    h('span', {}, watchPaneText(live)),
    h('button', {
      type: 'button',
      onClick: () => { stopWatch() },
      style: {
        cursor: 'pointer', fontSize: 10.5, color: '#9aa3ea',
        background: 'transparent', border: '1px solid rgba(122,134,255,.35)',
        borderRadius: 6, padding: '1px 8px'
      }
    }, 'Stop')
  )
}

/**
 * Say ONE "Watch ended — <reason>" line per ended watch, then clear the row.
 *
 * Deduped by the state's own identity (app + stopped.at + reason) at module
 * scope, so the pane's beat and the module beat cannot double-speak it. The row
 * is cleared through the same `/watch/stop` door the pane's Stop button uses;
 * the pane already hides a stopped row, so the two agree.
 * Returns true only when this call spoke.
 */
function announceWatchEnded(cur) {
  const watch = cur && cur.watch && typeof cur.watch === 'object' ? cur.watch : null
  const line = watchEndedLine(watch)
  if (!line) return false
  const stopped = watch.stopped
  const key = `${asText(watch.app) || ''}|${asText(stopped.at) || ''}|${asText(stopped.reason) || ''}`
  if (key === lastWatchEndedKey) return false
  // Mark the ending as spoken only once the line actually went out. A say that
  // throws must not burn the dedupe key: the next beat retries the one line
  // instead of losing it forever. A host with no pet channel burns the key on
  // purpose: there is nothing to speak the line with, and retrying every 2 s
  // would never change that.
  if (petCtx && typeof petCtx.say === 'function') {
    try {
      petCtx.say(line, { tone: 'info', ttlMs: WATCH_ENDED_TTL_MS, id: 'butler-watch-ended' })
    } catch (err) {
      return false
    }
  }
  lastWatchEndedKey = key
  petSayTicks += 1
  petLastTick = new Date().toLocaleTimeString()
  lastSaid = null
  lastSaidAt = 0
  stopWatch()
  return true
}

/**
 * Speak the agent's short answer on the pet exactly once per id.
 *
 * /state merges say.json as `say`; the desktop speaks it with the answer ttl and
 * remembers the id. Returns true only when this call actually spoke, so the
 * guide status line can yield the one shared channel for the answer's ttl.
 */
function speakButlerAnswer(say) {
  const row = say && typeof say === 'object' ? say : {}
  const id = asText(row.id)
  const text = asText(row.text)
  if (!id || !text) return false
  if (id === lastSpokenSayId) return false
  try {
    if (!petCtx || typeof petCtx.say !== 'function') return false
    petCtx.say(text, { tone: 'info', ttlMs: 15000, id: 'butler-answer' })
  } catch (err) {
    return false
  }
  petSayTicks += 1
  petLastTick = new Date().toLocaleTimeString()
  lastSpokenSayId = id
  return true
}

/**
 * One pet-speech beat on the 2 s clock. Returns which line won: `answer`,
 * `holding` (the answer is still within its ttl), `watch-ended`, `status`,
 * `repeat`, or `idle`.
 *
 * A spoken answer resets `lastSaid`, so the status line speaks again (instead of
 * being deduped away) once the answer's ttl lapses. One channel, last call wins,
 * no queueing. A live watch line is re-said every WATCH_KEEPALIVE_MS; every
 * other status line still speaks once.
 */
/**
 * The slice of /state the pet speech beat needs, built the same way the pane
 * builds it so the two speakers cannot drift.
 */
function speechSnapshot(data) {
  const rawGuide = data && data.guide
  const guide = rawGuide && typeof rawGuide === 'object' && Object.keys(rawGuide).length > 0 ? rawGuide : null
  const state = guide && guide.state && typeof guide.state === 'object' ? guide.state : {}
  const steps = asArray(guide && guide.steps)
  const verifications = asArray(guide && guide.verifications)
  const rawIdx = Number(state.idx)
  const idx = Number.isInteger(rawIdx) && rawIdx >= 0 ? rawIdx : 0
  const currentStep = steps.length > 0 ? Math.min(idx, steps.length - 1) : 0
  const rawAnswer = data && data.say
  const rawWatch = data && data.watch
  return {
    status: asText(state.status) || asText(guide && guide.status) || 'running',
    note: asText(state.note),
    instruction: asText(steps[currentStep] && steps[currentStep].say) || asText(state.say),
    currentStep,
    total: steps.length,
    verified: verifications.length,
    say: rawAnswer && typeof rawAnswer === 'object' && Object.keys(rawAnswer).length > 0 ? rawAnswer : null,
    watch: rawWatch && typeof rawWatch === 'object' && Object.keys(rawWatch).length > 0 ? rawWatch : null
  }
}

/**
 * Speak on the pet from module scope, NOT from the pane.
 *
 * The beat used to live in ButlerApp's effect, so the pet only spoke while the
 * butler pane was open — and `butler_say` answers written with the pane closed
 * (a watch hit, a LOOK answer) were never spoken at all. Same failure class as
 * the summon-pending hook: a consumer inside a component that may not be
 * mounted. Dedupe ids are module-scope, so the pane's own beat and this one
 * cannot double-speak the same line.
 */
let petSpeechTimer = null
function startPetSpeechPolling(ctx) {
  if (petSpeechTimer !== null) return
  const tick = () => {
    try {
      const pending = typeof rest === 'function' ? rest('/state') : Promise.resolve({})
      pending.then(data => { petSpeechBeat(speechSnapshot(data)) }).catch(() => { })
    } catch (err) { }
  }
  tick()
  try {
    petSpeechTimer = ctx && typeof ctx.setInterval === 'function'
      ? ctx.setInterval(tick, PET_STATUS_REFRESH_MS)
      : window.setInterval(tick, PET_STATUS_REFRESH_MS)
  } catch (err) { petSpeechTimer = null }
}

function petSpeechBeat(cur, now = Date.now()) {
  if (speakButlerAnswer(cur && cur.say)) {
    lastSaid = null
    // The answer owns the channel for 15 s; then the status line resumes.
    answerUntil = now + 15000
    return 'answer'
  }
  if (now < answerUntil) return 'holding'
  if (announceWatchEnded(cur)) return 'watch-ended'
  const line = petStatusLine(cur)
  if (!line) {
    try { petCtx && petCtx.clear && petCtx.clear('butler-say') } catch (err) { }
    lastSaid = null
    return 'idle'
  }
  // Is this line the ambient watch count? Only then does it get a keep-alive;
  // every other status line still speaks once (a guide instruction is already
  // carried by the ring and the pane). Compare the WINNING line to the watch
  // line instead of testing the instruction: a dead guide may still carry a
  // stale step while the watch line is the one the pet is speaking.
  const watchLine = watchIsActive(cur && cur.watch) ? watchStatusLine(cur.watch) : null
  const watching = watchLine && line === watchLine ? line : null
  if (lastSaid === line) {
    const due = watching && line === watching && now - lastSaidAt >= WATCH_KEEPALIVE_MS
    if (!due) return 'repeat'
  }
  // The host clamps a ttl to 30 s: the watch line asks for the clamp so the
  // 25 s re-say lands before the previous line expires.
  const ttlMs = watching && line === watching ? WATCH_TTL_MS : 9000
  try {
    petCtx && typeof petCtx.say === 'function'
      && petCtx.say(line, { tone: 'info', ttlMs, id: 'butler-say' })
  } catch (err) { }
  petSayTicks += 1
  petLastTick = new Date().toLocaleTimeString()
  lastSaid = line
  lastSaidAt = now
  return 'status'
}

function ButlerApp() {
  const query = useQuery({
    queryKey: ['butler', 'state'],
    queryFn: () => (typeof rest === 'function' ? rest('/state') : Promise.resolve({})),
    refetchInterval: REFRESH_MS,
    refetchOnMount: 'always'
  }) || {}
  const { data, error, isFetching, refetch } = query

  const historyQuery = useQuery({
    queryKey: ['butler', 'history'],
    queryFn: () => (typeof rest === 'function' ? rest('/history') : Promise.resolve([])),
    refetchInterval: REFRESH_MS,
    refetchOnMount: 'always'
  }) || {}

  const rawGuide = data && data.guide
  const guide = rawGuide && typeof rawGuide === 'object' && Object.keys(rawGuide).length > 0 ? rawGuide : null
  const state = guide && guide.state && typeof guide.state === 'object' ? guide.state : {}
  const steps = asArray(guide && guide.steps)
  const verifications = asArray(guide && guide.verifications)
  const rawIdx = Number(state.idx)
  const idx = Number.isInteger(rawIdx) && rawIdx >= 0 ? rawIdx : 0
  const currentStep = steps.length > 0 ? Math.min(idx, steps.length - 1) : 0
  const step = steps[currentStep] || null
  const status = asText(state.status) || asText(guide && guide.status) || 'running'
  const stalled = status === 'stalled'
  const overlay = asText(guide && guide.overlay)
  const session = asText(guide && guide.session_id)
  const currentLabel = asText(guide && guide.current_label)
  const sayLine = asText(step && step.say) || asText(state.say)
  // The agent's on-screen answer (butler_say) arrives as a top-level `say` row.
  const rawAnswer = data && data.say
  const answer = rawAnswer && typeof rawAnswer === 'object' && Object.keys(rawAnswer).length > 0
    ? rawAnswer
    : null

  // Pet speech is evaluated on its own 2 s clock so a stale React feed cannot
  // pin the last line on screen. `say` is the agent's on-screen answer; the
  // beat speaks it once, ahead of the guide status line, for its ttl.
  // The ambient watch row (if any) rides the same snapshot; the pet beat
  // speaks its change count / hit label, nothing else.
  const rawWatch = data && data.watch
  const watch = rawWatch && typeof rawWatch === 'object' && Object.keys(rawWatch).length > 0
    ? rawWatch
    : null
  // A stopped watch is an ending, not a live row: the speech beat says its one
  // line and stops it, and the pane drops the row immediately.
  const liveWatch = watchIsActive(watch) ? watch : null
  const stateRef = useRef({ status: null, note: null, instruction: null, currentStep: 0, total: 0, verified: 0, say: null, watch: null })
  stateRef.current = {
    status,
    note: asText(state.note),
    instruction: sayLine,
    currentStep,
    total: steps.length,
    verified: verifications.length,
    say: answer,
    watch
  }
  useEffect(() => {
    const beat = () => { petSpeechBeat(stateRef.current) }
    beat()
    const id = window.setInterval(beat, PET_STATUS_REFRESH_MS)
    return () => window.clearInterval(id)
  }, [])
  void speechSnapshot

  const stallNote = asText(state.note) || (stalled
    ? `Step ${currentStep + 1} is still unverified. The butler is waiting on the OS layer; ask the agent to revise or skip this step.`
    : overlay && overlay.startsWith('unavailable')
      ? `overlay: ${overlay} — rings stay in-app`
      : null)
  const progressLine = steps.length === 0 ? null : status === 'done'
    ? `Guide complete · ${verifications.length} of ${steps.length} verified`
    : `Step ${currentStep + 1} of ${steps.length} · ${verifications.length > 0
      ? `${verifications.length} verified`
      : 'unverified'}`
  const petSmokeLine = `pet.say smoke: ${petSayTicks} says fired (last ${petLastTick || 'never'})${petCtx ? '' : ' — ctx.pet MISSING from this app build'}`

  const finishedRows = asArray(historyQuery.data).map(historyRow)
  if (guide && (status === 'done' || status === 'stopped')) {
    const current = historyRow(guide)
    if (!finishedRows.some(row => row.id === current.id)) finishedRows.push(current)
  }

  const live = Boolean(guide) && ['running', 'stalled', 'capture_failed'].includes(status)

  return h('div', {
    className: 'hermes-p-4',
    style: { display: 'flex', flexDirection: 'column', gap: 12, minHeight: 220 }
  },
    h('div', { className: 'flex items-center gap-3' },
      h('div', {
        style: {
          width: 30, height: 30, borderRadius: 9, flex: 'none',
          background: 'linear-gradient(135deg,#5e6ad2,#4338ca)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 15
        }
      }, '🎩'),
      h('div', { style: { flex: 1 } },
        h('div', { style: { fontSize: 15, fontWeight: 600, color: '#eceef1' } }, 'Butler'),
        h('div', { style: { fontSize: 11.5, color: '#8a8f98', marginTop: 1 } },
          'Follow-mode guide state · verified from the OS layer')),
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } },
        h(LauncherButton, {}),
        h(Button, {
          variant: 'secondary',
          size: 'sm',
          onClick: () => { if (typeof refetch === 'function') refetch() }
        }, 'Refresh'))
    ),

    h(ButlerChipStatus, {}),

    liveWatch && h(WatchRow, { watch: liveWatch }),

    error && h('div', {
      style: { fontSize: 12, color: '#f08080', border: '1px solid rgba(240,128,128,.3)', borderRadius: 10, padding: '9px 12px' }
    }, 'Butler backend not reachable: install and enable the butler plugin, then reload desktop plugins (⌘K → Reload desktop plugins).'),

    !error && live && h('div', { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' } },
        h('span', { style: { fontSize: 13.5, fontWeight: 600, color: '#eceef1' } },
          asText(guide.title) || 'Butler guide'),
        h('span', {
          style: {
            fontSize: 10, padding: '2px 9px', borderRadius: 99, letterSpacing: .5,
            border: '1px solid rgba(122,134,255,.4)', color: '#9aa3ea', background: 'rgba(122,134,255,.10)'
          }
        }, session ? `session ${session.slice(0, 6)}` : 'session chat'),
        h('span', {
          style: {
            fontSize: 10, padding: '2px 9px', borderRadius: 99,
            border: `1px solid ${stalled ? 'rgba(245,194,107,.4)' : 'rgba(255,255,255,.09)'}`,
            color: stalled ? '#f5c26b' : '#8a8f98'
          }
        }, status),
        isFetching && h(Codicon, { name: 'sync', className: 'animate-spin' })
      ),
      steps.length === 0 && h(EmptyState, {
        title: 'This guide has no steps yet',
        description: 'The guide exists but has no steps to show. Ask the agent to restart or revise the plan.'
      }),
      steps.length > 0 && h(StepsChecklist, {
        steps,
        verified: verifications,
        currentIdx: currentStep,
        live: true
      }),
      (sayLine || stallNote) && h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 } },
        progressLine && h('div', { style: { fontSize: 11.5, color: '#8a8f98' } }, progressLine),
        sayLine && h('div', {
          style: {
            fontSize: 13, color: '#eceef1', background: 'rgba(255,255,255,.05)',
            border: '1px solid rgba(255,255,255,.07)', borderRadius: 10, padding: '10px 14px'
          }
        }, sayLine),
        stallNote && h('div', {
          style: {
            fontSize: 12, color: '#f5c26b',
            border: '1px solid rgba(245,194,107,.35)', borderRadius: 10, padding: '8px 12px'
          }
        }, stallNote)
      ),
      currentLabel && h('div', {
        style: { fontSize: 11.5, color: '#8a8f98' }
      }, 'ring parked on: ', h('b', { style: { color: '#d0d6e0', fontWeight: 500 } }, currentLabel.slice(0, 60))),
      h('div', { style: { fontSize: 11, color: '#6f7784', marginTop: 4 } }, petSmokeLine)
    ),

    !error && !live && (finishedRows.length > 0
      ? h(HistoryList, { guides: finishedRows })
      : h(EmptyState, {
        title: 'No butler task running',
        description: 'Ask in chat to fix something on your Mac, or run `hermes butler doctor`. The butler appears when a plan needs your hands on the screen.'
      })),

    !error && !live && finishedRows.length > 0 && h('div', {
      style: { fontSize: 11, color: '#6f7784', marginTop: 4 }
    }, petSmokeLine)
  )
}

export default {
  id: ID,
  name: 'Butler',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
    try { petCtx = ctx.pet || null } catch (err) { petCtx = null }
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => h(ButlerApp, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 40,
        data: { path: ROUTE, label: 'Butler', codicon: 'project' }
      },
      {
        id: 'launcher-chip',
        area: 'statusBar.right',
        order: 45,
        render: () => h(LauncherButton, { compact: true })
      },
      {
        id: 'summon',
        area: KEYBINDS_AREA,
        data: {
          id: 'butler.summon',
          label: 'Ask the butler',
          category: 'view',
          defaults: ['mod+shift+a'],
          run: () => openSummonBar('shortcut')
        }
      },
      {
        id: 'summon-bar',
        area: TITLEBAR_AREAS.right,
        order: 60,
        render: () => h(SummonBar, {})
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'butler.open',
          label: 'Open Butler guide state',
          keywords: ['butler', 'guide', 'follow', 'walk', 'steps'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])

    // Installing the plugin is the opt-in: arm the in-window listener and the
    // pop-out chip on register. The config poll still honours an explicit
    // stored `false` (the manual escape hatch) after a plugin reload.
    try {
      ctx.onDispose(() => {
        stopPetClickWatcher()
        petClickClaim = false
        petCanvasSeen = false
        summonOpen = false
        summonListeners.clear()
        try {
          if (claimConfigTimer !== null) window.clearInterval(claimConfigTimer)
        } catch (err) { }
        claimConfigTimer = null
        try {
          if (summonPendingTimer !== null) window.clearInterval(summonPendingTimer)
        } catch (err) { }
        summonPendingTimer = null
      })
    } catch (err) { }
    setPetClickClaim(true)
    syncCompanionClaim(true)
    // Reconcile with the stored value (including an explicit false) and keep
    // the companion heartbeat fresh on the periodic tick.
    try { syncPetClickClaim() } catch (err) { }
    startClaimConfigPolling(ctx)
    startSummonPendingPolling(ctx)
    startPetSpeechPolling(ctx)
  }
}
