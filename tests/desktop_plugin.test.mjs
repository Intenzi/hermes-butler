/**
 * Behaviour tests for the desktop half.
 *
 * The disk plugin may only import @hermes/plugin-sdk, react and react/jsx-runtime,
 * so this harness links those specifiers to stubs with `vm.SourceTextModule`.
 * It also appends an export list for the module-scope internals under test; the
 * shipped plugin's own exports are untouched.
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_JS = path.join(HERE, '..', 'desktop', 'plugin.js')
const INTERNALS = [
  'summonButler', 'sendSummonText', 'writePetClickClaim', 'setPetClickClaim',
  'startPetClickWatcher', 'stopPetClickWatcher', 'openSummonBar',
  'spawnSummonDouble', 'configPetClickClaim', 'subscribeSummonBar', 'petCanvasNode'
]

function makeDocument({ canvas = null } = {}) {
  const listeners = []
  const body = {
    children: [],
    appendChild(element) {
      element.parentNode = body
      body.children.push(element)
      return element
    },
    removeChild(element) {
      const index = body.children.indexOf(element)
      if (index >= 0) body.children.splice(index, 1)
      element.parentNode = null
    }
  }
  return {
    body,
    listeners,
    querySelector(selector) {
      return selector === 'canvas[aria-label$=" pet"]'
        ? (typeof canvas === 'function' ? canvas() : canvas)
        : null
    },
    createElement() {
      return {
        style: {},
        attrs: {},
        parentNode: null,
        setAttribute(name, value) { this.attrs[name] = value }
      }
    },
    addEventListener(type, listener, capture) {
      listeners.push({ type, listener, capture })
    },
    removeEventListener(type, listener, capture) {
      const index = listeners.findIndex(entry => entry.type === type
        && entry.listener === listener && entry.capture === capture)
      if (index >= 0) listeners.splice(index, 1)
    }
  }
}

async function loadPlugin({ host, rest, document }) {
  const contributions = []
  const disposers = []
  const intervals = []
  const consoleCalls = []
  const raf = { calls: 0 }

  const context = vm.createContext({
    console: {
      log(...args) { consoleCalls.push(['log', ...args]) },
      warn(...args) { consoleCalls.push(['warn', ...args]) },
      error(...args) { consoleCalls.push(['error', ...args]) }
    },
    document,
    Date,
    Math,
    Number,
    Promise,
    setTimeout,
    clearTimeout,
    queueMicrotask
  })
  context.window = {
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    innerWidth: 1280,
    innerHeight: 800,
    requestAnimationFrame(fn) {
      raf.calls += 1
      if (raf.calls <= 2) queueMicrotask(fn)
      return raf.calls
    },
    cancelAnimationFrame() {}
  }

  const sdk = new vm.SyntheticModule([
    'Button', 'Codicon', 'EmptyState', 'KEYBINDS_AREA', 'PALETTE_AREA',
    'ROUTES_AREA', 'SIDEBAR_NAV_AREA', 'TITLEBAR_AREAS', 'cn', 'host', 'useQuery'
  ], function () {
    this.setExport('Button', props => ({ type: 'button', props }))
    this.setExport('Codicon', props => ({ type: 'codicon', props }))
    this.setExport('EmptyState', props => ({ type: 'empty', props }))
    this.setExport('KEYBINDS_AREA', 'keybinds')
    this.setExport('PALETTE_AREA', 'palette')
    this.setExport('ROUTES_AREA', 'routes')
    this.setExport('SIDEBAR_NAV_AREA', 'sidebarNav')
    this.setExport('TITLEBAR_AREAS', { left: 'titleBar.left', center: 'titleBar.center', right: 'titleBar.right' })
    this.setExport('cn', (...parts) => parts.filter(Boolean).join(' '))
    this.setExport('host', host)
    this.setExport('useQuery', () => ({ data: null, error: null, isFetching: false, refetch() {} }))
  }, { context, identifier: '@hermes/plugin-sdk' })

  const react = new vm.SyntheticModule(['useEffect', 'useRef', 'useState'], function () {
    this.setExport('useEffect', () => {})
    this.setExport('useRef', value => ({ current: value }))
    this.setExport('useState', value => [typeof value === 'function' ? value() : value, () => {}])
  }, { context, identifier: 'react' })

  const jsxRuntime = new vm.SyntheticModule(['jsx', 'jsxs'], function () {
    this.setExport('jsx', (type, props, key) => ({ type, props, key }))
    this.setExport('jsxs', (type, props, key) => ({ type, props, key }))
  }, { context, identifier: 'react/jsx-runtime' })

  const source = `${readFileSync(PLUGIN_JS, 'utf8')}\nexport { ${INTERNALS.join(', ')} }\n`
  const module = new vm.SourceTextModule(source, { context, identifier: PLUGIN_JS })
  await module.link(async specifier => {
    if (specifier === '@hermes/plugin-sdk') return sdk
    if (specifier === 'react') return react
    if (specifier === 'react/jsx-runtime') return jsxRuntime
    throw new Error(`unexpected import ${specifier}`)
  })
  await module.evaluate()

  const plugin = module.namespace.default
  const ctx = {
    rest,
    pet: {},
    registerMany(items) { contributions.push(...items) },
    onDispose(fn) { disposers.push(fn) },
    setInterval(fn, ms) { intervals.push({ fn, ms }); return () => {} }
  }
  plugin.register(ctx)
  // Let register()'s config sync settle.
  await Promise.resolve()
  await Promise.resolve()

  return {
    plugin,
    api: module.namespace,
    contributions,
    disposers,
    intervals,
    consoleCalls,
    ctx,
    dispose() { for (const fn of disposers.splice(0)) fn() }
  }
}

function defaultHost(overrides = {}) {
  return {
    request: async () => [],
    openSession: async () => true,
    notify() {},
    navigate() {},
    composer: { submit: () => true, setDraft: async () => true },
    ...overrides
  }
}

test('registers the "Ask the butler" global shortcut', async () => {
  const env = await loadPlugin({ host: defaultHost(), rest: async () => ({}), document: makeDocument() })
  const keybind = env.contributions.find(item => item.area === 'keybinds')

  assert.ok(keybind, 'KEYBINDS_AREA contribution exists')
  assert.equal(keybind.data.label, 'Ask the butler')
  assert.equal(keybind.data.id, 'butler.summon')
  assert.equal(keybind.data.defaults.join(','), 'mod+shift+a')
  assert.equal(typeof keybind.data.run, 'function')
})

test('pane header toggle writes the hot-reload config knob', async () => {
  const calls = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (opts && opts.method === 'POST') return { summon: { pet_click_claim: true } }
    return { summon: { pet_click_claim: false } }
  }
  const env = await loadPlugin({ host: defaultHost(), rest, document: makeDocument() })

  assert.equal(await env.api.writePetClickClaim(true), true)
  const write = calls.find(call => call.path === '/config' && call.opts && call.opts.method === 'POST')
  assert.ok(write, 'POST /config was issued')
  assert.equal(write.opts.body.summon.pet_click_claim, true)
  assert.equal(write.opts.body.pet_click_claim, true)
  env.api.setPetClickClaim(false)
})

test('summon reuses an existing Butler session and sends immediately', async () => {
  const opened = []
  const submitted = []
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.list') return [{ id: 's1', title: 'Butler', created_at: 1 }]
      if (method === 'session.activate') return { ok: true, params }
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async id => { opened.push(id); return true },
    composer: {
      submit: (id, text) => { submitted.push({ id, text }); return id === 's1' }
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.summonButler('fix my wifi')

  assert.equal(result.ok, true)
  assert.deepEqual(opened, ['s1'])
  assert.deepEqual(submitted, [{ id: 's1', text: 'fix my wifi' }])
})

test('summon creates the Butler chat when none exists', async () => {
  const requested = []
  const submitted = []
  const host = defaultHost({
    request: async (method, params) => {
      requested.push(method)
      if (method === 'session.list') return []
      if (method === 'session.create') return { session_id: 'runtime-2', stored_session_id: 's2' }
      if (method === 'session.title') return {}
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async () => true,
    composer: {
      submit: (id, text) => { submitted.push({ id, text }); return id === 's2' }
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.summonButler('open system settings')

  assert.equal(result.ok, true)
  assert.ok(requested.includes('session.create'))
  assert.deepEqual(submitted, [{ id: 's2', text: 'open system settings' }])
})

test('petless summon still sends and skips the companion double', async () => {
  const posts = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 's1', title: 'Butler', created_at: 1 }]
      : [],
    openSession: async () => true,
    composer: { submit: () => true }
  })
  const env = await loadPlugin({
    host,
    rest: async (requestPath, opts) => {
      if (requestPath === '/summon/pet') {
        posts.push(requestPath)
        return { ok: true, pet: false }
      }
      return {}
    },
    document: makeDocument({ canvas: null })
  })

  assert.equal((await env.api.summonButler('petless request')).ok, true)
  assert.equal(await env.api.spawnSummonDouble(), false)
  assert.deepEqual(posts, ['/summon/pet'])
})

test('reach-in listener degrades silently when no pet canvas exists', async () => {
  const document = makeDocument({ canvas: null })
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async () => ({ summon: { pet_click_claim: false } }),
    document
  })

  assert.doesNotThrow(() => env.api.setPetClickClaim(true))
  await Promise.resolve()
  await Promise.resolve()

  assert.equal(document.listeners.length, 1)
  assert.equal(document.listeners[0].type, 'pointerdown')
  assert.equal(document.listeners[0].capture, true)
  assert.equal(document.body.children.length, 1)
  assert.equal(document.body.children[0].style.opacity, '0')
  assert.equal(env.consoleCalls.length, 0, 'no console spam')

  assert.doesNotThrow(() => env.api.setPetClickClaim(false))
  assert.equal(document.listeners.length, 0)
  assert.equal(document.body.children.length, 0)
  env.dispose()
})

test('capture-phase pet click summons only when the toggle is on', async () => {
  const rect = { left: 40, top: 60, width: 80, height: 90, right: 120, bottom: 150 }
  const canvas = { getBoundingClientRect: () => rect }
  const document = makeDocument({ canvas })
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async () => ({ summon: { pet_click_claim: false } }),
    document
  })
  const events = []
  const unsubscribe = env.api.subscribeSummonBar(state => events.push(state))
  assert.equal(events.at(-1).open, false)

  env.api.setPetClickClaim(true)
  const armed = document.listeners[0].listener
  armed({ button: 0, clientX: 80, clientY: 100 })
  assert.equal(events.at(-1).open, true)
  assert.equal(events.at(-1).origin, 'pet')

  armed({ button: 0, clientX: 5, clientY: 5 })
  assert.equal(events.at(-1).open, true, 'clicks outside the pet do not re-fire')

  env.api.setPetClickClaim(false)
  assert.equal(document.listeners.length, 0, 'disabling removes the capture listener')
  assert.equal(document.body.children.length, 0, 'disabling removes the follow overlay')
  unsubscribe()
  env.dispose()
})
