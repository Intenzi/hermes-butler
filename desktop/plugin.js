/**
 * Butler — guide-state pane for the Hermes desktop.
 *
 * Backend: ~/.hermes/plugins/butler/dashboard/plugin_api.py, mounted at
 * /api/plugins/butler/. The pane polls /state and renders the live guide:
 * title, verified step chips, current say-line, session chip, stall notes.
 *
 * SDK-only surface (routes, nav, palette). No pet-canvas touching: bubble
 * rendering stays on the Python side via ctx.pet.say.
 */
import {
  Button,
  Codicon,
  EmptyState,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  host,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'butler'
const ROUTE = '/butler'
const REFRESH_MS = 1500
const VERIFIED_CHIP_LIMIT = 6

let rest = null

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

function verificationLabel(entry, index) {
  const raw = entry && typeof entry === 'object' ? entry.step : entry
  if (raw === null || raw === undefined || raw === '') return `✓ verified ${index + 1}`
  const step = Number(raw)
  if (!Number.isFinite(step) || step < 0) return `✓ verified ${index + 1}`
  return `✓ step ${Math.floor(step) + 1}`
}

function Pane({ verified = [], sayLine = null, stallNote = null, progressLine = null }) {
  const rows = asArray(verified)
  const visible = rows.slice(0, VERIFIED_CHIP_LIMIT)
  const hidden = rows.length - visible.length
  const say = asText(sayLine)
  const stall = asText(stallNote)
  const progress = asText(progressLine)

  return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, marginTop: 10 } },
    visible.length > 0 && h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6 } },
      visible.map((v, i) => h('span', {
        key: i,
        style: {
          fontSize: 11, padding: '2px 10px', borderRadius: 99,
          border: '1px solid rgba(111,211,154,.45)', color: '#6fd39a',
          background: 'rgba(111,211,154,.10)'
        }
      }, verificationLabel(v, i))),
      hidden > 0 && h('span', {
        key: 'more',
        style: {
          fontSize: 11, padding: '2px 10px', borderRadius: 99,
          border: '1px solid rgba(138,143,152,.35)', color: '#8a8f98',
          background: 'rgba(255,255,255,.04)'
        }
      }, `+${hidden} more`)),
    progress && h('div', { style: { fontSize: 11.5, color: '#8a8f98' } }, progress),
    say && h('div', {
      style: {
        fontSize: 13, color: '#eceef1', background: 'rgba(255,255,255,.05)',
        border: '1px solid rgba(255,255,255,.07)', borderRadius: 10, padding: '10px 14px'
      }
    }, say),
    stall && h('div', {
      style: {
        fontSize: 12, color: '#f5c26b',
        border: '1px solid rgba(245,194,107,.35)', borderRadius: 10, padding: '8px 12px'
      }
    }, stall)
  )
}

function ButlerApp() {
  const query = useQuery({
    queryKey: ['butler', 'state'],
    queryFn: () => (typeof rest === 'function' ? rest('/state') : Promise.resolve({})),
    refetchInterval: REFRESH_MS,
    refetchOnMount: 'always'
  }) || {}
  const { data, error, isFetching, refetch } = query

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
      h(Button, {
        variant: 'secondary',
        size: 'sm',
        onClick: () => { if (typeof refetch === 'function') refetch() }
      }, 'Refresh')
    ),

    error && h('div', {
      style: { fontSize: 12, color: '#f08080', border: '1px solid rgba(240,128,128,.3)', borderRadius: 10, padding: '9px 12px' }
    }, 'Butler backend not reachable: install and enable the butler plugin, then reload desktop plugins (⌘K → Reload desktop plugins).'),

    !error && !guide && h(EmptyState, {
      title: 'No guide is running',
      description: 'Ask in chat to fix something on your Mac, or run `hermes butler doctor`. The butler appears when a plan needs your hands on the screen.'
    }),

    guide && h('div', { style: { display: 'flex', flexDirection: 'column', gap: 10 } },
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
      h(Pane, {
        verified: verifications,
        sayLine,
        stallNote,
        progressLine
      }),
      currentLabel && h('div', {
        style: { fontSize: 11.5, color: '#8a8f98' }
      }, 'ring parked on: ', h('b', { style: { color: '#d0d6e0', fontWeight: 500 } }, currentLabel.slice(0, 60)))
    )
  )
}

export default {
  id: ID,
  name: 'Butler',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
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
