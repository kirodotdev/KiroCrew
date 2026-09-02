/**
 * Screenshot harness for the terminal reconnect banner (Lane B).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures via Playwright route
 * interception. No gateway, no dashboard token, no PTY spawned — the client
 * code is unmodified, only the network is stubbed, so the docked terminal and
 * its disconnected banner lay out exactly as they do in production.
 *
 * The disconnected STATE is reached through the production code path, not
 * faked: the terminal WebSocket route accepts then immediately closes each
 * dial, so `terminalRegistry.connect` runs its full backoff chain to the retry
 * ceiling and flips the session to 'disconnected'. Because production caps the
 * backoff at 30s a step (~2.9 min to exhaust ten dials), an init script clamps
 * setTimeout delays inside the page so the same real chain completes in well
 * under a second — the timing is compressed, the state machine is not mocked.
 *
 * Captured:
 *   01-disconnected-banner-dark.png        docked terminal with the banner +
 *                                          Reconnect button, desktop width, dark
 *   02-disconnected-banner-light.png       light-theme parity of the banner
 *   03-disconnected-banner-phone-390.png   iPhone-width (390px): the banner and
 *                                          its button stay on-screen
 *   04-reconnecting-banner-dark.png        after clicking Reconnect while the WS
 *                                          keeps failing: the "Reconnecting…"
 *                                          presentation with the disabled button
 *
 * Usage: node scripts/capture-terminal-reconnect.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-reconnect'
mkdirSync(OUT, { recursive: true })

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const FIXTURES = {
  '/api/chat/folders': [],
  '/api/chat/slots': [],
  '/api/status': { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/themes': { themes: [], installed: [] },
  '/api/dashboard/branding': { bot_name: 'Kiro Crew', avatar: '/logo.png' },
  '/api/recent-projects': { dirs: [] },
  '/api/agents': {
    agents: [{ name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' }],
    default_agent: 'kirocrew',
  },
  '/api/agents/installed': [{ name: 'kirocrew' }],
  '/api/workspaces': { workspaces: [{ name: 'default' }] },
  '/api/chat/agents': [{ name: 'kirocrew', source: 'builtin' }],
  '/api/kiro-prerequisite': {
    platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
}

async function stubContext(context, theme, opts = {}) {
  const { hangAfterCloses = Infinity } = opts
  await context.routeWebSocket(/\/api\/ws/, () => {})
  // The PTY socket accepts, then closes at once with no `ready` frame. Each
  // reconnect attempt repeats this, exhausting the backoff chain — which is
  // exactly the "socket dead forever" condition the banner exists to surface.
  //
  // For the manual-reconnect capture, the first `hangAfterCloses` dials still
  // close (driving the chain to the 'disconnected' banner); every dial after
  // that is left OPEN-but-silent (no `ready`, no close), so once the user
  // clicks Reconnect the redial hangs and the session sits in the manual
  // 'Reconnecting…' presentation long enough to screenshot.
  let ptyCloses = 0
  await context.routeWebSocket(/\/api\/ws\/terminal\//, async ws => {
    if (ptyCloses < hangAfterCloses) { ptyCloses += 1; ws.close(); return }
    // Past the exhaustion count: hold the handshake open forever so the client
    // socket stays CONNECTING (never onopen, never onclose). The registry keeps
    // the session in 'reconnecting', which — because the redial was manual — is
    // exactly the "Reconnecting…" banner state.
    await new Promise(() => {})
  })

  await context.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots' && route.request().method() === 'POST') {
      return json(route, { key: 'fixture-chat', title: 'New Session…', agent: 'kirocrew' })
    }
    if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' })
    if (Object.hasOwn(FIXTURES, path)) return json(route, FIXTURES[path])
    if (path.startsWith('/api/chat/slots/')) return json(route, {})
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })

  await context.addInitScript(t => {
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    // Seed the docked terminal open with one tab — the persisted shape a reload
    // restores — so the harness never has to click the flaky nav row.
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({
      open: true, height: 300,
      tabs: [{ id: 'fixture-tab-1' }], activeId: 'fixture-tab-1',
    }))
    // Compress the real backoff clock: production caps each delay at 30s, so
    // ten dials would take ~3 min. Clamp the delay only; the retry chain, the
    // ceiling and the disconnected flip all still run in production code.
    const nativeSetTimeout = window.setTimeout.bind(window)
    window.setTimeout = (fn, delay, ...args) =>
      nativeSetTimeout(fn, Math.min(typeof delay === 'number' ? delay : 0, 5), ...args)
  }, theme)
}

async function shotPanel(page, name, matcher = /disconnect/i) {
  // The banner is anchored to the top of the terminal region. Crop the docked
  // terminal panel (banner + a slice of the terminal below it).
  const banner = await page.getByRole('status')
    .filter({ hasText: matcher })
    .boundingBox()
  if (!banner) throw new Error(`terminal banner (${matcher}) not laid out`)
  const vw = page.viewportSize().width
  const clip = {
    x: Math.max(0, banner.x - 8),
    y: Math.max(0, banner.y - 8),
    width: Math.min(vw - Math.max(0, banner.x - 8), banner.width + 16),
    height: banner.height + 180,
  }
  await page.screenshot({ path: `${OUT}/${name}.png`, clip })
  console.log('wrote', `${OUT}/${name}.png`)
}

async function openTerminal(page, base) {
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  // Wait for the banner to appear — it only shows once the backoff chain has
  // exhausted, which proves the whole production path ran.
  await page.getByRole('status')
    .filter({ hasText: /disconnect/i })
    .waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(300)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch(
    process.env.PW_CHROME ? { executablePath: process.env.PW_CHROME } : {},
  )
  try {
    // Desktop, dark.
    let context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    await stubContext(context, 'dark')
    let page = await context.newPage()
    page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
    await openTerminal(page, base)
    await shotPanel(page, '01-disconnected-banner-dark')
    await context.close()

    // Desktop, light.
    context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    await stubContext(context, 'light')
    page = await context.newPage()
    await openTerminal(page, base)
    await shotPanel(page, '02-disconnected-banner-light')
    await context.close()

    // Phone 390px, dark.
    context = await browser.newContext({ viewport: { width: 390, height: 780 }, deviceScaleFactor: 2 })
    await stubContext(context, 'dark')
    page = await context.newPage()
    await openTerminal(page, base)
    await shotPanel(page, '03-disconnected-banner-phone-390')
    await context.close()

    // Desktop, dark — manual "Reconnecting…" state. The PTY WS closes the first
    // ten dials (reaching the disconnected banner), then holds every later dial
    // open-but-silent, so clicking Reconnect leaves the manual retry in flight.
    context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    await stubContext(context, 'dark', { hangAfterCloses: 10 })
    page = await context.newPage()
    page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
    await openTerminal(page, base)
    // Click Reconnect and wait for the banner to switch to the Reconnecting…
    // presentation (which only a user-initiated retry produces).
    await page.getByRole('button', { name: /reconnect/i }).click()
    await page.getByRole('status')
      .filter({ hasText: /reconnecting/i })
      .waitFor({ state: 'visible', timeout: 30000 })
    await page.waitForTimeout(300)
    await shotPanel(page, '04-reconnecting-banner-dark', /reconnecting/i)
    await context.close()
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
