import { test, expect, type BrowserContext, type Frame, type Page, type Request, type WebSocketRoute } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * Which dashboard surfaces still need `'unsafe-inline'` in the shell's `script-src`.
 *
 * The shell policy (`_BASE_CSP` in `dashboard/server.py`) allows inline script and
 * four CDN hosts. A document the shell builds in the browser (a `srcdoc` frame, a
 * `blob:` page) inherits that policy, so dropping either breaks every such surface
 * silently: a blank frame and a console line. This spec adds the policy the shell is
 * meant to end with as `Content-Security-Policy-Report-Only` to every document that
 * still relies on the permissive one, opens each surface, and compares the violations
 * with {@link EXPECTED}. A report-only policy is inherited the way the enforced one is,
 * so the browser reports what would break without breaking it.
 *
 * The target keeps the shell's own inline scripts, by hash: the built HTML files are
 * hashed here the way the server would hash them, so a violation means a surface,
 * not the shell's bootstrap.
 *
 * Each entry of {@link EXPECTED} is one surface that still breaks. A change that moves
 * a surface off the inherited policy deletes its entry; a new violation fails here.
 * The surfaces that draw a model-written document (a widget, an MCP App, a meeting
 * sketch) get it the way the gateway would hand it over: a frame added to the
 * dashboard socket, or a stubbed app API. Script hosts those documents name are
 * answered by {@link stubScriptHosts}, so nothing leaves the machine.
 */

const HERE = dirname(fileURLToPath(import.meta.url))
const DIST = resolve(HERE, '..', 'dist')
const WINDOWS = join(DIST, 'src', 'apps')
const PYTHON = process.env.KIROCREW_PYTHON || 'python3'

// Inline classic, module and import-map scripts. A `src` script is governed by its URL.
const INLINE_SCRIPT = /<script\b(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)<\/script>/gi

/** The built window entries, as the gateway routes them (`/app-windows/<app>/<page>.html`). */
function windowRoutes(): string[] {
  return readdirSync(WINDOWS).flatMap((app) =>
    readdirSync(join(WINDOWS, app))
      .filter((name) => name.endsWith('.html'))
      .map((name) => `/app-windows/${app}/${name}`),
  ).sort()
}

function inlineScriptHashes(html: string): string[] {
  return [...html.matchAll(INLINE_SCRIPT)].map(
    ([, text]) => `'sha256-${createHash('sha256').update(text, 'utf8').digest('base64')}'`,
  )
}

function targetPolicy(): string {
  const files = [join(DIST, 'index.html'), ...windowRoutes().map((r) => join(WINDOWS, r.split('/').slice(2).join('/')))]
  const hashes = [...new Set(files.flatMap((f) => inlineScriptHashes(readFileSync(f, 'utf8'))))]
  return ["script-src 'self' 'wasm-unsafe-eval'", ...hashes, "'report-sample'"].join(' ')
}

type Violation = { directive: string; blocked: string; sample: string; document: string }

// An origin no policy names; see settle().
const SENTINEL = 'https://strict-script-policy-probe.invalid'

/**
 * Which kind of document raised a violation: one the browser built from a string
 * (`srcdoc`), a `blob:` page, or a document the gateway served (`page`). The first
 * two inherit the shell policy; each later change moves one of them to a served URL.
 */
function documentKind(uri: string): string {
  if (uri === 'about:srcdoc') return 'srcdoc'
  if (uri.startsWith('blob:')) return 'blob'
  return 'page'
}

/** One surface's violations as sorted `document directive blocked` strings, the {@link EXPECTED} form. */
function summarize(violations: Violation[]): string[] {
  return [...new Set(violations.map((v) => `${documentKind(v.document)} ${v.directive} ${v.blocked}`))].sort()
}

/**
 * Report every document in *context* that relies on an inline-permitting `script-src`
 * against the target policy, and collect what it would block.
 */
async function probe(context: BrowserContext): Promise<Violation[]> {
  const seen: Violation[] = []
  const target = targetPolicy()
  // A null-origin frame loads its runtime (Tailwind, Mermaid) from the gateway on
  // loopback, which Chromium refuses without the local network access permission.
  // A headless browser cannot ask for it, so the probe grants it: otherwise the
  // runtime fails and the report describes that failure, not the surface.
  await context.grantPermissions(['local-network-access'])
  await context.exposeBinding('__strictScriptPolicyReport', (_source, v: Violation) => {
    seen.push(v)
  })
  await context.addInitScript((sentinel) => {
    // On the window, not the document: a popup's first document (about:blank) hands
    // its window to the page it navigates to, and this script does not run again
    // there. The event bubbles from the document to its window.
    window.addEventListener('securitypolicyviolation', (e) => {
      const blocked = /^https?:/.test(e.blockedURI) ? new URL(e.blockedURI).origin : e.blockedURI
      if (e.disposition !== 'report' && blocked !== sentinel) return
      ;(window as unknown as { __strictScriptPolicyReport: (v: unknown) => void }).__strictScriptPolicyReport({
        directive: e.effectiveDirective,
        blocked,
        sample: e.sample,
        // The page's own URL: `documentURI` is cut to its scheme for a srcdoc or blob: document.
        document: location.href.split('?')[0],
      })
    })
  }, SENTINEL)
  await context.route('**/*', async (route) => {
    if (route.request().resourceType() !== 'document') return route.fallback()
    const response = await route.fetch()
    const headers = response.headers()
    if (!/(^|;)\s*script-src[^;]*'unsafe-inline'/.test(headers['content-security-policy'] ?? '')) {
      return route.fulfill({ response })
    }
    return route.fulfill({ response, headers: { ...headers, 'content-security-policy-report-only': target } })
  })
  return seen
}

/**
 * Wait until every report *page*'s document has raised so far has arrived. Reports
 * come back through a binding, asynchronously but in order, so a script from a host no
 * policy allows, added last, is a marker: once its report is in, the earlier ones are
 * too. It is a URL rather than inline text so that a page with a strict policy of its
 * own, which the target policy is not added to, still reports it.
 */
async function settle(target: Page | Frame, seen: Violation[]): Promise<Violation[]> {
  const markers = () => seen.filter((v) => v.blocked === SENTINEL).length
  const before = markers()
  await target.evaluate((marker) => {
    const s = document.createElement('script')
    s.src = `${marker}/marker.js`
    document.head.append(s)
  }, SENTINEL)
  await expect.poll(markers).toBeGreaterThan(before)
  return seen.filter((v) => v.blocked !== SENTINEL)
}

/** {@link settle} for several documents in turn. */
async function settleAll(targets: Array<Page | Frame>, seen: Violation[]): Promise<Violation[]> {
  for (const target of targets) await settle(target, seen)
  return seen.filter((v) => v.blocked !== SENTINEL)
}

// The script hosts a widget or MCP App document may name. Each is answered here, so
// a document that loads one runs offline and the report names the host.
const SCRIPT_HOSTS = ['https://cdn.jsdelivr.net', 'https://esm.sh']

async function stubScriptHosts(context: BrowserContext) {
  for (const host of SCRIPT_HOSTS) {
    await context.route(`${host}/**`, (route) => route.fulfill({
      contentType: 'application/javascript',
      headers: { 'Access-Control-Allow-Origin': '*' },
      body: `document.documentElement.dataset.probeHost = ${JSON.stringify(host)}`,
    }))
  }
}

/**
 * The dashboard socket, passed through, with a way to add a frame the gateway would
 * send. `frames` holds what the gateway sent, parsed.
 */
async function gatewaySocket(page: Page) {
  const frames: Array<{ type?: string; data?: Record<string, unknown> }> = []
  let client: WebSocketRoute | undefined
  await page.routeWebSocket(/\/api\/ws(?:\?|$)/, (ws) => {
    const server = ws.connectToServer()
    ws.onMessage((message) => server.send(message))
    server.onMessage((message) => {
      if (typeof message === 'string') {
        try {
          frames.push(JSON.parse(message))
        } catch {
          // not a JSON frame; passed on as it is
        }
      }
      ws.send(message)
    })
    client = ws
  })
  return {
    frames,
    send(type: string, data: Record<string, unknown>) {
      if (!client) throw new Error('the dashboard socket has not opened')
      client.send(JSON.stringify({ type, data }))
    },
  }
}

/** A new chat session, opened in *page* once the gateway lists it. */
async function openNewChat(page: Page): Promise<string> {
  const created = await page.request.post('/api/chat/slots', { data: { agent: 'default' } })
  expect(created.ok(), await created.text()).toBeTruthy()
  const slot = ((await created.json()) as { key: string }).key
  await page.goto(`/chat?sid=${encodeURIComponent(slot)}`, { waitUntil: 'domcontentloaded' })
  await expect(page.locator(`[data-slot-key="${slot}"] .session-row.session-active`)).toBeVisible({ timeout: 15_000 })
  return slot
}

/** The frames of *page* whose document is *uri* (`about:srcdoc`, say). */
function framesAt(page: Page, uri: string): Frame[] {
  return page.frames().filter((frame) => frame.url() === uri)
}

// A widget as an agent writes one: markup, an inline script and a library from a
// CDN the widget policy allows. The inline script proves the document ran.
const WIDGET_TITLE = 'Probe widget'
const WIDGET = [
  '<div id="probe-widget">not run</div>',
  "<script>document.getElementById('probe-widget').textContent = 'Probe widget ran'</script>",
  '<script src="https://cdn.jsdelivr.net/npm/probe-chart@1/probe.min.js"></script>',
].join('')
const WIDGET_MESSAGE = `<mcwidget title="${WIDGET_TITLE}">${WIDGET}</mcwidget>`
// A fixed stamp for the rows the tests hand over; only their content matters here.
const MESSAGE_TS = '2026-01-01T00:00:00.000Z'

/** What each surface would still block under the target policy. */
const EXPECTED: Record<string, string[]> = {
  shell: [],
  'app windows': [],
  'sign-in page': [],
  // sandboxFrame.ts fallBack(): the frame document's nonce'd module, run as srcdoc.
  'app-frame srcdoc fallback': ['srcdoc script-src-elem inline'],
  // WidgetFrame.tsx openInNewTab(): the blob: page's link bridge, and the widget
  // document it holds as srcdoc (the builder's own scripts, the widget's, its CDN).
  // The widget in the transcript is a served /sandbox-doc/ document and adds nothing.
  'chat widget pop-out': [
    'blob script-src-elem inline',
    'srcdoc script-src-elem https://cdn.jsdelivr.net',
    'srcdoc script-src-elem inline',
  ],
  // Mochi's renderer/WidgetFrame.tsx: the same widget document, as srcdoc in the panel.
  'Mochi widget': ['srcdoc script-src-elem https://cdn.jsdelivr.net', 'srcdoc script-src-elem inline'],
  // Mochi's openWidgetExternal(): the blob: page carries no script of its own; the
  // widget document inside it inherits through it.
  'Mochi widget pop-out': ['srcdoc script-src-elem https://cdn.jsdelivr.net', 'srcdoc script-src-elem inline'],
  // McpAppFrame.tsx: the bridge guard, the app's own module and the host its metadata names.
  'MCP App': ['srcdoc script-src-elem https://esm.sh', 'srcdoc script-src-elem inline'],
  // Meetings AgentPanel.tsx: the sketch frame's Mermaid bootstrap. The runtime is same-origin.
  'Meetings sketch': ['srcdoc script-src-elem inline'],
}

// The service worker answers the shell from its cache, around this spec's routes.
test.use({ serviceWorkers: 'block' })

test('the shell runs under the strict script policy', async ({ page }) => {
  const seen = await probe(page.context())
  await page.goto('/')
  await expect(page.locator('#root > *').first()).toBeVisible({ timeout: 15_000 })
  const found = await settle(page, seen)
  expect(summarize(found), JSON.stringify(found, null, 2)).toEqual(EXPECTED.shell)
})

test('every app window runs under the strict script policy', async ({ page }) => {
  const seen = await probe(page.context())
  const routes = windowRoutes()
  expect(routes.length, 'the build emits the app windows').toBeGreaterThan(0)
  const found: Violation[] = []
  for (const route of routes) {
    const response = await page.goto(route)
    expect(response?.status(), route).toBe(200)
    found.push(...await settle(page, seen))
    seen.length = 0
  }
  expect(summarize(found), JSON.stringify(found, null, 2)).toEqual(EXPECTED['app windows'])
})

test('the sign-in page runs under the strict script policy', async ({ browser }) => {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] }, serviceWorkers: 'block' })
  try {
    const seen = await probe(context)
    const page = await context.newPage()
    // A window entry is outside the shell fallback, so an anonymous GET is refused
    // with the sign-in page rather than answered with the dashboard.
    const response = await page.goto(windowRoutes()[0])
    expect(response?.status()).toBe(403)
    await expect(page.getByRole('heading', { name: /Sign in required/ })).toBeVisible()
    await page.getByRole('button', { name: 'Connect' }).click()
    await page.getByRole('textbox').press('Enter')
    const found = await settle(page, seen)
    expect(summarize(found), JSON.stringify(found, null, 2)).toEqual(EXPECTED['sign-in page'])
    // The page's script still runs under whatever policy it is served with.
    await page.getByRole('textbox').fill('probe-token')
    await page.getByRole('button', { name: 'Connect' }).click()
    await expect(page).toHaveURL(/[?&]token=probe-token\b/)
  } finally {
    await context.close()
  }
})

test('the app-frame srcdoc fallback runs under the strict script policy', async ({ page }) => {
  const seen = await probe(page.context())
  const app = 'csp-probe-app'
  const program = [
    'import sys',
    'from kiro_crew.apps import app_frame as lp',
    'p = lp._POINTS["linkPreviews"]',
    'sys.stdout.write(lp._frame_html(sys.argv[1], "lp.mjs", "Nprobe0123456789", p.script, srcdoc=True))',
  ].join('\n')
  const document = execFileSync(PYTHON, ['-c', program, app], { encoding: 'utf8' })
  const moduleSource = readFileSync(join(HERE, 'fixtures', 'lp-register-app', 'ui', 'lp.mjs'), 'utf8')
  const frame = `/app-frame/${app}/linkPreviews/cards/ffffffffffffffff/t0k3n`
  const fromFrame = (request: Request) => {
    try {
      return request.frame() !== page.mainFrame()
    } catch {
      return false
    }
  }
  const login = { status: 403, contentType: 'text/html; charset=UTF-8', body: '<!doctype html><title>Access Required</title>' }
  await page.route('**/api/apps', (route) => route.fulfill({
    json: [{
      name: app,
      enabled: true,
      manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'lp.mjs', hosts: ['probe.example.com'] }] } },
    }],
  }))
  await page.route(`**/api/apps/${app}/frames/linkPreviews/*`, (route) => route.fulfill({
    json: { frame, frameId: 'ffffffffffffffff', capability: 'ffffffffffffffff~linkPreviews~cards~cap', ttl: 900, document },
  }))
  // An authenticating proxy: the frame's own requests carry no cookie and get its login page.
  await page.route(`**${frame}`, (route) => route.fulfill(fromFrame(route.request()) ? login : { status: 404, body: '' }))
  await page.route(`**/apps/${app}/ui/lp.mjs`, (route) => route.fulfill(fromFrame(route.request()) ? login : {
    status: 200,
    contentType: 'application/javascript',
    headers: { 'Access-Control-Allow-Origin': 'null', Vary: 'Origin' },
    body: moduleSource,
  }))
  await hoverProbeLink(page)
  const card = page.frameLocator(`iframe[data-link-preview-frame="${app}:cards"]`).locator('#probe-card')
  await expect(card).toHaveText(/Probe card:/, { timeout: 12_000 })
  const found = await settle(page, seen)
  expect(summarize(found), JSON.stringify(found, null, 2)).toEqual(EXPECTED['app-frame srcdoc fallback'])
})

test('a chat widget and its pop-out run under the strict script policy', async ({ page }) => {
  const context = page.context()
  const seen = await probe(context)
  await stubScriptHosts(context)
  const socket = await gatewaySocket(page)
  const slot = await openNewChat(page)
  socket.send('chat_message', {
    slot,
    role: 'assistant',
    content: WIDGET_MESSAGE,
    ts: MESSAGE_TS,
    mid: 'm-strict-script-policy-widget',
  })
  // In the transcript the widget is a served /sandbox-doc/ document: it inherits nothing.
  const inChat = page.frameLocator('iframe[src*="/sandbox-doc/"]').locator('#probe-widget')
  await expect(inChat).toHaveText('Probe widget ran', { timeout: 15_000 })
  const popupOpened = context.waitForEvent('page')
  await page.getByText(WIDGET_TITLE, { exact: true }).hover()
  await page.getByRole('button', { name: 'Open in new tab' }).click()
  const popup = await popupOpened
  await expect(popup.frameLocator('iframe').locator('#probe-widget')).toHaveText('Probe widget ran')
  const served = page.frames().filter((frame) => frame.url().includes('/sandbox-doc/'))
  expect(served.length, 'the transcript widget is a served document').toBeGreaterThan(0)
  const found = await settleAll([page, ...served, popup, ...framesAt(popup, 'about:srcdoc')], seen)
  expect(summarize(found), JSON.stringify(found, null, 2)).toEqual(EXPECTED['chat widget pop-out'])
})

test('a Mochi widget and its pop-out run under the strict script policy', async ({ page }) => {
  const context = page.context()
  const seen = await probe(context)
  await stubScriptHosts(context)
  // The panel reads its history from the Mochi session.
  await page.route('**/api/chat/slots/mochi', (route) => route.request().method() !== 'GET' ? route.fallback() : route.fulfill({
    json: { messages: [{ role: 'assistant', content: WIDGET_MESSAGE, ts: MESSAGE_TS }] },
  }))
  await page.goto('/app-windows/mochi/panel.html')
  await expect(page.frameLocator(`iframe[title="${WIDGET_TITLE}"]`).locator('#probe-widget'))
    .toHaveText('Probe widget ran', { timeout: 15_000 })
  const inPanel = await settleAll([page, ...framesAt(page, 'about:srcdoc')], seen)
  expect(summarize(inPanel), JSON.stringify(inPanel, null, 2)).toEqual(EXPECTED['Mochi widget'])
  seen.length = 0
  const popupOpened = context.waitForEvent('page')
  await page.getByRole('button', { name: 'Open in browser' }).click()
  const popup = await popupOpened
  await expect(popup.frameLocator('iframe').locator('#probe-widget')).toHaveText('Probe widget ran')
  const popped = await settleAll([popup, ...framesAt(popup, 'about:srcdoc')], seen)
  expect(summarize(popped), JSON.stringify(popped, null, 2)).toEqual(EXPECTED['Mochi widget pop-out'])
})

test('an MCP App runs under the strict script policy', async ({ page }) => {
  const context = page.context()
  const seen = await probe(context)
  await stubScriptHosts(context)
  const socket = await gatewaySocket(page)
  const slot = await openNewChat(page)
  // A real tool call from the fake backend, so the transcript has the row an app attaches to.
  const composer = page.getByPlaceholder(/message/i)
  await expect(composer).not.toHaveAttribute('placeholder', /offline/i)
  await composer.fill('[[TOOL]] draw it')
  await page.keyboard.press('Enter')
  await expect.poll(() => socket.frames.some((f) => f.type === 'chat_done' && f.data?.slot === slot), { timeout: 30_000 }).toBe(true)
  const call = socket.frames.find((f) => f.type === 'tool_call' && f.data?.slot === slot && f.data?.tool_call_id)
  expect(call, 'the fake backend made a tool call').toBeTruthy()
  // An app document as a server sends one: its own inline module, and a library from
  // a host its resource metadata allows.
  const html = [
    '<!doctype html><html><head></head><body>',
    '<div id="probe-app">not run</div>',
    '<script type="module">document.getElementById("probe-app").textContent = "Probe app ran"</script>',
    '<script type="module" src="https://esm.sh/probe-diagram@1"></script>',
    '</body></html>',
  ].join('')
  socket.send('mcp_app_render', {
    session_key: slot,
    tool_call_id: call?.data?.tool_call_id,
    server: 'probe',
    tool: 'draw',
    html,
    csp: { resourceDomains: ['https://esm.sh'] },
    permissions: null,
    spool_id: 'spool-strict-script-policy',
    callback_secret: 'probe-secret',
  })
  await expect(page.frameLocator('iframe[title="probe / draw"]').locator('#probe-app'))
    .toHaveText('Probe app ran', { timeout: 15_000 })
  const found = await settleAll([page, ...framesAt(page, 'about:srcdoc')], seen)
  expect(summarize(found), JSON.stringify(found, null, 2)).toEqual(EXPECTED['MCP App'])
})

test('the Meetings sketch frame runs under the strict script policy', async ({ page }) => {
  const seen = await probe(page.context())
  const agent = { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html', enabled_by_default: true, builtin: true }
  const meta = (id: string) => ({
    event_id: id, title: 'Probe meeting', status: 'ended', attachments: [], outputs: {},
    muted_agents: [], agents_enabled: [agent.id],
  })
  // The sketch agent's output: markup and a diagram the frame's own Mermaid bootstrap draws.
  const sketch = '<div id="probe-sketch">Probe sketch</div><pre class="mermaid">graph TD; A-->B</pre>'
  await page.route('**/api/apps/meetings/**', (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/apps/meetings', '')
    const meeting = /^\/meetings\/([^/]+)(\/[a-z]+)?$/.exec(path)
    const id = meeting ? decodeURIComponent(meeting[1]) : ''
    const json = ((): unknown => {
      if (path === '/config') {
        return {
          config: {
            meeting_agents: [agent], stt_provider: '', task_provider: '', calendar: { provider: '', source: '' },
            presets: {}, default_preset: '', poll_interval_active: 60, poll_interval_idle: 60, translation_language: '',
          },
          task_providers: [], calendar_providers: [], stt_providers: [], translation_languages: [],
        }
      }
      if (path === '/agents') return { agents: [agent], task_extractor_id: '' }
      if (path === '/calendar') return { events: [] }
      if (path === '/meetings') return { meetings: [] }
      if (path === '/dictionary') return { terms: [] }
      if (path === '/status') return { active_meeting: null, muted_agents: [], agents: {}, agents_paused: false, expired: false }
      if (!meeting) return {}
      switch (meeting[2]) {
        case undefined: return { meta: meta(id), live: null }
        case '/init': return { meeting_id: id, meta: meta(id) }
        case '/outputs': return { outputs: { [agent.id]: sketch }, edits: {}, tasks: [] }
        case '/transcript': return { segments: [], next_cursor: 0 }
        case '/note': return { content: '', updated_at: '', path: '' }
        case '/translations': return { language: '', language_label: '', lines: [], next_n: 0, pending: 0, dropped: 0 }
        default: return {}
      }
    })()
    return route.fulfill({ json })
  })
  await page.goto('/meetings')
  await page.getByRole('button', { name: 'New meeting' }).click()
  const frame = page.frameLocator('[data-agent-panel="sketch-artist"] iframe')
  await expect(frame.locator('#probe-sketch')).toBeVisible({ timeout: 15_000 })
  // The diagram is drawn only if the frame's bootstrap ran.
  await expect(frame.locator('.mermaid svg')).toBeVisible({ timeout: 15_000 })
  const found = await settleAll([page, ...framesAt(page, 'about:srcdoc')], seen)
  expect(summarize(found), JSON.stringify(found, null, 2)).toEqual(EXPECTED['Meetings sketch'])
})

async function hoverProbeLink(page: Page) {
  await page.goto('/')
  await page.evaluate(() => {
    const scope = document.createElement('div')
    scope.setAttribute('data-link-preview-scope', '')
    document.body.append(scope)
    const a = document.createElement('a')
    a.href = 'https://probe.example.com/item/1'
    a.textContent = 'probe link'
    a.id = 'csp-probe-link'
    Object.assign(a.style, { position: 'fixed', left: '200px', top: '200px', zIndex: '2147483646' })
    scope.append(a)
  })
  // The first hover does not always open a preview, so hover again until a frame opens.
  await expect(async () => {
    await page.mouse.move(0, 0)
    await page.locator('#csp-probe-link').hover()
    await expect(page.locator('iframe[data-link-preview-frame]')).toHaveCount(1, { timeout: 2_000 })
  }).toPass({ timeout: 15_000 })
}
