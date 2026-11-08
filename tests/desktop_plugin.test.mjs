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
  'summonButler', 'sendSummonText', 'sendSummonInjection', 'summonFromBar',
  'summonFailureMessage', 'writePetClickClaim', 'setPetClickClaim',
  'startPetClickWatcher', 'stopPetClickWatcher', 'openSummonBar',
  'spawnSummonDouble', 'configPetClickClaim', 'subscribeSummonBar', 'petCanvasNode',
  'petClaimStatusText', 'syncCompanionClaim', 'petStatusRowText', 'summonBarState',
  'readSummonDestination', 'setSummonDestination', 'getSummonDestination', 'butlerCommandText',
  'openFreshButlerSession', 'openButlerSession', 'createButlerSession', 'listButlerSessions',
  'sessionTargetId', 'sessionKeyNamespace', 'expectedSummonSessionKey', 'dismissSummonBar'
]

function makeDocument({
  canvas = null,
  selectors = ['canvas[aria-label$=" pet"]', 'canvas[aria-label="pet"]']
} = {}) {
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
      return selectors.includes(selector)
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

function makeStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    values,
    getItem(key) { return values.has(key) ? values.get(key) : null },
    setItem(key, value) { values.set(key, String(value)) }
  }
}

async function loadPlugin({ host, rest, document, storage }) {
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
  if (storage) context.localStorage = storage
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
  // Let register()'s config sync (GET /config then POST /summon/claim) settle.
  for (let i = 0; i < 8; i += 1) await Promise.resolve()

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

// Deterministic clock for the retry ladder: `wait(ms)` advances time instead of
// actually sleeping, so the default 300 ms grace / 150 ms retry / 6 s budget
// are exercised without a multi-second test.
function makeClock(start = 0) {
  let value = start
  const waits = []
  return {
    now: () => value,
    wait: async ms => {
      waits.push(ms)
      value += Math.max(0, ms)
    },
    advance(ms) {
      value += ms
    },
    waits
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

test('summon injects into the oldest existing Butler chat without using the composer', async () => {
  const opened = []
  const submitted = []
  const injected = []
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
  const env = await loadPlugin({
    host,
    rest: async (requestPath, opts) => {
      if (requestPath === '/summon/send') {
        injected.push({ text: opts.body.text, session_id: opts.body.session_id })
        return { ok: true, session: opts.body.session_id }
      }
      return {}
    },
    document: makeDocument()
  })

  const result = await env.api.summonButler('fix my wifi')

  assert.equal(result.ok, true)
  assert.equal(result.send, 'inject')
  assert.deepEqual(opened, ['s1'])
  assert.deepEqual(injected, [{ text: 'fix my wifi', session_id: 's1' }])
  assert.deepEqual(submitted, [], 'the injection path must not submit /butler')
})

test('summon creates the Butler chat when none exists, then injects into that id', async () => {
  const requested = []
  const opened = []
  const injected = []
  const submitted = []
  const host = defaultHost({
    request: async (method, params) => {
      requested.push(method)
      if (method === 'session.list') return []
      if (method === 'session.create') return { session_id: 'runtime-2', stored_session_id: 's2' }
      if (method === 'session.title') return {}
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async id => { opened.push(id); return true },
    composer: {
      submit: (id, text) => { submitted.push({ id, text }); return id === 's2' }
    }
  })
  const env = await loadPlugin({
    host,
    rest: async (requestPath, opts) => {
      if (requestPath === '/summon/send') {
        injected.push({ text: opts.body.text, session_id: opts.body.session_id })
        return { ok: true, session: opts.body.session_id }
      }
      return {}
    },
    document: makeDocument()
  })

  const result = await env.api.summonButler('open system settings')

  assert.equal(result.ok, true)
  assert.equal(result.send, 'inject')
  assert.ok(requested.includes('session.create'))
  assert.deepEqual(opened, ['s2'])
  assert.deepEqual(injected, [{ text: 'open system settings', session_id: 's2' }])
  assert.deepEqual(submitted, [])
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
      if (requestPath === '/summon/send') return { ok: true, session: opts.body.session_id }
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

test('summon ladder waits 300 ms, retries at 150 ms, and ends on the second success', async () => {
  const clock = makeClock()
  const submittedAt = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 's1', title: 'Butler', created_at: 1 }]
      : [],
    composer: {
      submit() {
        submittedAt.push(clock.now())
        return submittedAt.length === 2
      },
      setDraft: async () => false
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.sendSummonText('s1', 'second time lucky', {
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(result.ok, true)
  assert.equal(result.attempts, 2)
  assert.equal(submittedAt.length, 2, 'a successful second attempt ends the loop')
  assert.ok(submittedAt[0] >= 300, `first submit at ${submittedAt[0]} ms is not before the grace`)
  assert.equal(submittedAt[0], 300)
  assert.equal(submittedAt[1], 450)
})

test('summon ladder keeps retrying until the 6 s budget is spent', async () => {
  const clock = makeClock()
  const submittedAt = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 's1', title: 'Butler', created_at: 1 }]
      : [],
    composer: {
      submit() {
        submittedAt.push(clock.now())
        return false
      },
      setDraft: async () => false
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.sendSummonText('s1', 'never lands', {
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(result.ok, false)
  assert.equal(result.reason, 'no_visible_composer')
  assert.equal(result.seated, false)
  assert.equal(submittedAt[0], 300)
  assert.ok(submittedAt.at(-1) >= 5700, `last submit at ${submittedAt.at(-1)} ms kept retrying`)
  assert.ok(submittedAt.at(-1) < 6000, 'the ladder stops at the 6 s budget')
  assert.ok(submittedAt.length >= 30, `retried ${submittedAt.length} times`)
  assert.ok(clock.now() >= 6000, 'the whole budget was spent')
})

test('summon re-derives a stale stored id from the live session list', async () => {
  const clock = makeClock()
  const listCalls = []
  const submitted = []
  const host = defaultHost({
    request: async method => {
      if (method === 'session.list') {
        listCalls.push(clock.now())
        return [{ id: 'runtime-9', stored_session_id: 'stored-1', title: 'Butler', created_at: 1 }]
      }
      return []
    },
    composer: {
      submit(id, text) {
        submitted.push({ id, text })
        return id === 'runtime-9'
      },
      setDraft: async () => false
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.sendSummonText('stored-1', 'stale address', {
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(result.ok, true)
  assert.equal(result.session, 'runtime-9')
  assert.ok(listCalls.length >= 1, 'session.list was re-read between attempts')
  assert.deepEqual(submitted.map(call => call.id), ['stored-1', 'stored-1', 'runtime-9'])
})

test('summon honors a runtime id returned by openSession', async () => {
  const clock = makeClock()
  const submitted = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 'stored-1', title: 'Butler', created_at: 1 }]
      : [],
    openSession: async () => ({ resolved_id: 'runtime-7' }),
    composer: {
      submit(id) {
        submitted.push(id)
        return id === 'runtime-7'
      },
      setDraft: async () => false
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.summonButler('open result address', {
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(result.ok, true)
  assert.equal(result.session, 'runtime-7')
  assert.deepEqual(submitted, ['stored-1', 'runtime-7'])
})

test('bar failure keeps the text and toasts the exact reason plus an unseated draft', async () => {
  const clock = makeClock()
  const notifications = []
  const draftCalls = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 's1', title: 'Butler', created_at: 1 }]
      : [],
    notify(notification) { notifications.push(notification) },
    composer: {
      submit: () => false,
      setDraft: async (id, text) => {
        draftCalls.push({ id, text })
        return false
      }
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const outcome = await env.api.summonFromBar('fix my wifi', {
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(outcome.result.ok, false)
  assert.equal(outcome.result.reason, 'no_visible_composer')
  assert.equal(outcome.clearText, false, 'the typed text stays in the bar')
  assert.equal(outcome.dismiss, false, 'the bar stays open for a retry')
  assert.deepEqual(draftCalls, [{ id: 's1', text: '/butler fix my wifi' }])
  const toast = notifications.find(notification => notification.kind === 'error')
  assert.ok(toast, 'a failure toast was shown')
  assert.match(toast.message, /^Could not summon: no visible composer \(no_visible_composer\)/)
  assert.match(toast.message, /Draft seat: not seated\./)
})

test('bar failure reports a seated draft without claiming the send succeeded', async () => {
  const clock = makeClock()
  const notifications = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 's1', title: 'Butler', created_at: 1 }]
      : [],
    notify(notification) { notifications.push(notification) },
    composer: {
      submit: () => false,
      setDraft: async () => true
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const outcome = await env.api.summonFromBar('seat this instead', {
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(outcome.result.ok, false)
  assert.equal(outcome.result.seated, true)
  assert.equal(outcome.result.reason, 'no_visible_composer')
  assert.equal(outcome.clearText, false)
  const toast = notifications.find(notification => notification.kind === 'error')
  assert.ok(toast)
  assert.match(toast.message, /Draft seat: seated\./)
  assert.doesNotMatch(toast.message, /Draft seat: not seated\./)
})

test('denied injection falls back to /butler and toasts honestly', async () => {
  const clock = makeClock()
  const notifications = []
  const submitted = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 's1', title: 'Butler', created_at: 1 }]
      : [],
    notify(notification) { notifications.push(notification) },
    composer: { submit: (id, text) => { submitted.push({ id, text }); return true } }
  })
  const env = await loadPlugin({
    host,
    rest: async requestPath => requestPath === '/summon/send'
      ? { ok: false, reason: 'injection_denied' }
      : {},
    document: makeDocument()
  })

  const outcome = await env.api.summonFromBar('make a guide', {
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(outcome.result.ok, true)
  assert.equal(outcome.result.fallback, true)
  assert.equal(outcome.result.injection_reason, 'injection_denied')
  assert.equal(outcome.clearText, true)
  assert.equal(outcome.dismiss, true)
  assert.deepEqual(submitted, [{ id: 's1', text: '/butler make a guide' }])
  const toast = notifications.find(notification => notification.kind === 'warning')
  assert.ok(toast, 'the fallback is reported as a warning, not a full success')
  assert.match(toast.message, /^Summoned → Butler chat \(existing\)/)
  assert.match(toast.message, /could not host the full conversation/)
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

test('config loaded true arms the in-window receiver without a manual toggle', async () => {
  // Reproduces the runtime condition: plugin register reads the gateway
  // /config (which now round-trips the engine's file) and the toggle is on.
  const rect = { left: 40, top: 60, width: 80, height: 90, right: 120, bottom: 150 }
  const canvas = { getBoundingClientRect: () => rect }
  const document = makeDocument({ canvas })
  const calls = []
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, opts) => {
      calls.push({ path: requestPath, opts })
      if (requestPath === '/config') return { summon: { pet_click_claim: true } }
      if (requestPath === '/summon/claim') return { ok: true, enabled: true, running: true }
      return {}
    },
    document
  })

  const listener = document.listeners.find(entry => entry.type === 'pointerdown')
  assert.ok(listener, 'register() armed the capture listener from the config file')
  const events = []
  const unsubscribe = env.api.subscribeSummonBar(state => events.push(state))
  listener.listener({ button: 0, clientX: 80, clientY: 100 })
  assert.equal(events.at(-1).open, true)
  assert.equal(events.at(-1).origin, 'pet')
  const claim = calls.find(call => call.path === '/summon/claim' && call.opts?.method === 'POST')
  assert.ok(claim, 'the companion chip is synced from the same config value')
  assert.equal(claim.opts.body.enabled, true)
  unsubscribe()
  env.dispose()
})

test('the pet locator accepts the core bare aria-label="pet" spelling', async () => {
  const rect = { left: 10, top: 10, width: 32, height: 32, right: 42, bottom: 42 }
  const canvas = { getBoundingClientRect: () => rect }
  const document = makeDocument({ canvas, selectors: ['canvas[aria-label="pet"]'] })
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async requestPath => requestPath === '/config'
      ? { summon: { pet_click_claim: true } }
      : { ok: true, enabled: true },
    document
  })

  assert.equal(env.api.petCanvasNode(), canvas)
  env.api.setPetClickClaim(true)
  const listener = document.listeners.find(entry => entry.type === 'pointerdown')
  const events = []
  const unsubscribe = env.api.subscribeSummonBar(state => events.push(state))
  listener.listener({ button: 0, clientX: 20, clientY: 20 })
  assert.equal(events.at(-1).open, true)
  assert.equal(events.at(-1).origin, 'pet')
  unsubscribe()
  env.dispose()
})

test('the pane claim status is honest about armed / no in-window pet', async () => {
  const rect = { left: 10, top: 10, width: 32, height: 32, right: 42, bottom: 42 }
  const withCanvas = await loadPlugin({
    host: defaultHost(),
    rest: async requestPath => requestPath === '/config'
      ? { summon: { pet_click_claim: true } }
      : { ok: true, enabled: true },
    document: makeDocument({ canvas: { getBoundingClientRect: () => rect } })
  })
  assert.equal(withCanvas.api.petClaimStatusText(), 'Armed — click your pet (in-window)')
  withCanvas.api.setPetClickClaim(false)
  assert.equal(withCanvas.api.petClaimStatusText(), 'Off — use Cmd+Shift+A')
  withCanvas.dispose()

  const withoutCanvas = await loadPlugin({
    host: defaultHost(),
    rest: async requestPath => requestPath === '/config'
      ? { summon: { pet_click_claim: true } }
      : { ok: true, enabled: true },
    document: makeDocument({ canvas: null })
  })
  assert.equal(withoutCanvas.api.petClaimStatusText(),
    'No in-window pet — use Cmd+Shift+A')
  withoutCanvas.dispose()
})

test('unroutable session surfaces the gateway detail and still runs the /butler fallback',
  async () => {
    const submitted = []
    const notifications = []
    const host = defaultHost({
      request: async method => method === 'session.list'
        ? [{ id: 's1', title: 'Butler', created_at: 1 }]
        : [],
      notify(notification) { notifications.push(notification) },
      composer: { submit: (id, text) => { submitted.push({ id, text }); return true } }
    })
    const env = await loadPlugin({
      host,
      rest: async (requestPath, opts) => {
        if (requestPath === '/config') return { summon: { pet_click_claim: false } }
        if (requestPath === '/summon/claim') return { ok: true, enabled: false }
        if (requestPath === '/summon/send') {
          return {
            ok: false,
            reason: 'unroutable_session',
            detail: 'no gateway session route for s1'
          }
        }
        return {}
      },
      document: makeDocument()
    })

    const outcome = await env.api.summonFromBar('make a guide')

    assert.equal(outcome.result.ok, true)
    assert.equal(outcome.result.fallback, true)
    assert.equal(outcome.result.injection_reason, 'unroutable_session')
    assert.deepEqual(submitted, [{ id: 's1', text: '/butler make a guide' }])
    const toast = notifications.find(notification => notification.kind === 'warning')
    assert.ok(toast)
    assert.match(toast.message, /session not routable through the gateway/)
    assert.match(toast.message, /no gateway session route for s1/)
    env.dispose()
  })

test('a successful injection keeps the gateway routing key for debugging', async () => {
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 'local-abc', title: 'Butler', created_at: 1 }]
      : []
  })
  const env = await loadPlugin({
    host,
    rest: async (requestPath, opts) => {
      if (requestPath === '/config') return { summon: { pet_click_claim: false } }
      if (requestPath === '/summon/claim') return { ok: true, enabled: false }
      if (requestPath === '/summon/send') {
        return { ok: true, session: opts.body.session_id, session_key: 'agent:main:local:dm:local-abc' }
      }
      return {}
    },
    document: makeDocument()
  })

  const result = await env.api.sendSummonInjection('local-abc', 'fix it')

  assert.equal(result.ok, true)
  assert.equal(result.session_key, 'agent:main:local:dm:local-abc')
  env.dispose()
})

test('summon bar injects into the existing destination and shows the success toast', async () => {
  const notifications = []
  const injected = []
  const submitted = []
  const host = defaultHost({
    request: async method => method === 'session.list'
      ? [{ id: 's1', title: 'Butler', created_at: 1 }]
      : [],
    notify(notification) { notifications.push(notification) },
    composer: { submit: (id, text) => { submitted.push({ id, text }); return true } }
  })
  const env = await loadPlugin({
    host,
    rest: async (requestPath, opts) => {
      if (requestPath === '/summon/send') {
        injected.push({ text: opts.body.text, session_id: opts.body.session_id })
        return { ok: true, session: opts.body.session_id }
      }
      return {}
    },
    document: makeDocument()
  })

  const outcome = await env.api.summonFromBar('make a guide')

  assert.equal(outcome.result.ok, true)
  assert.equal(outcome.result.send, 'inject')
  assert.deepEqual(injected, [{ text: 'make a guide', session_id: 's1' }])
  assert.deepEqual(submitted, [], 'the chip path is only the fallback')
  assert.ok(notifications.some(notification => notification.kind === 'success'
    && notification.message === 'Summoned → Butler chat (existing)'))
})

test('new destination mints an UNTITLED fresh id, retitles, and posts that exact id', async () => {
  const clock = makeClock()
  const calls = []
  const opened = []
  const injected = []
  const submitted = []
  const notifications = []
  const host = defaultHost({
    state: { profile: { get: () => 'default' } },
    request: async (method, params) => {
      calls.push({ method, params: { ...(params || {}) } })
      if (method === 'session.list') {
        return [{ id: 'oldest', session_id: 'oldest', title: 'Butler', created_at: 1 }]
      }
      if (method === 'session.create') {
        return { session_id: 'runtime-new', stored_session_id: 'stored-new' }
      }
      if (method === 'session.title') return {}
      throw new Error(`unexpected request ${method}: ${JSON.stringify(params)}`)
    },
    openSession: async id => { opened.push(id); return true },
    notify(notification) { notifications.push(notification) },
    composer: {
      submit: (id, text) => { submitted.push({ id, text }); return id === 'stored-new' },
      setDraft: async () => false
    }
  })
  const env = await loadPlugin({
    host,
    rest: async (requestPath, opts) => {
      if (requestPath === '/summon/send') {
        injected.push({ text: opts.body.text, session_id: opts.body.session_id })
        return {
          ok: true,
          session: opts.body.session_id,
          session_key: 'agent:main:local:dm:stored-new'
        }
      }
      return {}
    },
    document: makeDocument()
  })

  const outcome = await env.api.summonFromBar('fresh start', {
    destination: 'new',
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(outcome.result.ok, true)
  assert.equal(outcome.result.destination, 'new')
  assert.equal(outcome.result.send, 'inject')
  assert.equal(outcome.result.session_key, 'agent:main:local:dm:stored-new')

  // Exact RPC params, in order: list -> untitled create -> free old title ->
  // set the fresh title. No title rides on session.create.
  assert.deepEqual(calls.map(call => call.method), [
    'session.list', 'session.create', 'session.title', 'session.title'
  ])
  assert.deepEqual(calls[0].params, { limit: 200 })
  assert.deepEqual(calls[1].params, {}, 'session.create must be UNTITLED')
  assert.deepEqual(calls[2].params, { session_id: 'oldest', title: 'Butler (previous)' })
  assert.deepEqual(calls[3].params, { session_id: 'runtime-new', title: 'Butler' })

  // The fresh id is the key, not the title: it differs from the oldest chat
  // and every subsequent action uses its stored value.
  assert.notEqual(outcome.result.session, 'oldest')
  assert.equal(outcome.result.session, 'stored-new')
  assert.deepEqual(opened, ['stored-new'])
  assert.deepEqual(injected, [{ text: 'fresh start', session_id: 'stored-new' }])
  assert.ok(!injected.some(body => body.session_id === 'oldest'))
  assert.deepEqual(submitted, [])
  assert.ok(notifications.some(notification => notification.kind === 'success'
    && notification.message === 'Summoned → fresh Butler session'))
  env.dispose()
})

test('fresh flow only lists sessions once, before the untitled create', async () => {
  const clock = makeClock()
  const calls = []
  const host = defaultHost({
    state: { profile: { get: () => 'better-butler' } },
    request: async (method, params) => {
      calls.push({ method, params })
      if (method === 'session.list') return []
      if (method === 'session.create') return { session_id: 'runtime-x', stored_session_id: 'stored-x' }
      if (method === 'session.title') return {}
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async () => true
  })
  const env = await loadPlugin({
    host,
    rest: async requestPath => requestPath === '/summon/send'
      ? { ok: true, session: 'stored-x', session_key: 'agent:better-butler:local:dm:stored-x' }
      : {},
    document: makeDocument()
  })

  const outcome = await env.api.summonFromBar('one list only', {
    destination: 'new', now: clock.now, wait: clock.wait
  })

  assert.equal(outcome.result.ok, true)
  assert.equal(calls.filter(call => call.method === 'session.list').length, 1,
    'the fresh flow lists once to find old titles; the fallback never re-reads')
  assert.ok(!calls.some(call => call.method === 'session.create' && 'title' in (call.params || {})),
    'no title is sent to session.create')
  env.dispose()
})

test('fresh creation failure surfaces the true reason and never reuses the oldest', async () => {
  const clock = makeClock()
  const requested = []
  const opened = []
  const injected = []
  const submitted = []
  const notifications = []
  const host = defaultHost({
    request: async (method, params) => {
      requested.push(method)
      if (method === 'session.list') return [{ id: 'oldest', title: 'Butler', created_at: 1 }]
      if (method === 'session.create') throw new Error('session cap reached')
      throw new Error(`unexpected request ${method}: ${JSON.stringify(params)}`)
    },
    openSession: async id => { opened.push(id); return true },
    notify(notification) { notifications.push(notification) },
    composer: { submit: (id, text) => { submitted.push({ id, text }); return true } }
  })
  const env = await loadPlugin({
    host,
    rest: async (requestPath, opts) => {
      if (requestPath === '/summon/send') {
        injected.push({ text: opts.body.text, session_id: opts.body.session_id })
        return { ok: true, session: opts.body.session_id }
      }
      return {}
    },
    document: makeDocument()
  })

  const outcome = await env.api.summonFromBar('fresh start', {
    destination: 'new',
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(outcome.result.ok, false)
  assert.equal(outcome.result.destination, 'new')
  assert.equal(outcome.result.reason, 'session_create_failed')
  assert.match(outcome.result.error.message, /session cap reached/)
  assert.ok(!requested.includes('session.activate'), 'never focuses the oldest on a failed create')
  assert.deepEqual(opened, [])
  assert.deepEqual(injected, [], 'nothing is injected into the oldest')
  assert.deepEqual(submitted, [], 'no composer fallback can silently reach the oldest')
  const toast = notifications.find(notification => notification.kind === 'error')
  assert.ok(toast)
  assert.match(toast.message, /could not create a fresh Butler session/)
  assert.match(toast.message, /session cap reached/)
})

test('fresh-denied fallback stays on the new id and never re-reads the oldest', async () => {
  const clock = makeClock()
  const listCalls = []
  const opened = []
  const submitted = []
  const notifications = []
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.list') {
        listCalls.push({ ...(params || {}) })
        return [{ id: 'oldest', title: 'Butler', created_at: 1 }]
      }
      if (method === 'session.create') return { session_id: 'runtime-new', stored_session_id: 'stored-new' }
      if (method === 'session.title') return {}
      throw new Error(`unexpected request ${method}: ${JSON.stringify(params)}`)
    },
    openSession: async id => { opened.push(id); return true },
    notify(notification) { notifications.push(notification) },
    composer: { submit: (id, text) => { submitted.push({ id, text }); return id === 'stored-new' } }
  })
  const env = await loadPlugin({
    host,
    rest: async requestPath => requestPath === '/summon/send'
      ? { ok: false, reason: 'injection_denied' }
      : {},
    document: makeDocument()
  })

  const outcome = await env.api.summonFromBar('fresh fallback', {
    destination: 'new',
    now: clock.now,
    wait: clock.wait
  })

  assert.equal(outcome.result.ok, true)
  assert.equal(outcome.result.fallback, true)
  assert.equal(outcome.result.session, 'stored-new')
  assert.deepEqual(submitted, [{ id: 'stored-new', text: '/butler fresh fallback' }])
  assert.deepEqual(listCalls, [{ limit: 200 }],
    'one title scan at create; the fresh fallback itself never re-derives the oldest chat')
  assert.deepEqual(opened, ['stored-new'])
  const toast = notifications.find(notification => notification.kind === 'warning')
  assert.ok(toast)
  assert.match(toast.message, /could not host the full conversation/)
})

test('a second fresh chat frees every prior Butler title before claiming it', async () => {
  const titleCalls = []
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.create') return { session_id: 'runtime-3', stored_session_id: 'stored-3' }
      if (method === 'session.title') { titleCalls.push({ ...params }); return {} }
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async () => true
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const created = await env.api.createButlerSession([
    { id: 'old-b', title: 'Butler', created_at: 2 },
    { id: 'old-a', title: 'Butler', created_at: 1 }
  ])

  assert.equal(created.ok, true)
  assert.equal(created.target, 'stored-3')
  // Oldest first, then the fresh id last: the new chat owns the clean title.
  assert.deepEqual(titleCalls, [
    { session_id: 'old-a', title: 'Butler (previous)' },
    { session_id: 'old-b', title: 'Butler (previous 2)' },
    { session_id: 'runtime-3', title: 'Butler' }
  ])
  env.dispose()
})

test('a revision-fenced title rename primes quietly with a resume and retries', async () => {
  const calls = []
  let titleAttempts = 0
  const host = defaultHost({
    request: async (method, params) => {
      calls.push({ method, params: { ...(params || {}) } })
      if (method === 'session.create') {
        return { session_id: 'runtime-9', stored_session_id: 'stored-9' }
      }
      if (method === 'session.resume') return { session_id: 'oldest', revision: 7, messages: [] }
      if (method === 'session.title') {
        titleAttempts += 1
        if (titleAttempts === 1) {
          throw new Error('Session revision unavailable; reopen the session before editing metadata')
        }
        return {}
      }
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async () => true
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const created = await env.api.createButlerSession([
    { id: 'oldest', title: 'Butler', created_at: 1 }
  ])

  assert.equal(created.ok, true)
  assert.deepEqual(calls.map(call => call.method), [
    'session.create', 'session.title', 'session.resume', 'session.title', 'session.title'
  ])
  assert.deepEqual(calls[1].params, { session_id: 'oldest', title: 'Butler (previous)' })
  assert.deepEqual(calls[2].params, { session_id: 'oldest', omit_messages: true })
  assert.deepEqual(calls[3].params, { session_id: 'oldest', title: 'Butler (previous)' })
  assert.deepEqual(calls[4].params, { session_id: 'runtime-9', title: 'Butler' })
  env.dispose()
})

test('a create that reuses the oldest id is refused, never sent', async () => {
  const titleCalls = []
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.create') return { session_id: 'oldest', stored_session_id: 'oldest' }
      if (method === 'session.title') { titleCalls.push({ ...params }); return {} }
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async () => true
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const created = await env.api.createButlerSession([
    { id: 'oldest', title: 'Butler', created_at: 1 }
  ])

  assert.equal(created.ok, false)
  assert.equal(created.reason, 'session_create_reused')
  assert.deepEqual(titleCalls, [], 'a reused id must not be retitled')
  env.dispose()
})

test('fresh send failure toast names the wrong gateway session key', async () => {
  const clock = makeClock()
  const notifications = []
  const host = defaultHost({
    state: { profile: { get: () => 'default' } },
    request: async method => {
      if (method === 'session.list') return [{ id: 'oldest', title: 'Butler', created_at: 1 }]
      if (method === 'session.create') return { session_id: 'runtime-new', stored_session_id: 'stored-new' }
      if (method === 'session.title') return {}
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async () => true,
    notify(notification) { notifications.push(notification) }
  })
  const env = await loadPlugin({
    host,
    rest: async requestPath => requestPath === '/summon/send'
      ? { ok: true, session: 'oldest', session_key: 'agent:main:local:dm:oldest' }
      : {},
    document: makeDocument()
  })

  const outcome = await env.api.summonFromBar('fresh start', {
    destination: 'new', now: clock.now, wait: clock.wait
  })

  assert.equal(outcome.result.ok, false)
  assert.equal(outcome.result.reason, 'session_key_mismatch')
  assert.equal(outcome.result.session_key, 'agent:main:local:dm:oldest')
  assert.equal(outcome.result.expected_session_key, 'agent:main:local:dm:stored-new')
  assert.equal(outcome.clearText, false, 'the typed text stays for a retry')
  assert.equal(outcome.dismiss, false)
  const toast = notifications.find(notification => notification.kind === 'error')
  assert.ok(toast)
  assert.match(toast.message, /wrong session/)
  assert.match(toast.message, /agent:main:local:dm:oldest/)
  assert.match(toast.message, /agent:main:local:dm:stored-new/)
  env.dispose()
})

test('session key namespaces follow the gateway default-profile rule', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })

  assert.equal(env.api.sessionKeyNamespace(null), 'main')
  assert.equal(env.api.sessionKeyNamespace('default'), 'main')
  assert.equal(env.api.sessionKeyNamespace('main'), 'main~')
  assert.equal(env.api.sessionKeyNamespace('better-butler'), 'better-butler')
  assert.equal(env.api.expectedSummonSessionKey('default', 'local-abc'),
    'agent:main:local:dm:local-abc')
  assert.equal(env.api.expectedSummonSessionKey('work', 'local-def'),
    'agent:work:local:dm:local-def')
  env.dispose()
})

test('the pane status row always names claim and in-app pet, and flashes once', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })

  assert.equal(env.api.petStatusRowText({ claim: false, petFound: false }),
    'claim: off · pet (in-app): not found')
  assert.equal(env.api.petStatusRowText({ claim: true, petFound: false }),
    'claim: on · pet (in-app): not found')
  assert.equal(env.api.petStatusRowText({ claim: false, petFound: true }),
    'claim: off · pet (in-app): found')
  assert.equal(env.api.petStatusRowText({ claim: true, petFound: true }),
    'claim: on · pet (in-app): found')
  assert.equal(env.api.petStatusRowText({ claim: true, petFound: true, flash: true }),
    'claim: on · pet (in-app): found · → bar opened')
  env.dispose()
})

test('a pet-origin open publishes a new click sequence; dismiss and shortcut do not', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })
  const states = []
  const unsubscribe = env.api.subscribeSummonBar(state => states.push(state))
  const start = states.at(-1).petClickSeq

  env.api.openSummonBar('pet')
  assert.equal(states.at(-1).open, true)
  assert.equal(states.at(-1).origin, 'pet')
  assert.equal(states.at(-1).petClickSeq, start + 1, 'a pet click bumps the flash sequence')

  env.api.dismissSummonBar()
  assert.equal(states.at(-1).open, false)
  assert.equal(states.at(-1).petClickSeq, start + 1, 'dismiss is not a click')

  env.api.openSummonBar('shortcut')
  assert.equal(states.at(-1).petClickSeq, start + 1, 'the shortcut is not a pet click')

  unsubscribe()
  env.dispose()
})

test('a click while the claim is off never opens the bar', async () => {
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

  // A watcher can be alive before the authoritative config poll turns it on.
  env.api.startPetClickWatcher()
  const listener = document.listeners.find(entry => entry.type === 'pointerdown').listener
  listener({ button: 0, clientX: 80, clientY: 100 })
  assert.equal(events.at(-1).open, false, 'claim off ignores the pet click')

  env.api.setPetClickClaim(true)
  listener({ button: 0, clientX: 80, clientY: 100 })
  assert.equal(events.at(-1).open, true, 'claim on opens the bar')
  assert.equal(events.at(-1).origin, 'pet')

  unsubscribe()
  env.dispose()
})

test('the in-app pet readout re-queries the canvas instead of trusting a stale flag', async () => {
  let canvas = null
  const document = makeDocument({ canvas: () => canvas })
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async () => ({ summon: { pet_click_claim: true } }),
    document
  })

  assert.equal(env.api.petClaimStatusText(), 'No in-window pet — use Cmd+Shift+A')
  canvas = { getBoundingClientRect: () => ({ left: 1, top: 1, width: 8, height: 8, right: 9, bottom: 9 }) }
  assert.equal(env.api.petClaimStatusText(), 'Armed — click your pet (in-window)',
    'the status query sees a canvas that appeared after register')
  env.dispose()
})

test('destination choice persists across plugin loads in localStorage', async () => {
  const storage = makeStorage()
  const first = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), storage
  })
  assert.equal(first.api.readSummonDestination(), 'existing')
  assert.equal(first.api.butlerCommandText('fix it'), '/butler fix it')
  assert.equal(first.api.butlerCommandText('/butler already'), '/butler already')

  first.api.setSummonDestination('new')
  assert.equal(storage.values.get('butler.summon.destination'), 'new')
  first.dispose()

  const second = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), storage
  })
  assert.equal(second.api.getSummonDestination(), 'new')
  assert.equal(second.api.readSummonDestination(), 'new')
  second.api.setSummonDestination('existing')
  assert.equal(storage.values.get('butler.summon.destination'), 'existing')
  second.dispose()
})
