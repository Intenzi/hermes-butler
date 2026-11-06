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
  cn,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'butler'
const ROUTE = '/butler'
const REFRESH_MS = 1500

let rest = null

function h(type, props, ...children) {
  let key = undefined
  const p = {}
  if (props) { for (const [k, v] of Object.entries(props)) { if (k === 'key') { key = v } else { p[k] = v } } }
  const flat = children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false)
  if (flat.length === 1) { p.children = flat[0] } else if (flat.length > 1) { p.children = flat }
  return jsx(type, p, key)
}

function Pane({ verified = [], sayLine = null, stallNote = null }) {
  return h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8, marginTop: 10 } },
    verified.length > 0 && h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6 } },
      verified.map((v, i) => h('span', {
        key: i,
        style: {
          fontSize: 11, padding: '2px 10px', borderRadius: 99,
          border: '1px solid rgba(111,211,154,.45)', color: '#6fd39a',
          background: 'rgba(111,211,154,.10)'
        }
      }, `✓ step ${v.step + 1}`))),
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
  )
}

function ButlerApp() {
  const { data, error, isFetching, refetch } = useQuery({
    queryKey: ['butler', 'state'],
    queryFn: () => rest('/state'),
    refetchInterval: REFRESH_MS,
    refetchOnMount: 'always'
  })
  const guide = data && data.guide
  const state = guide && guide.state
  const step = guide && guide.steps && guide.steps[state && state.idx != null ? state.idx : 0]

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
      h(Button, { variant: 'secondary', size: 'sm', onClick: () => refetch() }, 'Refresh')
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
        h('span', { style: { fontSize: 13.5, fontWeight: 600, color: '#eceef1' } }, guide.title),
        h('span', {
          style: {
            fontSize: 10, padding: '2px 9px', borderRadius: 99, letterSpacing: .5,
            border: '1px solid rgba(122,134,255,.4)', color: '#9aa3ea', background: 'rgba(122,134,255,.10)'
          }
        }, `session ${guide.session_id ? guide.session_id.slice(0, 6) : 'chat'}`),
        h('span', {
          style: {
            fontSize: 10, padding: '2px 9px', borderRadius: 99,
            border: '1px solid rgba(255,255,255,.09)', color: '#8a8f98'
          }
        }, (state && state.status) || 'running'),
        isFetching && h(Codicon, { name: 'sync', className: 'animate-spin' })
      ),
      h(Pane, {
        verified: guide.verifications || [],
        sayLine: (step && step.say) || (state && state.say) || null,
        stallNote: (state && state.note) || (guide.overlay && String(guide.overlay).startsWith('unavailable')
          ? `overlay: ${guide.overlay} — rings stay in-app` : null)
      }),
      guide.current_label && h('div', {
        style: { fontSize: 11.5, color: '#8a8f98' }
      }, 'ring parked on: ', h('b', { style: { color: '#d0d6e0', fontWeight: 500 } }, String(guide.current_label).slice(0, 60)))
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
        data: { path: ROUTE, label: 'Butler', codicon: 'walktown' }
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
