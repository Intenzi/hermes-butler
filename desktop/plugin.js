/**
 * Butler — guide-state pane for the Hermes desktop.
 *
 * Backend: ~/.hermes/plugins/butler/dashboard/plugin_api.py, mounted at
 * /api/plugins/butler/. The pane polls /state for the live guide and /history
 * for finished ones. The status bar carries a small Butler launcher that
 * creates or focuses the "Butler" chat.
 *
 * SDK-only surface (routes, nav, palette, status bar, host sessions). No
 * pet-canvas touching: bubble rendering stays on the Python side via ctx.pet.say.
 */
import {
  Button,
  Codicon,
  EmptyState,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
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

let rest = null
let petCtx = null
let lastSaid = null
let launcherBusy = false
// smoke-test counters so the page can PROVE the plugin side spoke (or not):
// petSayTicks increments on every say attempt, petLastTick stores wall-clock.
let petSayTicks = 0
let petLastTick = null

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

async function focusSession(row) {
  if (!row) return false
  const storedId = asText(row.resolved_id) || asText(row.stored_session_id)
    || asText(row.id) || asText(row.session_id)
  if (!storedId) return false
  if (host && typeof host.openSession === 'function') {
    try {
      await host.openSession(storedId, { intent: 'in-place' })
      return true
    } catch (err) {
      // Older desktop builds may only expose the gateway RPC.
    }
  }
  if (!host || typeof host.request !== 'function') return false
  try {
    await host.request('session.activate', {
      session_id: asText(row.session_id) || asText(row.id) || storedId
    })
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
      await host.openSession(target, { intent: 'in-place' })
      return true
    } catch (err) {
      // Fall through to activating the runtime session.
    }
  }
  if (runtimeId) {
    try {
      await host.request('session.activate', { session_id: runtimeId })
      return true
    } catch (err) {
      return false
    }
  }
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

/** Create the one "Butler" chat, or focus the oldest matching one. */
async function openButlerSession() {
  if (launcherBusy) return false
  launcherBusy = true
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

// ---------------------------------------------------------------- pet speech

/**
 * The pet heartbeat never repeats a step instruction. It speaks only status
 * lines: done, stalled, capture failed, and stop summaries. A running guide
 * with a fresh capture stays silent; its instruction lives in the overlay
 * bubble and on the page checklist.
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
  return null
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
  }
}
