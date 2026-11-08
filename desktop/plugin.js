/**
 * Butler — guide-state pane for the Hermes desktop.
 *
 * Backend: ~/.hermes/plugins/butler/dashboard/plugin_api.py, mounted at
 * /api/plugins/butler/. The pane polls /state for the live guide and /history
 * for finished ones. The status bar carries a small Butler launcher that
 * creates or focuses the "Butler" chat.
 *
 * SDK-first surface (routes, nav, palette, status bar, keybinds, host sessions,
 * composer submit). The one deliberate reach-in is the opt-in pet-click
 * receiver: it only READS the pet canvas rect and observes pointerdown; it
 * never paints into the canvas and is torn down when the toggle is off.
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
// we never paint into it and never edit Hermes core to add a hook.
const PET_CANVAS_SELECTOR = 'canvas[aria-label$=" pet"]'

let rest = null
let petCtx = null
let lastSaid = null
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
// keybind and the opt-in pet-click listener just publish "open".
let summonOpen = false
let summonOrigin = 'shortcut'
const summonListeners = new Set()
let petClickClaim = false
let petClickWatcher = null
let petClickConfigWrites = 0

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

function oldestSession(rows) {
  const matching = asArray(rows).map(exactButlerTitle).filter(Boolean)
  matching.sort((a, b) => sessionStartedAt(a) - sessionStartedAt(b))
  return matching[0] || null
}

async function listButlerSessions() {
  if (!host || typeof host.request !== 'function') return []
  try {
    const result = await host.request('session.list', { limit: 200 })
    const rows = Array.isArray(result) ? result : asArray(result && result.sessions)
    return rows.filter(row => exactButlerTitle(row))
  } catch (err) {
    return []
  }
}

function sessionTargetId(row) {
  return asText(row && (row.resolved_id || row.stored_session_id
    || row.id || row.session_id))
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

async function focusSession(row) {
  if (!row) return false
  const storedId = sessionTargetId(row)
  if (!storedId) return false
  if (host && typeof host.openSession === 'function') {
    try {
      const opened = await host.openSession(storedId, { intent: 'in-place' })
      launcherTargetId = storedId
      launcherTargetAliases = uniqueIds(launcherTargetAliases, rowTargetIds(row), openResultIds(opened))
      return true
    } catch (err) {
      // Older desktop builds may only expose the gateway RPC.
    }
  }
  if (!host || typeof host.request !== 'function') return false
  try {
    const activated = await host.request('session.activate', {
      session_id: asText(row.session_id) || asText(row.id) || storedId
    })
    launcherTargetId = storedId
    launcherTargetAliases = uniqueIds(launcherTargetAliases, rowTargetIds(row), openResultIds(activated))
    return true
  } catch (err) {
    return false
  }
}

async function createButlerSession() {
  if (!host || typeof host.request !== 'function') return false
  const created = await host.request('session.create', { title: BUTLER_TITLE })
  const runtimeId = asText(created && created.session_id)
  const storedId = asText(created && created.stored_session_id) || runtimeId
  if (!runtimeId && !storedId) return false
  if (runtimeId) {
    try {
      await host.request('session.title', { session_id: runtimeId, title: BUTLER_TITLE })
    } catch (err) {
      // The create-time title is enough on builds without eager titling.
    }
  }
  const target = storedId || runtimeId
  if (target && host && typeof host.openSession === 'function') {
    try {
      const opened = await host.openSession(target, { intent: 'in-place' })
      launcherTargetId = target
      launcherTargetAliases = uniqueIds(
        launcherTargetAliases,
        target,
        runtimeId,
        openResultIds(created),
        openResultIds(opened)
      )
      return true
    } catch (err) {
      // Fall through to activating the runtime session.
    }
  }
  if (runtimeId) {
    try {
      const activated = await host.request('session.activate', { session_id: runtimeId })
      launcherTargetId = target
      launcherTargetAliases = uniqueIds(
        launcherTargetAliases,
        target,
        runtimeId,
        openResultIds(created),
        openResultIds(activated)
      )
      return true
    } catch (err) {
      return false
    }
  }
  launcherTargetId = target
  launcherTargetAliases = uniqueIds(launcherTargetAliases, target, runtimeId)
  return Boolean(target)
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

function notifySummonSuccess() {
  try {
    host && host.notify && host.notify({
      kind: 'success',
      title: 'Butler',
      message: 'Summoned → Butler chat'
    })
  } catch (err) {
    // A missing toast surface must not break the send.
  }
}

/** Create the one "Butler" chat, or focus the oldest matching one. */
async function openButlerSession() {
  if (launcherBusy) return false
  launcherBusy = true
  launcherTargetId = null
  launcherTargetAliases = []
  try {
    const existing = oldestSession(await listButlerSessions())
    if (existing && await focusSession(existing)) return true

    let created = false
    try {
      created = await createButlerSession()
    } catch (err) {
      created = false
    }
    if (created) return true

    // Creation unsupported (or failed): fall back to the oldest match.
    const fallback = oldestSession(await listButlerSessions())
    if (fallback && await focusSession(fallback)) return true
    notifyLauncherFailure()
    return false
  } finally {
    launcherBusy = false
  }
}

// ------------------------------------------------------------ summon door

function wait(ms) {
  return new Promise(resolve => window.setTimeout(resolve, ms))
}

function petCanvasNode() {
  if (typeof document === 'undefined') return null
  try {
    return document.querySelector(PET_CANVAS_SELECTOR) || null
  } catch (err) {
    return null
  }
}

function subscribeSummonBar(listener) {
  summonListeners.add(listener)
  try {
    listener({ open: summonOpen, origin: summonOrigin })
  } catch (err) {
    // A broken listener must not break the summon.
  }
  return () => { summonListeners.delete(listener) }
}

function publishSummonBar() {
  for (const listener of [...summonListeners]) {
    try {
      listener({ open: summonOpen, origin: summonOrigin })
    } catch (err) {
      // Keep the remaining listeners alive.
    }
  }
}

function openSummonBar(origin = 'shortcut') {
  summonOpen = true
  summonOrigin = origin
  publishSummonBar()
}

function dismissSummonBar() {
  summonOpen = false
  publishSummonBar()
}

function configPetClickClaim(data) {
  if (!data || typeof data !== 'object') return false
  if (data.summon && typeof data.summon === 'object'
    && typeof data.summon.pet_click_claim === 'boolean') {
    return data.summon.pet_click_claim
  }
  if (typeof data['summon.pet_click_claim'] === 'boolean') {
    return data['summon.pet_click_claim']
  }
  return data.pet_click_claim === true
}

async function writePetClickClaim(enabled) {
  const next = Boolean(enabled)
  // Apply locally first so the switch cannot lag the listener; revert if the
  // backend refuses the write.
  setPetClickClaim(next)
  if (!rest || typeof rest !== 'function') return false
  const writeId = ++petClickConfigWrites
  try {
    const result = await rest('/config', {
      method: 'POST',
      body: { summon: { pet_click_claim: next }, pet_click_claim: next }
    })
    return configPetClickClaim(result) === next
  } catch (err) {
    if (writeId === petClickConfigWrites) setPetClickClaim(!next)
    return false
  }
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
    // No canvas (no pet installed, or a Hermes update moved it): degrade
    // silently. The watcher keeps looking so a later pet is picked up.
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
  petClickClaim = Boolean(enabled)
  if (petClickClaim) startPetClickWatcher()
  else stopPetClickWatcher()
}

async function syncPetClickClaim() {
  if (!rest || typeof rest !== 'function') return
  try {
    const result = await rest('/config')
    setPetClickClaim(configPetClickClaim(result))
  } catch (err) {
    // Backend not mounted yet; the toggle still writes once it is.
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
  const row = oldestSession(await listButlerSessions())
  if (!row) return null
  const ids = rowTargetIds(row)
  const target = sessionTargetId(row) || asText(fallback) || ids[0]
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
    if (now() >= nextRefreshAt) {
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
  draft_failed: 'draft_failed'
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
  const error = summonErrorText(result && result.error)
  if (error) message += ` — ${error}`
  if (result && result.seated) {
    message += '. Draft seat: seated. Press Enter in the Butler composer to send it.'
  } else {
    message += '. Draft seat: not seated.'
  }
  return message
}

/** Resolve the one Butler chat, send the request, then own the leaving visual. */
async function summonButler(text, options = {}) {
  const message = asText(text)
  if (!message) return { ok: false, reason: 'empty' }
  if (!await openButlerSession()) return { ok: false, reason: 'no_session' }
  const target = launcherTargetId
  const result = await sendSummonText(target, message, options)
  return { ...result, session: result.session || target }
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
    result = { ok: false, reason: 'threw', error: err, seated: false }
  }
  if (result.ok) {
    notifySummonSuccess()
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
  const inputRef = useRef(null)
  const barRef = useRef(null)
  const busyRef = useRef(false)
  busyRef.current = busy

  useEffect(() => subscribeSummonBar(state => {
    setOpen(state.open)
    setOrigin(state.origin)
  }), [])

  useEffect(() => {
    if (!open) return undefined
    setText('')
    const id = window.setTimeout(() => {
      try { inputRef.current && inputRef.current.focus() } catch (err) { }
    }, 0)
    return () => window.clearTimeout(id)
  }, [open])

  // Follow the pet while the bar is open; otherwise park upper-right. The
  // same read-only locator the opt-in click listener uses.
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

  const submit = async () => {
    if (busyRef.current) return
    const value = text.trim()
    if (!value) return
    setBusy(true)
    const outcome = await summonFromBar(value)
    setBusy(false)
    // Failure keeps the typed text in the bar so the user can retry; only a
    // real send clears and dismisses it. The toast names the exact reason.
    if (outcome.clearText) setText('')
    if (outcome.dismiss) dismissSummonBar()
  }

  if (!open) return null

  const placement = position || { left: undefined, top: 16 }
  return h('div', {
    ref: barRef,
    role: 'dialog',
    'aria-label': 'Ask the butler',
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
      onBlur: () => {
        if (!busyRef.current) dismissSummonBar()
      },
      style: {
        width: '100%', boxSizing: 'border-box', fontSize: 13,
        color: '#eceef1', background: 'rgba(255,255,255,.06)',
        border: '1px solid rgba(255,255,255,.12)', borderRadius: 8,
        padding: '8px 10px', outline: 'none'
      }
    })
  )
}

function ButlerPetClickToggle() {
  const configQuery = useQuery({
    queryKey: ['butler', 'config'],
    queryFn: () => (typeof rest === 'function' ? rest('/config') : Promise.resolve({})),
    refetchInterval: CONFIG_REFRESH_MS,
    refetchOnMount: 'always'
  }) || {}
  const configured = configPetClickClaim(configQuery.data)
  const [enabled, setEnabled] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setEnabled(configured)
    setPetClickClaim(configured)
  }, [configured])

  const onToggle = async (event) => {
    const next = Boolean(event.target.checked)
    setEnabled(next)
    setSaving(true)
    const saved = await writePetClickClaim(next)
    setSaving(false)
    if (!saved) {
      setEnabled(!next)
      setPetClickClaim(!next)
      try {
        host && host.notify && host.notify({
          kind: 'error',
          title: 'Butler',
          message: 'Could not save the pet-click setting. The receiver is off; the shortcut still works.'
        })
      } catch (err) {
        // No toast surface: the reverted switch is the report.
      }
    }
  }

  return h('label', {
    style: {
      display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer',
      fontSize: 12, color: '#c7cbd2', padding: '7px 10px', borderRadius: 9,
      border: '1px solid rgba(255,255,255,.08)', background: 'rgba(255,255,255,.03)'
    }
  },
    h('input', {
      type: 'checkbox',
      checked: enabled,
      disabled: saving,
      'aria-label': 'Click the pet to ask the butler',
      onChange: onToggle,
      style: { cursor: 'pointer', accentColor: 'var(--ui-accent-local, #9aa3ea)' }
    }),
    h('span', { style: { flex: 1 } }, 'Click the pet to ask the butler'),
    h('span', { style: { fontSize: 10, color: enabled ? '#6fd39a' : '#6f7784' } },
      saving ? 'saving…' : (enabled ? 'on' : 'off'))
  )
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
  if (status === 'done') {
    const total = asNumber(cur.total)
    const verified = asNumber(cur.verified)
    return `Guide complete · ${verified} of ${total} verified`
  }
  if (status === 'stopped') {
    return note && note !== instruction ? `Butler stopped: ${note}` : 'Butler stopped'
  }
  if (status === 'stalled') {
    return note || `Butler stalled on step ${asNumber(cur.currentStep) + 1}`
  }
  if (status === 'capture_failed') {
    return note && note !== instruction
      ? `Capture failed: ${note}`
      : 'Capture failed — butler could not read the screen'
  }
  // Live step: speak the current instruction (restored by user request).
  return instruction
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

  // Pet speech is evaluated on its own 2 s clock so a stale React feed cannot
  // pin the last line on screen. Running guides stay silent.
  const stateRef = useRef({ status: null, note: null, instruction: null, currentStep: 0, total: 0, verified: 0 })
  stateRef.current = {
    status,
    note: asText(state.note),
    instruction: sayLine,
    currentStep,
    total: steps.length,
    verified: verifications.length
  }
  useEffect(() => {
    const beat = () => {
      const line = petStatusLine(stateRef.current)
      if (!line) {
        try { petCtx && petCtx.clear && petCtx.clear('butler-say') } catch (err) { }
        lastSaid = null
        return
      }
      if (lastSaid === line) return
      try {
        petCtx && typeof petCtx.say === 'function'
          && petCtx.say(line, { tone: 'info', ttlMs: 9000, id: 'butler-say' })
      } catch (err) { }
      petSayTicks += 1
      petLastTick = new Date().toLocaleTimeString()
      lastSaid = line
    }
    beat()
    const id = window.setInterval(beat, 2000)
    return () => window.clearInterval(id)
  }, [])

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

    h(ButlerPetClickToggle, {}),

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

    // pet-click claim is opt-in and hot-reloads from plugin data on every
    // config poll; disabling it tears the listener + overlay down.
    try {
      ctx.onDispose(() => {
        stopPetClickWatcher()
        petClickClaim = false
        summonOpen = false
        summonListeners.clear()
      })
    } catch (err) { }
    try { syncPetClickClaim() } catch (err) { }
    try {
      if (typeof ctx.setInterval === 'function') ctx.setInterval(syncPetClickClaim, CONFIG_REFRESH_MS)
    } catch (err) {
      // Older hosts: no tracked timer; the pane toggle still applies directly.
    }
  }
}
