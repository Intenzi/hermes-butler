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
  'sendSummonInjection', 'sendSummonRpc', 'configPetClickClaim',
  'syncCompanionClaim', 'petStatusRowText',
  'butlerCommandText',
  'openButlerSession', 'createButlerSession',
  'sessionTargetId',
  'startSummonPendingPolling', 'pollSummonPending',
  'petSpeechBeat', 'speakButlerAnswer', 'petStatusLine', 'watchStatusLine',
  'watchIsActive', 'watchEndedLine', 'watchPaneText', 'WatchRow', 'stopWatch',
  'watchHitLine', 'speechSnapshot'
]

// The plugin never reads or mutates the host page. The stub keeps a document in
// the vm context so a regression that reaches for one has a defined surface.
function makeDocument() {
  return { activeElement: null }
}

function makeStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    values,
    getItem(key) { return values.has(key) ? values.get(key) : null },
    setItem(key, value) { values.set(key, String(value)) }
  }
}

async function loadPlugin({ host, rest, document, storage, pet = {} }) {
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
    pet,
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

test('the desktop plugin never reaches into the host page DOM', () => {
  // The pet-click receiver was a document-wide capture listener plus a rect
  // follower. Both are gone: the SDK slots and the companion chip own the
  // surfaces now, so the source may not query, append to, or listen on the
  // app's page at all.
  const source = readFileSync(PLUGIN_JS, 'utf8')

  assert.equal(source.includes('document.querySelector'), false)
  assert.equal(source.includes('document.body.appendChild'), false)
  assert.equal(source.includes("addEventListener('pointerdown'"), false)
  assert.equal(source.includes('PET_CANVAS_SELECTORS'), false)
  assert.equal(source.includes('petCanvasNode'), false)
})

test('the web summon bar is gone and the two-door pending send survives', () => {
  const source = readFileSync(PLUGIN_JS, 'utf8')

  // The React bar and its titlebar slot / keybind are gone: the companion's
  // Carbon chord owns ⌘⇧A and the Swift field owns the visible input.
  assert.equal(source.includes('SummonBar'), false, 'no web bar component')
  assert.equal(source.includes("id: 'summon-bar'"), false, 'no titlebar slot')
  assert.equal(source.includes("id: 'butler.summon'"), false, 'no shadowed keybind')
  // The LOOK answer path is the reason the module-scope consumer stays.
  assert.ok(source.includes('startSummonPendingPolling'), 'pending poll is armed')
  // A text row goes out through the injection door first and the RPC door
  // second; the composer ladder that used to sit in front of both is gone.
  assert.match(source, /sendSummonInjection\(\s*targetId,\s*outgoing/,
    'a text pending row is still sent through the injection door')
  assert.match(source, /sendSummonRpc\(targetId, outgoing\)/,
    'the RPC door is reached when injection cannot continue the chat')
  assert.equal(source.includes('sendPendingSummonText'), false,
    'no composer send path survives')
})

test('register arms the companion chip with no config key', async () => {
  const calls = []
  const document = makeDocument()
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, opts) => {
      calls.push({ path: requestPath, opts })
      if (requestPath === '/config') return {}
      if (requestPath === '/summon/claim') {
        return { ok: true, enabled: true, running: true }
      }
      return {}
    },
    document
  })

  const claims = calls.filter(call => call.path === '/summon/claim'
    && call.opts && call.opts.method === 'POST')
  assert.ok(claims.length >= 1, 'register synced the companion chip')
  assert.ok(claims.every(call => call.opts.body.enabled === true),
    'every register-time claim sync asks for the chip to be on')
  env.dispose()
})

test('a config response without the key resolves to on, an explicit false still disarms',
  async () => {
    const env = await loadPlugin({
      host: defaultHost(), rest: async () => ({}), document: makeDocument()
    })

    assert.equal(env.api.configPetClickClaim(null), true)
    assert.equal(env.api.configPetClickClaim({}), true)
    assert.equal(env.api.configPetClickClaim({ summon: {} }), true)
    assert.equal(env.api.configPetClickClaim({ summon: { pet_click_claim: false } }), false)
    env.dispose()
  })

test('config loaded true keeps the companion chip armed without a manual toggle', async () => {
  const calls = []
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, opts) => {
      calls.push({ path: requestPath, opts })
      if (requestPath === '/config') return { summon: { pet_click_claim: true } }
      if (requestPath === '/summon/claim') return { ok: true, enabled: true, running: true }
      return {}
    },
    document: makeDocument()
  })

  const claim = calls.find(call => call.path === '/summon/claim' && call.opts?.method === 'POST')
  assert.ok(claim, 'the companion chip is synced from the config value')
  assert.equal(claim.opts.body.enabled, true)
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

test('an active watch rides the pet line as a count, and the hit once fired', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })

  assert.equal(env.api.petStatusLine({ watch: { app: 'Slack', events: 3 } }),
    '⏲ Watch · Slack · 3 changes')
  assert.equal(env.api.petStatusLine({ watch: { app: 'Slack', events: 1 } }),
    '⏲ Watch · Slack · 1 change')
  assert.equal(env.api.petStatusLine({
    watch: { app: 'Slack', events: 9, hit: { label: 'Export complete' } }
  }), '⏲ Watch · hit: Export complete')
  // A stopped watch (app quit) is no longer a live status line.
  assert.ok(!env.api.petStatusLine({
    watch: { app: 'Slack', events: 9, stopped: { reason: 'app-quit' } }
  }), 'a stopped watch drops the pet line')
  // A live guide instruction outranks the ambient watch line.
  assert.equal(env.api.petStatusLine({
    status: 'running', instruction: 'Click Export',
    watch: { app: 'Slack', events: 2 }
  }), 'Click Export')
  // No watch, no line: the ordinary idle/guide path is unchanged.
  assert.ok(!env.api.petStatusLine({}))
  env.dispose()
})

test('a live watch outranks a dead guide block on the pet line', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })
  const watch = { app: 'Opera GX', events: 596, stopped: null }
  const watchLine = '⏲ Watch · Opera GX · 596 changes'

  // The measured defect: a guide stopped long ago left `stalled` + a note on
  // disk, and the pet kept speaking it over the live watch.
  assert.equal(env.api.petStatusLine({
    status: 'stalled',
    note: 'step 1 unverified for 600s; expect=expectation_unmet',
    instruction: 'Click Settings',
    watch
  }), watchLine, 'a live watch beats a dead stalled guide')
  // A live step still leads the watch while the guide is being walked.
  assert.equal(env.api.petStatusLine({
    status: 'running', instruction: 'Click Export', watch
  }), 'Click Export')
  // Every terminal status is dead weight: never mask the watch line.
  assert.equal(env.api.petStatusLine({
    status: 'stopped', note: 'stopped by agent',
    instruction: 'Click Export', watch
  }), watchLine)
  assert.equal(env.api.petStatusLine({
    status: 'abandoned', note: 'step 1 was never verified',
    instruction: 'Click Export', watch
  }), watchLine)
  assert.equal(env.api.petStatusLine({
    status: 'done', instruction: 'Click Export', watch
  }), watchLine)
  // No watch: the stalled note is unchanged (the guide is the only status).
  assert.equal(env.api.petStatusLine({
    status: 'stalled',
    note: 'step 1 unverified for 600s; expect=expectation_unmet',
    instruction: 'Click Settings'
  }), 'step 1 unverified for 600s; expect=expectation_unmet')
  env.dispose()
})

test('a dead guide step does not steal the watch keep-alive', async () => {
  const said = []
  const pet = {
    say(text, options) { said.push({ text, options }) },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), pet
  })
  const cur = {
    status: 'stalled',
    instruction: 'Click Settings',
    note: 'step 1 unverified for 600s; expect=expectation_unmet',
    watch: { app: 'Opera GX', events: 596, started_at: 1_800_000_000 }
  }

  assert.equal(env.api.petSpeechBeat(cur, 1000), 'status')
  assert.equal(said[0].text, '⏲ Watch · Opera GX · 596 changes')
  assert.equal(said[0].options.ttlMs, 30000)
  // The watch line, not the dead guide's instruction, owns the keep-alive.
  assert.equal(env.api.petSpeechBeat(cur, 26000), 'status')
  assert.equal(said.length, 2)
  env.dispose()
})

test('the pane status row names the chip', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })

  assert.equal(env.api.petStatusRowText({ claim: true }), 'chip: on')
  assert.equal(env.api.petStatusRowText({ claim: false }),
    'chip: off', 'only the stored escape hatch can read off')
  env.dispose()
})

test('register arms the module summon-pending timer next to the config poll', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })

  const timerPeriods = env.intervals.map(entry => entry.ms)
  assert.ok(timerPeriods.includes(5000), 'the config poll is still armed')
  assert.ok(timerPeriods.includes(1500), 'the summon-pending poll is armed at REFRESH_MS')
  env.dispose()
})

test('the module poll acks a text-less legacy row once per unseen pending id', async () => {
  let statePayload = { summon_pending: { id: 'pending-1' } }
  const calls = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') return statePayload
    if (requestPath === '/summon/ack') return { ok: true, cleared: true }
    return {}
  }
  const env = await loadPlugin({ host: defaultHost(), rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)
  assert.equal(calls.filter(call => call.path === '/summon/send').length, 0,
    'a legacy marker row has no surface left and sends nothing')
  let acks = calls.filter(call => call.path === '/summon/ack')
  assert.equal(acks.length, 1)
  assert.equal(acks[0].opts.method, 'POST')
  assert.equal(acks[0].opts.body.id, 'pending-1')

  // The row can survive one poll interval before the ack lands: dedupe by id
  // must not consume it twice.
  assert.equal(await env.api.pollSummonPending(), false)
  assert.equal(calls.filter(call => call.path === '/summon/ack').length, 1)

  // A second click is a new id and is consumed again.
  statePayload = { summon_pending: { id: 'pending-2' } }
  assert.equal(await env.api.pollSummonPending(), true)
  acks = calls.filter(call => call.path === '/summon/ack')
  assert.deepEqual(acks.map(call => call.opts.body.id), ['pending-1', 'pending-2'])

  env.dispose()
})

test('a text pending row sends to the newest Butler chat in place',
  async () => {
    const statePayload = {
      summon_pending: { id: 'pending-text', text: 'turn off wifi', origin: 'chip' }
    }
    const calls = []
    const notifications = []
    const rest = async (requestPath, opts) => {
      calls.push({ path: requestPath, opts })
      if (requestPath === '/state') return statePayload
      if (requestPath === '/summon/send') return { ok: true, session: opts.body.session_id }
      if (requestPath === '/summon/ack') return { ok: true, cleared: true }
      return {}
    }
    const host = defaultHost({
      request: async method => {
        if (method === 'session.list') {
          return [
            { id: 'old-chat', title: 'Butler', created_at: 10 },
            { id: 'new-chat', title: 'Butler', created_at: 900 },
            { id: 'other', title: 'Not Butler', created_at: 999 }
          ]
        }
        throw new Error(`unexpected request ${method}`)
      },
      notify: payload => notifications.push(payload)
    })
    const env = await loadPlugin({ host, rest, document: makeDocument() })

    assert.equal(await env.api.pollSummonPending(), true)

    const sends = calls.filter(call => call.path === '/summon/send')
    assert.equal(sends.length, 1)
    assert.equal(sends[0].opts.method, 'POST')
    assert.equal(sends[0].opts.body.text, 'turn off wifi',
      'injection gets the bare text; the gateway adds the [butler summon] frame')
    assert.equal(sends[0].opts.body.session_id, 'new-chat')
    assert.equal('origin' in sends[0].opts.body, false,
      'plain ask-field rows keep sending without an origin')
    const acks = calls.filter(call => call.path === '/summon/ack')
    assert.deepEqual(acks.map(call => call.opts.body.id), ['pending-text'])
    assert.equal(notifications.length, 1)
    assert.equal(notifications[0].kind, 'success')
    assert.equal(notifications[0].message, 'Sent to Butler')

    env.dispose()
  })

test('a look row frames the control and the question in place', async () => {
  const statePayload = {
    summon_pending: {
      id: 'pending-look',
      origin: 'look',
      text: 'what does this export?',
      destination: 'recent',
      target: {
        app: 'Preview',
        role: 'Button',
        label: 'Export',
        window: 'receipt.pdf',
        doc: 'file:///tmp/receipt.pdf',
        enabled: true,
        box: [10, 20, 30, 40]
      }
    }
  }
  const calls = []
  const notifications = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') return statePayload
    if (requestPath === '/summon/send') return { ok: true, session: opts.body.session_id }
    if (requestPath === '/summon/ack') return { ok: true, cleared: true }
    return {}
  }
  const host = defaultHost({
    request: async method => {
      if (method === 'session.list') {
        return [{ id: 'butler-chat', title: 'Butler', created_at: 900 }]
      }
      throw new Error(`unexpected request ${method}`)
    },
    notify: payload => notifications.push(payload)
  })
  const env = await loadPlugin({ host, rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)

  const sends = calls.filter(call => call.path === '/summon/send')
  assert.equal(sends.length, 1)
  const injected = sends[0].opts.body.text
  assert.equal(sends[0].opts.body.session_id, 'butler-chat')
  assert.equal(sends[0].opts.body.origin, 'look',
    'the route needs the LOOK origin to pick the butler_say frame')
  assert.match(injected, /^\[butler look\] /)
  assert.ok(injected.includes('App: Preview'), 'the app is named')
  assert.ok(injected.includes('Window: receipt.pdf'), 'the window is named')
  assert.ok(
    injected.includes('Control: Button "Export" (enabled) at 10,20,30,40'),
    'the control renders role, label, enabled state and box')
  assert.ok(injected.includes('\nQuestion: what does this export?'), 'the question is asked')
  assert.deepEqual(
    calls.filter(call => call.path === '/summon/ack').map(call => call.opts.body.id),
    ['pending-look'])
  assert.equal(notifications.length, 1)
  assert.equal(notifications[0].message, 'Asked about that control')

  env.dispose()
})

test('a look row omits parts the companion could not read', async () => {
  const statePayload = {
    summon_pending: {
      id: 'pending-look-thin',
      origin: 'look',
      text: 'what now?',
      target: { app: 'Finder' }
    }
  }
  const calls = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') return statePayload
    if (requestPath === '/summon/send') return { ok: true, session: opts.body.session_id }
    if (requestPath === '/summon/ack') return { ok: true, cleared: true }
    return {}
  }
  const host = defaultHost({
    request: async method => {
      if (method === 'session.list') {
        return [{ id: 'butler-chat', title: 'Butler', created_at: 900 }]
      }
      throw new Error(`unexpected request ${method}`)
    }
  })
  const env = await loadPlugin({ host, rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)

  const injected = calls.find(call => call.path === '/summon/send').opts.body.text
  assert.equal(injected, '[butler look] App: Finder\nQuestion: what now?')
  env.dispose()
})

test('a destination:"new" row mints a fresh chat and posts to that id', async () => {
  const statePayload = {
    summon_pending: {
      id: 'pending-new', text: 'start something new', origin: 'chip', destination: 'new'
    }
  }
  const calls = []
  const requests = []
  const notifications = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') return statePayload
    if (requestPath === '/summon/send') return { ok: true, session: opts.body.session_id }
    if (requestPath === '/summon/ack') return { ok: true, cleared: true }
    return {}
  }
  const host = defaultHost({
    request: async (method, opts) => {
      requests.push({ method, opts })
      if (method === 'session.list') {
        return [
          { id: 'old-chat', title: 'Butler', created_at: 10 },
          { id: 'new-chat', title: 'Butler', created_at: 900 }
        ]
      }
      // Untitled create first: a titled create would hand back the old chat.
      if (method === 'session.create') {
        // The opts object comes from the vm realm, so compare keys, not
        // prototypes (deepStrictEqual would reject the foreign prototype).
        assert.equal(Object.keys(opts).length, 0, 'create must be untitled')
        return { session_id: 'fresh-runtime', stored_session_id: 'fresh-stored' }
      }
      if (method === 'session.title') return { ok: true }
      if (method === 'session.activate') return { ok: true }
      throw new Error(`unexpected request ${method}`)
    },
    openSession: async id => {
      requests.push({ method: 'openSession', id })
      return true
    },
    notify: payload => notifications.push(payload)
  })
  const env = await loadPlugin({ host, rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)
  const sends = calls.filter(call => call.path === '/summon/send')
  assert.equal(sends.length, 1)
  assert.equal(sends[0].opts.body.text, 'start something new')
  assert.equal(sends[0].opts.body.session_id, 'fresh-stored',
    'the send targets the freshly minted id, not either existing chat')
  assert.ok(!sends.some(call => call.opts.body.session_id === 'new-chat'))
  assert.ok(!sends.some(call => call.opts.body.session_id === 'old-chat'))

  const creates = requests.filter(entry => entry.method === 'session.create')
  assert.equal(creates.length, 1, 'a fresh chat is always minted')
  // The title is claimed for the new chat after freeing it from the old ones.
  const titles = requests.filter(entry => entry.method === 'session.title')
  assert.deepEqual(
    titles.map(entry => [entry.opts.session_id, entry.opts.title]).at(-1),
    ['fresh-runtime', 'Butler'])
  // The whole point of the field: no window may be raised.
  assert.equal(requests.filter(entry => entry.method === 'session.activate').length, 0)
  assert.equal(requests.filter(entry => entry.method === 'openSession').length, 0)

  const acks = calls.filter(call => call.path === '/summon/ack')
  assert.deepEqual(acks.map(call => call.opts.body.id), ['pending-new'])
  assert.equal(notifications.length, 1)
  assert.equal(notifications[0].kind, 'success')
  assert.equal(notifications[0].message, 'Started a new Butler chat')

  env.dispose()
})

test('a destination:"recent" row posts to the newest chat and never creates', async () => {
  const statePayload = {
    summon_pending: {
      id: 'pending-recent', text: 'keep going', origin: 'chip', destination: 'recent'
    }
  }
  const calls = []
  const requests = []
  const notifications = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') return statePayload
    if (requestPath === '/summon/send') return { ok: true, session: opts.body.session_id }
    if (requestPath === '/summon/ack') return { ok: true, cleared: true }
    return {}
  }
  const host = defaultHost({
    request: async (method, opts) => {
      requests.push({ method, opts })
      if (method === 'session.list') {
        return [
          { id: 'old-chat', title: 'Butler', created_at: 10 },
          { id: 'new-chat', title: 'Butler', created_at: 900 },
          { id: 'other', title: 'Not Butler', created_at: 999 }
        ]
      }
      throw new Error(`unexpected request ${method}`)
    },
    notify: payload => notifications.push(payload)
  })
  const env = await loadPlugin({ host, rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)

  const sends = calls.filter(call => call.path === '/summon/send')
  assert.equal(sends.length, 1)
  assert.equal(sends[0].opts.body.session_id, 'new-chat')
  assert.equal(requests.filter(entry => entry.method === 'session.create').length, 0,
    'recent must never mint a chat while one exists')
  assert.equal(notifications.length, 1)
  assert.equal(notifications[0].message, 'Sent to Butler')

  env.dispose()
})

test('a failed fresh-chat create reports session_create_failed and sends nothing',
  async () => {
    const statePayload = {
      summon_pending: {
        id: 'pending-no-create', text: 'do not fall back', destination: 'new'
      }
    }
    const calls = []
    const notifications = []
    const rest = async (requestPath, opts) => {
      calls.push({ path: requestPath, opts })
      if (requestPath === '/state') return statePayload
      if (requestPath === '/summon/ack') return { ok: true, cleared: true }
      return {}
    }
    const host = defaultHost({
      request: async method => {
        if (method === 'session.list') return [{ id: 'new-chat', title: 'Butler' }]
        if (method === 'session.create') throw new Error('gateway said no')
        throw new Error(`unexpected request ${method}`)
      },
      notify: payload => notifications.push(payload)
    })
    const env = await loadPlugin({ host, rest, document: makeDocument() })

    assert.equal(await env.api.pollSummonPending(), true)

    assert.equal(calls.filter(call => call.path === '/summon/send').length, 0,
      'a failed "new" must never silently use an existing chat')
    assert.equal(notifications.length, 1)
    assert.equal(notifications[0].kind, 'error')
    assert.match(notifications[0].message, /session_create_failed/)
    assert.deepEqual(
      calls.filter(call => call.path === '/summon/ack').map(call => call.opts.body.id),
      ['pending-no-create'])

    env.dispose()
  })

test('a text-less pending row is consumed and posts no send', async () => {
  const calls = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') return { summon_pending: { id: 'pending-legacy' } }
    if (requestPath === '/summon/ack') return { ok: true, cleared: true }
    return {}
  }
  const env = await loadPlugin({ host: defaultHost(), rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)

  assert.equal(calls.filter(call => call.path === '/summon/send').length, 0)
  assert.deepEqual(calls.filter(call => call.path === '/summon/ack')
    .map(call => call.opts.body.id), ['pending-legacy'])

  env.dispose()
})

test('a failed in-place send still acks the row and notifies the reason', async () => {
  const calls = []
  const requests = []
  const notifications = []
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') {
      return { summon_pending: { id: 'pending-fail', text: 'hi' } }
    }
    if (requestPath === '/summon/send') throw new Error('gateway down')
    if (requestPath === '/summon/ack') return { ok: true }
    return {}
  }
  const host = defaultHost({
    request: async (method, props) => {
      requests.push({ method, props })
      if (method === 'session.list') return [{ id: 'chat-1', title: 'Butler', created_at: 1 }]
      if (method === 'session.resume') return { session_id: props.session_id }
      // The second door takes the turn but hands back no admission receipt.
      if (method === 'prompt.submit') return {}
      return []
    },
    notify: payload => notifications.push(payload)
  })
  const env = await loadPlugin({ host, rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)

  // Injection failed, so the RPC door was tried for the same chat.
  const submit = requests.find(entry => entry.method === 'prompt.submit')
  assert.ok(submit, 'the second door carried the text when injection failed')
  assert.equal(submit.props.session_id, 'chat-1')
  assert.equal(submit.props.text, '/butler hi', 'the failover keeps the butler frame')
  assert.deepEqual(calls.filter(call => call.path === '/summon/ack')
    .map(call => call.opts.body.id), ['pending-fail'])
  assert.equal(notifications.length, 1)
  assert.equal(notifications[0].kind, 'error')
  assert.match(notifications[0].message, /gateway down/,
    'the toast names the transport failure, not only the last empty receipt')
  env.dispose()
})

test('a failing /state never throws out of the module poll', async () => {
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async requestPath => {
      if (requestPath === '/state') throw new Error('backend down')
      return {}
    },
    document: makeDocument()
  })

  assert.equal(await env.api.pollSummonPending(), false)
  env.dispose()
})

test('a failed ack still consumes the row and never throws', async () => {
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async requestPath => {
      if (requestPath === '/state') return { summon_pending: { id: 'pending-x' } }
      if (requestPath === '/summon/ack') throw new Error('ack down')
      return {}
    },
    document: makeDocument()
  })

  assert.equal(await env.api.pollSummonPending(), true)

  env.dispose()
})

// ----------------------------------------------------- tracked chat ids

function trackedIds(storage) {
  const raw = storage.values.get('butler.chat.ids')
  return raw ? JSON.parse(raw) : []
}

function pendingTextRest(sends, { destination = 'recent' } = {}) {
  return async (requestPath, opts) => {
    if (requestPath === '/state') {
      return {
        summon_pending: {
          id: `pending-${destination}`, text: 'keep the guide open', origin: 'chip', destination
        }
      }
    }
    if (requestPath === '/summon/send') {
      sends.push({ text: opts.body.text, session_id: opts.body.session_id })
      return { ok: true, session: opts.body.session_id }
    }
    if (requestPath === '/summon/ack') return { ok: true, cleared: true }
    return {}
  }
}

test('a tracked id is chosen even when NO chat still carries the Butler title', async () => {
  // The reported failure: the gateway renamed the chat from its content, so the
  // exact-title rule found nothing and every send created a new chat. The id is
  // the identity and must win without any title match.
  const storage = makeStorage({ 'butler.chat.ids': JSON.stringify(['chat-42']) })
  const sends = []
  const requests = []
  const host = defaultHost({
    request: async (method, params) => {
      requests.push({ method, params })
      if (method === 'session.list') {
        return [{ id: 'chat-42', title: 'Fixing the wifi', updated_at: 900 }]
      }
      throw new Error(`unexpected request ${method}: ${JSON.stringify(params)}`)
    }
  })
  const env = await loadPlugin({
    host, rest: pendingTextRest(sends), document: makeDocument(), storage
  })

  assert.equal(await env.api.pollSummonPending(), true)

  assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'chat-42' }])
  assert.ok(!requests.some(entry => entry.method === 'session.create'),
    'a renamed tracked chat must stop the create-every-send fallback')
  assert.deepEqual(trackedIds(storage), ['chat-42'])
  env.dispose()
})

test('tracked ids whose sessions are gone are skipped; the next tracked id wins', async () => {
  const storage = makeStorage({
    'butler.chat.ids': JSON.stringify(['gone-chat', 'alive-new', 'alive-old'])
  })
  const sends = []
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.list') {
        // List order is irrelevant: the tracked order decides which chat is
        // "recent", and a missing id is pruned only because it is confirmed absent.
        return [
          { id: 'alive-old', title: 'Older guide', updated_at: 10 },
          { id: 'alive-new', title: 'Newer guide', updated_at: 900 }
        ]
      }
      throw new Error(`unexpected request ${method}: ${JSON.stringify(params)}`)
    }
  })
  const env = await loadPlugin({
    host, rest: pendingTextRest(sends), document: makeDocument(), storage
  })

  assert.equal(await env.api.pollSummonPending(), true)

  assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'alive-new' }])
  assert.deepEqual(trackedIds(storage), ['alive-new', 'alive-old'],
    'the confirmed-absent id is pruned, the survivors keep their order')
  env.dispose()
})

test('nothing tracked and no title creates, titles, remembers, and sends to the fresh id',
  async () => {
    const storage = makeStorage()
    const sends = []
    const titleCalls = []
    const host = defaultHost({
      request: async (method, params) => {
        if (method === 'session.list') {
          return [{ id: 'unrelated', title: 'An unrelated chat', updated_at: 5 }]
        }
        if (method === 'session.create') {
          assert.equal(Object.keys(params || {}).length, 0, 'the create stays untitled')
          return { session_id: 'runtime-fresh', stored_session_id: 'stored-fresh' }
        }
        if (method === 'session.title') {
          titleCalls.push({ session_id: params.session_id, title: params.title })
          return {}
        }
        throw new Error(`unexpected request ${method}: ${JSON.stringify(params)}`)
      }
    })
    const env = await loadPlugin({
      host, rest: pendingTextRest(sends), document: makeDocument(), storage
    })

    assert.equal(await env.api.pollSummonPending(), true)

    assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'stored-fresh' }])
    assert.deepEqual(titleCalls, [{ session_id: 'runtime-fresh', title: 'Butler' }],
      'only the fresh chat is titled; the unrelated chat is never renamed')
    assert.deepEqual(trackedIds(storage), ['stored-fresh'],
      'the routed stored id is remembered for the next send')
    env.dispose()
  })

test('destination:"new" creates and prepends the fresh id to the tracked list', async () => {
  const storage = makeStorage({
    'butler.chat.ids': JSON.stringify(['tracked-old', 'tracked-older'])
  })
  const sends = []
  const titleCalls = []
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.list') {
        return [{ id: 'tracked-old', title: 'Butler', updated_at: 900 }]
      }
      if (method === 'session.create') {
        return { session_id: 'runtime-new', stored_session_id: 'stored-new' }
      }
      if (method === 'session.title') {
        titleCalls.push({ session_id: params.session_id, title: params.title })
        return {}
      }
      throw new Error(`unexpected request ${method}: ${JSON.stringify(params)}`)
    }
  })
  const env = await loadPlugin({
    host,
    rest: pendingTextRest(sends, { destination: 'new' }),
    document: makeDocument(),
    storage
  })

  assert.equal(await env.api.pollSummonPending(), true)

  assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'stored-new' }])
  assert.deepEqual(trackedIds(storage), ['stored-new', 'tracked-old', 'tracked-older'],
    'the fresh id is newest, the old ids are untouched')
  assert.deepEqual(titleCalls.at(-1), { session_id: 'runtime-new', title: 'Butler' })
  env.dispose()
})

test('a failed session.list leaves the tracked list intact and still uses its newest id',
  async () => {
    const storage = makeStorage({
      'butler.chat.ids': JSON.stringify(['tracked-only', 'tracked-older'])
    })
    const sends = []
    const host = defaultHost({
      request: async method => {
        if (method === 'session.list') throw new Error('gateway list unavailable')
        throw new Error(`unexpected request ${method}`)
      }
    })
    const env = await loadPlugin({
      host, rest: pendingTextRest(sends), document: makeDocument(), storage
    })

    assert.equal(await env.api.pollSummonPending(), true)

    // A transient list failure cannot confirm absence, so nothing is pruned and
    // no duplicate chat is minted; the newest known id still receives the send.
    assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'tracked-only' }])
    assert.deepEqual(trackedIds(storage), ['tracked-only', 'tracked-older'])
    env.dispose()
  })

test('the legacy exact-title chat is remembered once found', async () => {
  // Rule 2: nothing tracked yet, but a chat still titled Butler. It is found by
  // title once, verified through the session list, and remembered so the next
  // send is id-first even after the gateway renames it.
  const storage = makeStorage()
  const sends = []
  const requests = []
  const host = defaultHost({
    request: async method => {
      requests.push(method)
      if (method === 'session.list') {
        return [{ id: 'legacy-butler', title: 'Butler', updated_at: 10 }]
      }
      throw new Error(`unexpected request ${method}`)
    }
  })
  const env = await loadPlugin({
    host, rest: pendingTextRest(sends), document: makeDocument(), storage
  })

  assert.equal(await env.api.pollSummonPending(), true)

  assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'legacy-butler' }])
  assert.deepEqual(trackedIds(storage), ['legacy-butler'])
  assert.ok(!requests.includes('session.create'))
  env.dispose()
})

test('a full session.list page does not prune an id it may have truncated away', async () => {
  // A 200-row page is the request limit: absence from it is not confirmed
  // absence, so the stale front id survives instead of being erased.
  const storage = makeStorage({
    'butler.chat.ids': JSON.stringify(['maybe-on-page-2', 'alive-on-page-1'])
  })
  const sends = []
  const rows = Array.from({ length: 200 }, (_, index) => ({
    id: `filler-${index}`, title: `Chat ${index}`, updated_at: index
  }))
  rows[0] = { id: 'alive-on-page-1', title: 'A renamed Butler chat', updated_at: 900 }
  const host = defaultHost({
    request: async method => {
      if (method === 'session.list') return rows
      throw new Error(`unexpected request ${method}`)
    }
  })
  const env = await loadPlugin({
    host, rest: pendingTextRest(sends), document: makeDocument(), storage
  })

  assert.equal(await env.api.pollSummonPending(), true)

  assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'alive-on-page-1' }])
  // The successful send marks the used id newest; the point is that the id the
  // possibly-truncated page could not show was not erased.
  assert.deepEqual(trackedIds(storage), ['alive-on-page-1', 'maybe-on-page-2'],
    'a possibly truncated page must not be treated as a full inventory')
  env.dispose()
})

test('a success-shaped session.list with no rows is not an empty inventory', async () => {
  const storage = makeStorage({
    'butler.chat.ids': JSON.stringify(['tracked-only'])
  })
  const sends = []
  let listCalls = 0
  const host = defaultHost({
    request: async method => {
      if (method === 'session.list') {
        listCalls += 1
        return { ok: true } // no `sessions`: an unreadable inventory shape
      }
      throw new Error(`unexpected request ${method}`)
    }
  })
  const env = await loadPlugin({
    host, rest: pendingTextRest(sends), document: makeDocument(), storage
  })

  assert.equal(await env.api.pollSummonPending(), true)

  assert.equal(listCalls, 1)
  assert.deepEqual(sends, [{ text: 'keep the guide open', session_id: 'tracked-only' }])
  assert.deepEqual(trackedIds(storage), ['tracked-only'], 'an unusable response prunes nothing')
  env.dispose()
})

// --- butler_say: the answer arrives on the pet, not only in the chat ---

test('a new say.id is spoken once with the answer ttl; the same id is silent', async () => {
  const said = []
  const pet = { say(text, options) { said.push({ text, options }) } }
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), pet
  })
  const answer = { id: 'abcd1234', text: 'It exports the page as a PDF.', at: 1 }

  assert.equal(env.api.petSpeechBeat({ say: answer }, 1000), 'answer')
  assert.equal(said.length, 1)
  assert.equal(said[0].text, 'It exports the page as a PDF.')
  assert.equal(said[0].options.tone, 'info')
  assert.equal(said[0].options.ttlMs, 15000)
  assert.equal(said[0].options.id, 'butler-answer')

  // The same id twice says it once, and holds the channel for its ttl.
  assert.equal(env.api.petSpeechBeat({ say: answer }, 3000), 'holding')
  assert.equal(env.api.petSpeechBeat({ say: answer }, 15000), 'holding')
  assert.equal(said.length, 1)

  // A new id (a second answer) speaks again.
  assert.equal(env.api.petSpeechBeat(
    { say: { id: 'ffff9999', text: 'The second answer.' } }, 17000), 'answer')
  assert.equal(said.length, 2)
  assert.equal(said[1].text, 'The second answer.')
  assert.equal(said[1].options.ttlMs, 15000)
  env.dispose()
})

test('an answer wins the pet channel for its ttl, then the status line resumes', async () => {
  const said = []
  const pet = { say(text, options) { said.push({ text, options }) } }
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), pet
  })
  const cur = {
    status: 'running',
    note: null,
    instruction: 'Click Keyboard',
    currentStep: 0,
    total: 3,
    verified: 0,
    say: { id: 'abcd1234', text: 'This opens the keyboard settings.' }
  }

  assert.equal(env.api.petSpeechBeat(cur, 1000), 'answer')
  assert.deepEqual(said.map(row => row.text), ['This opens the keyboard settings.'])

  // Within the ttl the status line yields instead of overwriting the answer.
  assert.equal(env.api.petSpeechBeat(cur, 5000), 'holding')
  assert.deepEqual(said.map(row => row.text), ['This opens the keyboard settings.'])

  // After the ttl the guide instruction resumes on the same channel.
  assert.equal(env.api.petSpeechBeat(cur, 16001), 'status')
  assert.equal(said.at(-1).text, 'Click Keyboard')
  assert.equal(said.at(-1).options.id, 'butler-say')
  assert.equal(said.at(-1).options.ttlMs, 9000)

  // ...and does not repeat itself on the next beat.
  assert.equal(env.api.petSpeechBeat(cur, 18001), 'repeat')
  assert.equal(said.length, 2)
  env.dispose()
})

test('a say row without an id or text never speaks', async () => {
  const said = []
  const pet = { say(text, options) { said.push({ text, options }) } }
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), pet
  })

  assert.equal(env.api.petSpeechBeat({ say: { id: '', text: 'orphan' } }, 1000), 'idle')
  assert.equal(env.api.petSpeechBeat({ say: { id: 'abcd1234', text: '   ' } }, 1000), 'idle')
  assert.equal(env.api.petSpeechBeat({}, 1000), 'idle')
  assert.equal(env.api.petSpeechBeat({ say: null }, 1000), 'idle')
  assert.equal(said.length, 0)
  env.dispose()
})

test('pet speech runs from module scope, not only while the pane is open', () => {
  const src = readFileSync(PLUGIN_JS, 'utf8')
  assert.match(src, /function startPetSpeechPolling\(ctx\)/)
  assert.match(src, /startPetSpeechPolling\(ctx\)/, 'activate() must start it')
  assert.match(src, /function speechSnapshot\(data\)/)
  // The poller must speak the answer row, not just the guide status line.
  assert.match(src, /petSpeechBeat\(speechSnapshot\(data\)\)/)
})

// --- WATCH leaves a persistent mark: pane row, ended line, keep-alive ---

function descendants(node) {
  if (!node || typeof node !== 'object') return []
  const children = node.props ? node.props.children : null
  const list = Array.isArray(children)
    ? children
    : (children === undefined || children === null ? [] : [children])
  return [node, ...list.flatMap(descendants)]
}

function buttonByText(tree, text) {
  return descendants(tree).find(node => node.type === 'button'
    && node.props && node.props.children === text)
}

test('a live watch renders one pane row with app, changes, and a Stop button', async () => {
  const calls = []
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, options) => {
      calls.push({ path: requestPath, method: options && options.method })
      return { ok: true, status: 'stopped' }
    },
    document: makeDocument()
  })
  const started = 1_800_000_000
  const watch = { app: 'Finder', events: 2, started_at: started }

  assert.equal(env.api.watchIsActive(watch), true)
  assert.equal(env.api.watchIsActive({ ...watch, stopped: { reason: 'app-quit' } }), false)
  assert.equal(env.api.watchPaneText(watch, (started + 65) * 1000),
    '⏲ Watching Finder · 2 changes · 01:05')
  assert.equal(env.api.watchPaneText({ app: 'Finder', events: 0 }, started * 1000),
    '⏲ Watching Finder · 0 changes · 00:00')

  const row = env.api.WatchRow({ watch })
  const rendered = JSON.stringify(row)
  assert.match(rendered, /Watching Finder/)
  assert.match(rendered, /2 changes/)
  assert.match(rendered, /\d\d:\d\d/)
  const stop = buttonByText(row, 'Stop')
  assert.ok(stop, 'the row carries a Stop button')
  // A stopped row is an ending, not a live row: it must not render.
  assert.equal(env.api.WatchRow({ watch: { ...watch, stopped: { reason: 'app-quit' } } }), null)

  stop.props.onClick()
  await Promise.resolve()
  assert.deepEqual(calls.at(-1), { path: '/watch/stop', method: 'POST' })
  env.dispose()
})

test('a stopped watch speaks "Watch ended" once, then the row clears', async () => {
  const said = []
  const calls = []
  const pet = {
    say(text, options) { said.push({ text, options }) },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, options) => {
      calls.push({ path: requestPath, method: options && options.method })
      return requestPath === '/watch/stop' ? { ok: true, status: 'stopped' } : {}
    },
    document: makeDocument(),
    pet
  })
  const stopped = {
    app: 'Finder', events: 4, stopped: { reason: 'app-quit', at: 2000 }
  }

  assert.equal(env.api.watchEndedLine(stopped), 'Watch ended — the app quit')
  assert.equal(env.api.petSpeechBeat({ watch: stopped }, 1000), 'watch-ended')
  assert.deepEqual(said.map(row => row.text), ['Watch ended — the app quit'])
  assert.equal(said[0].options.id, 'butler-watch-ended')
  assert.ok(calls.some(call => call.path === '/watch/stop' && call.method === 'POST'),
    'the dead row is cleared through the same endpoint the Stop button uses')

  // Exactly once: the row may linger for a poll, the line does not repeat.
  assert.equal(env.api.petSpeechBeat({ watch: stopped }, 3000), 'idle')
  assert.equal(said.length, 1)

  // A different ending is its own line.
  assert.equal(env.api.petSpeechBeat({
    watch: { app: 'Finder', events: 1, stopped: { reason: 'stopped', at: 4000 } }
  }, 5000), 'watch-ended')
  assert.equal(said.at(-1).text, 'Watch ended — stopped')
  env.dispose()
})

test('a throwing pet say retries the ended line instead of losing it', async () => {
  const said = []
  const calls = []
  let throwOnce = true
  const pet = {
    say(text, options) {
      if (throwOnce) {
        throwOnce = false
        throw new Error('pet muted')
      }
      said.push({ text, options })
    },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, options) => {
      calls.push({ path: requestPath, method: options && options.method })
      return { ok: true, status: 'stopped' }
    },
    document: makeDocument(),
    pet
  })
  const stopped = {
    app: 'Finder', events: 2, stopped: { reason: 'app-quit', at: 2000 }
  }

  // The say threw, so the line did not go out and the dedupe key is not burned.
  assert.equal(env.api.petSpeechBeat({ watch: stopped }, 1000), 'idle')
  assert.equal(said.length, 0)
  assert.ok(!calls.some(call => call.path === '/watch/stop'))

  assert.equal(env.api.petSpeechBeat({ watch: stopped }, 3000), 'watch-ended')
  assert.deepEqual(said.map(row => row.text), ['Watch ended — the app quit'])
  assert.ok(calls.some(call => call.path === '/watch/stop' && call.method === 'POST'))
  env.dispose()
})

test('a live watch is re-said every 25 s on the existing speech beat', async () => {
  const said = []
  const pet = {
    say(text, options) { said.push({ text, options }) },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), pet
  })
  const watch = { app: 'Finder', events: 2, started_at: 1_800_000_000 }
  const cur = { watch }

  assert.equal(env.api.petSpeechBeat(cur, 1000), 'status')
  assert.equal(said[0].text, '⏲ Watch · Finder · 2 changes')
  // The host clamps a ttl to 30 s: the watch line asks for the clamp so the
  // 25 s re-say lands before the previous one expires.
  assert.equal(said[0].options.ttlMs, 30000)

  assert.equal(env.api.petSpeechBeat(cur, 24999), 'repeat')
  assert.equal(said.length, 1)
  assert.equal(env.api.petSpeechBeat(cur, 26000), 'status')
  assert.equal(said.length, 2)
  assert.equal(said[1].text, said[0].text)

  // Only the ambient watch line gets the keep-alive: a guide instruction is
  // carried by the ring and the pane, and still speaks once.
  const guide = { status: 'running', instruction: 'Click Export', watch }
  assert.equal(env.api.petSpeechBeat(guide, 80000), 'status')
  assert.equal(said.at(-1).text, 'Click Export')
  assert.equal(env.api.petSpeechBeat(guide, 106000), 'repeat')
  assert.equal(said.length, 3)
  env.dispose()
})

test('a sticky hit is pane-visible and the keep-alive never re-says it', async () => {
  const said = []
  const pet = {
    say(text, options) { said.push({ text, options }) },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), pet
  })
  const listed = 1_800_000_010
  const watch = {
    app: 'Finder', events: 4, once: false, started_at: 1_800_000_000,
    hit: { label: 'Export complete', window: 'Desktop', at: listed, acknowledged: false }
  }
  const cur = { watch }

  // The pane row names the hit until the user acknowledges it.
  assert.equal(env.api.watchPaneText(watch, (1_800_000_000 + 5) * 1000),
    '⏲ Watching Finder · hit: Export complete')
  const row = env.api.WatchRow({ watch })
  assert.match(JSON.stringify(row), /hit: Export complete/)
  assert.ok(buttonByText(row, 'Stop'), 'Stop stays beside the hit')

  assert.equal(env.api.petSpeechBeat(cur, 1000), 'status')
  assert.equal(said[0].text, '⏲ Watch · hit: Export complete')
  // The hit asks for a readable ttl, but it has no keep-alive: the same line
  // must NOT be re-said 25 s (or 60 s) later.
  assert.equal(said[0].options.ttlMs, 30000)
  assert.equal(env.api.petSpeechBeat(cur, 26000), 'repeat')
  assert.equal(env.api.petSpeechBeat(cur, 60000), 'repeat')
  assert.equal(said.length, 1, 'a hit is announced once, not every 25 s')

  // Acknowledged: the row returns to the change count, and the live count gets
  // its normal keep-alive back.
  const acked = { ...watch, hit: null }
  assert.equal(env.api.watchPaneText(acked, (1_800_000_000 + 90) * 1000),
    '⏲ Watching Finder · 4 changes · 01:30')
  assert.equal(env.api.petSpeechBeat({ watch: acked }, 90000), 'status')
  assert.equal(said.at(-1).text, '⏲ Watch · Finder · 4 changes')
  assert.equal(env.api.petSpeechBeat({ watch: acked }, 116000), 'status')
  assert.equal(said.length, 3)
  env.dispose()
})

test('an unacknowledged watch_hit outlives its watch and is re-said every 25 s', async () => {
  const said = []
  const pet = {
    say(text, options) { said.push({ text, options }) },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument(), pet
  })
  const hit = {
    app: 'Slack', label: 'Export complete', window: 'general — Slack',
    at: 1_800_000_010, acknowledged: false, id: 'a1b2c3d4'
  }
  const watch = { app: 'Slack', events: 9, stopped: null }
  const stalled = {
    status: 'stalled',
    note: 'step 1 unverified for 600s; expect=expectation_unmet',
    instruction: 'Click Settings',
    watch,
    watchHit: hit
  }

  assert.equal(env.api.watchHitLine(hit), '⏲ Hit · Slack: Export complete')
  assert.equal(env.api.petStatusLine(stalled),
    '⏲ Hit · Slack: Export complete',
    'the hit outranks the live watch line and a stalled guide note')
  assert.equal(env.api.petStatusLine({
    status: 'stopped', note: 'stopped by agent', watchHit: hit
  }), '⏲ Hit · Slack: Export complete', 'and every terminal guide line')
  assert.equal(env.api.petStatusLine({
    status: 'done', watchHit: hit
  }), '⏲ Hit · Slack: Export complete')
  // A live guide instruction still wins: the user is being walked through it.
  assert.equal(env.api.petStatusLine({
    status: 'running', instruction: 'Click Export', watchHit: hit, watch
  }), 'Click Export')

  assert.equal(env.api.petSpeechBeat(stalled, 1000), 'status')
  assert.equal(said[0].text, '⏲ Hit · Slack: Export complete')
  // The host clamps a ttl to 30 s: the 25 s re-say lands before it expires.
  assert.equal(said[0].options.ttlMs, 30000)
  assert.equal(said[0].options.id, 'butler-say')
  assert.equal(env.api.petSpeechBeat(stalled, 24999), 'repeat')
  assert.equal(said.length, 1)
  assert.equal(env.api.petSpeechBeat(stalled, 26000), 'status')
  assert.equal(said.length, 2)
  assert.equal(said[1].text, said[0].text)

  // Acknowledging removes it on the next beat: no "thanks", no extra speech.
  const acked = { ...stalled, watchHit: { ...hit, acknowledged: true } }
  assert.equal(env.api.petStatusLine(acked), '⏲ Watch · Slack · 9 changes')
  assert.equal(env.api.petSpeechBeat(acked, 52000), 'status')
  assert.equal(said.at(-1).text, '⏲ Watch · Slack · 9 changes')
  // Absent behaves like round 20 too.
  assert.equal(env.api.petStatusLine({ watch }), '⏲ Watch · Slack · 9 changes')
  assert.equal(env.api.petStatusLine({}), null)
  assert.equal(env.api.watchHitLine({ app: 'Slack', label: '', acknowledged: false }), null)
  assert.equal(env.api.watchHitLine({ app: 'Slack', label: 'X', acknowledged: true }), null)
  env.dispose()
})

test('a one-shot hit speaks the persistent hit line, not "Watch ended"', async () => {
  const said = []
  const calls = []
  const pet = {
    say(text, options) { said.push({ text, options }) },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, options) => {
      calls.push({ path: requestPath, method: options && options.method })
      return requestPath === '/watch/stop'
        ? { ok: true, status: 'stopped' }
        : {}
    },
    document: makeDocument(),
    pet
  })
  const watch = {
    app: 'Slack', events: 3, once: true,
    hit: { label: 'Export complete', at: 2000 },
    stopped: { reason: 'hit', events: 3, at: 3000 }
  }
  const watchHit = {
    app: 'Slack', label: 'Export complete', at: 2000,
    acknowledged: false, id: 'a1b2c3d4'
  }
  const cur = { watch, watchHit }

  assert.equal(env.api.petSpeechBeat(cur, 1000), 'status')
  assert.deepEqual(said.map(row => row.text), ['⏲ Hit · Slack: Export complete'],
    'the ended line yields to the announcement that outlives it')
  assert.ok(calls.some(call => call.path === '/watch/stop' && call.method === 'POST'),
    'the ended watch row is still cleared')

  const posted = calls.filter(call => call.path === '/watch/stop').length
  assert.equal(env.api.petSpeechBeat(cur, 3000), 'repeat')
  assert.equal(calls.filter(call => call.path === '/watch/stop').length, posted,
    'the silent clear runs once, not every beat')
  assert.equal(env.api.petSpeechBeat(cur, 26000), 'status')
  assert.deepEqual(said.map(row => row.text),
    ['⏲ Hit · Slack: Export complete', '⏲ Hit · Slack: Export complete'])
  env.dispose()
})

test('the speech snapshot carries the top-level watch_hit row', async () => {
  const env = await loadPlugin({
    host: defaultHost(), rest: async () => ({}), document: makeDocument()
  })
  const hit = { app: 'Slack', label: 'Export complete', acknowledged: false }

  const snapshot = env.api.speechSnapshot({ watch_hit: hit })

  assert.deepEqual(snapshot.watch_hit, hit)
  assert.equal(env.api.speechSnapshot({}).watch_hit, null)
  // The beat also reads the raw state spelling straight through.
  assert.equal(env.api.petStatusLine({ watch_hit: hit }),
    '⏲ Hit · Slack: Export complete')
  env.dispose()
})

test('a one-shot hit says "Watch ended" once, then the row clears', async () => {
  const said = []
  const calls = []
  const pet = {
    say(text, options) { said.push({ text, options }) },
    clear() {}
  }
  const env = await loadPlugin({
    host: defaultHost(),
    rest: async (requestPath, options) => {
      calls.push({ path: requestPath, method: options && options.method })
      return requestPath === '/watch/stop'
        ? { ok: true, status: 'stopped' }
        : {}
    },
    document: makeDocument(),
    pet
  })
  const watch = {
    app: 'Finder', events: 3, once: true,
    hit: { label: 'Export complete', at: 2000 },
    stopped: { reason: 'hit', events: 3, at: 3000 }
  }

  assert.equal(env.api.watchEndedLine(watch), 'Watch ended — hit')
  assert.equal(env.api.petSpeechBeat({ watch }, 1000), 'watch-ended')
  assert.deepEqual(said.map(row => row.text), ['Watch ended — hit'])
  assert.equal(said[0].options.id, 'butler-watch-ended')
  assert.ok(calls.some(call => call.path === '/watch/stop' && call.method === 'POST'),
    'the hit row is cleared through the same endpoint the Stop button uses')

  // The terminal row is not re-announced, and it is not a live pane row.
  assert.equal(env.api.petSpeechBeat({ watch }, 3000), 'idle')
  assert.equal(said.length, 1)
  assert.equal(env.api.WatchRow({ watch }), null)
  env.dispose()
})

test('rpc send reuses the most recent tracked chat that still exists', async () => {
  const calls = []
  const host = defaultHost({
    request: async (method, params) => {
      calls.push({ method, params })
      if (method === 'session.list') return [{ id: 'live-1', title: 'Butler', created_at: 2 }]
      if (method === 'session.resume') return { session_id: params.session_id }
      if (method === 'prompt.submit') return { status: 'streaming' }
      throw new Error(`unexpected ${method}`)
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.sendSummonRpc('live-1', 'hello there')

  assert.equal(result.ok, true)
  assert.equal(result.session, 'live-1')
  assert.equal(result.send, 'rpc')
  assert.equal(calls.some(call => call.method === 'session.create'), false,
    'an existing chat is reused, never re-created')
  const submit = calls.find(call => call.method === 'prompt.submit')
  assert.equal(submit.params.text, '/butler hello there',
    'the frame rides the same slash door the composer uses')
  assert.equal(submit.params.session_id, 'live-1')
})

test('rpc send creates a chat only when no tracked id exists', async () => {
  const calls = []
  const remembered = []
  const host = defaultHost({
    request: async (method, params) => {
      calls.push({ method, params })
      if (method === 'session.list') return []
      if (method === 'session.create') return { session_id: 'runtime-9', stored_session_id: 'stored-9' }
      if (method === 'session.resume') return { session_id: params.session_id }
      if (method === 'prompt.submit') return { status: 'streaming' }
      throw new Error(`unexpected ${method}`)
    }
  })
  const env = await loadPlugin({
    host,
    rest: async () => ({}),
    document: makeDocument(),
    storage: { getItem: () => null, setItem: (key, value) => remembered.push({ key, value }) }
  })

  const result = await env.api.sendSummonRpc('draft-1', 'first words')

  assert.equal(result.ok, true)
  assert.equal(result.session, 'stored-9', 'the real chat id is the stored one')
  assert.equal(calls.filter(call => call.method === 'session.create').length, 1)
  assert.ok(remembered.some(entry => String(entry.value).includes('stored-9')),
    'the created chat is remembered for the next send')
})

test('rpc send refuses to claim a send without an admission receipt', async () => {
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.list') return [{ id: 'live-1', created_at: 1 }]
      if (method === 'session.resume') return { session_id: params.session_id }
      if (method === 'prompt.submit') return {}
      throw new Error(`unexpected ${method}`)
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const result = await env.api.sendSummonRpc('live-1', 'lost words')

  assert.equal(result.ok, false)
  assert.equal(result.reason, 'prompt_submit_empty')
})

test('the pet field reaches a background chat over RPC when no composer is mounted', async () => {
  // The regression this covers: the field called the composer ladder alone, so a
  // chat that was not on screen ended the chain with a toast and the RPC door
  // was never tried. The row must still land.
  const calls = []
  const acks = []
  const statePayload = {
    summon_pending: { id: 'pending-rpc', text: 'turn off wifi', destination: 'recent' }
  }
  const rest = async (requestPath, opts) => {
    calls.push({ path: requestPath, opts })
    if (requestPath === '/state') return statePayload
    if (requestPath === '/summon/ack') { acks.push(opts.body.id); return { ok: true } }
    if (requestPath === '/summon/send') return { ok: false, reason: 'injection_denied' }
    if (requestPath === '/journal') return { ok: true }
    return {}
  }
  const host = defaultHost({
    request: async (method, params) => {
      calls.push({ path: `rpc:${method}`, opts: { params } })
      if (method === 'session.list') return [{ id: 'live-1', title: 'Butler', created_at: 2 }]
      if (method === 'session.resume') return { session_id: params.session_id }
      if (method === 'prompt.submit') return { status: 'streaming' }
      throw new Error(`unexpected ${method}`)
    },
    composer: { submit: () => false, setDraft: async () => false }
  })
  const env = await loadPlugin({ host, rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)

  const submit = calls.find(call => call.path === 'rpc:prompt.submit')
  assert.ok(submit, 'the RPC door carried the text')
  assert.equal(submit.opts.params.text, '/butler turn off wifi',
    'the pet frame is never dropped on the RPC door')
  assert.equal(submit.opts.params.session_id, 'live-1')
  assert.deepEqual(acks, ['pending-rpc'], 'the row is acked once the send landed')

  env.dispose()
})

test('a look row keeps its own frame on the RPC door', async () => {
  const seen = []
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.list') return [{ id: 'live-1', created_at: 1 }]
      if (method === 'session.resume') return { session_id: params.session_id }
      if (method === 'prompt.submit') { seen.push(params.text); return { status: 'streaming' } }
      throw new Error(`unexpected ${method}`)
    }
  })
  const env = await loadPlugin({ host, rest: async () => ({}), document: makeDocument() })

  const looked = '[butler look] App: Preview · Window: a.pdf\nQuestion: why is this disabled?'
  const result = await env.api.sendSummonRpc('live-1', looked)

  assert.equal(result.ok, true)
  assert.equal(seen[0], looked, 'an already-framed row is never wrapped twice')
})

test('a refused injection is not retried on the next press', async () => {
  // The host reads the gate once per session; re-proving it every press is a
  // wasted round trip. First row: inject refused, RPC carries it. Second row:
  // inject is skipped entirely.
  let sends = 0
  let rpcSubmits = 0
  let statePayload = { summon_pending: { id: 'p1', text: 'one', destination: 'recent' } }
  const rest = async (path, opts) => {
    if (path === '/state') return statePayload
    if (path === '/summon/ack') return { ok: true }
    if (path === '/journal') return { ok: true }
    if (path === '/summon/send') { sends += 1; return { ok: false, reason: 'injection_denied' } }
    return {}
  }
  const host = defaultHost({
    request: async (method, params) => {
      if (method === 'session.list') return [{ id: 'chat-1', title: 'Butler', created_at: 2 }]
      if (method === 'session.resume') return { session_id: params.session_id }
      if (method === 'prompt.submit') { rpcSubmits += 1; return { status: 'streaming' } }
      throw new Error(`unexpected ${method}`)
    }
  })
  const env = await loadPlugin({ host, rest, document: makeDocument() })

  assert.equal(await env.api.pollSummonPending(), true)
  assert.equal(sends, 1, 'the first press tries injection once')
  assert.equal(rpcSubmits, 1, 'and RPC carries it')

  statePayload = { summon_pending: { id: 'p2', text: 'two', destination: 'recent' } }
  assert.equal(await env.api.pollSummonPending(), true)
  assert.equal(sends, 1, 'the refused gate is not re-proven')
  assert.equal(rpcSubmits, 2, 'the second press went straight to RPC')

  env.dispose()
})
