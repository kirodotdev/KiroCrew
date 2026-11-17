/**
 * Screenshot harness for one calm offline state instead of per-panel errors (#38100).
 *
 * Serves the REAL built SPA (website/dist) on 127.0.0.1 against the shared
 * fixture router, so no gateway, relay or credential is involved. Each scene
 * opens the dashboard connected, then drops the `/api/ws` socket the way the
 * real outage does and fails every later `/api` read, so any panel that drew
 * its own error would be on screen:
 *
 * - `viewer-reconnecting`: the relay Crew window's user agent, and the socket
 *   closed 4404 `crew_not_connected` (the relay's close once the host is gone).
 *   The calm reconnecting view covers the panels.
 * - `viewer-refused-control`: the same window closed 4410 (the host turned
 *   access off), a real refusal: the red top-bar notice is unchanged.
 * - `host-offline`: a plain browser tab on 127.0.0.1 whose socket drops with no
 *   code (the gateway stopped). The "gateway isn't running on this machine" view covers
 *   the panels; the top bar keeps "Gateway offline — reconnecting".
 * - `unavailable-card`: the desktop app's existing notice page
 *   (electron/relay-crew-notice.html) with the text relay-crew-notice.js gives
 *   it. Electron is not installed here, so the preload's fill step is done in
 *   the page; when it shows is pinned by electron/test/relay-crew-window.test.js.
 *
 * Usage, from website/ after `npm run build`:
 *   node scripts/capture-38100-offline-cover.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/38100-offline-cover'
const VIEWPORT = { width: 1400, height: 860 }
const RELAY_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Electron/44 Safari/537.36 kiroCrewRelayWindow'

delete process.env.LD_LIBRARY_PATH
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slots = [
  { key: 'chat-release-notes', title: 'Draft the release notes', running: false, messages: 2, agent: 'kirocrew', modified: now, last_ts: new Date().toISOString(), folder_id: '', last_message: 'Noted.', source_links: [], source_links_total: 0 },
  { key: 'chat-triage', title: 'Triage the overnight pages', running: false, messages: 5, agent: 'kirocrew', modified: now - 600, last_ts: new Date(Date.now() - 600_000).toISOString(), folder_id: '', last_message: 'Two left.', source_links: [], source_links_total: 0 },
]

let failures = 0
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) failures++
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}: got ${JSON.stringify(got)}${ok ? '' : ` want ${JSON.stringify(want)}`}`)
}

const visibleText = (page, selector) => page.evaluate(sel => {
  const el = document.querySelector(sel)
  if (!el) return null
  const r = el.getBoundingClientRect()
  return r.width && r.height ? (el.textContent || '').replace(/\s+/g, ' ').trim() : null
}, selector)

async function scene(browser, base, name, { userAgent, close, expect, settledOn }) {
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2, userAgent })
  const page = await context.newPage()
  logPageProblems(page)
  const state = { down: false, live: null }
  await stubDashboardApi(page, {
    slots, theme: 'dark',
    localStorageEntries: { 'mc-active-slot': 'chat-release-notes', 'mc-privacy-notice-v1': '1', 'mc-sidebar-pinned': 'true', 'mc-onboarded': '1' },
  })
  // Registered after the stub, so consulted first: once the connection is down,
  // every read fails as it does with the gateway or the relay's host gone.
  await page.route('**/api/**', route => (state.down ? route.abort('connectionrefused') : route.fallback()))
  await page.routeWebSocket(/\/api\/ws/, ws => {
    if (state.down) ws.close(close)
    else state.live = ws
  })
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.locator('[data-ui-location="chat.session-title"]').first().waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(1200)
  state.down = true
  state.live?.close(close)
  // A scene that names its settled view waits for it (the host view shows only
  // after the quiet-reconnect grace); the others give the drop four seconds.
  if (settledOn) await page.locator(settledOn).waitFor({ state: 'visible', timeout: 20_000 })
  else await page.waitForTimeout(4000)
  await expect(page)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log(`wrote ${OUT}/${name}.png`)
  await context.close()
}

const served = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })
try {
  await scene(browser, served.base, 'viewer-reconnecting', {
    userAgent: RELAY_UA,
    close: { code: 4404, reason: 'crew_not_connected' },
    expect: async page => {
      check('viewer: calm view', await visibleText(page, '[data-testid="relay-reconnecting"] p'), 'Reconnecting…')
      check('viewer: no red top-bar notice', await visibleText(page, '[data-testid="topbar-offline-notice"]'), null)
      check('viewer: panels inert', await page.locator('#main-content').getAttribute('inert'), '')
    },
  })
  await scene(browser, served.base, 'viewer-refused-control', {
    userAgent: RELAY_UA,
    close: { code: 4410, reason: '' },
    expect: async page => {
      check('refused: red notice kept', await visibleText(page, '[data-testid="topbar-offline-notice"]'), 'Relay refused the connection — retrying')
      check('refused: no calm view', await visibleText(page, '[data-testid="relay-reconnecting"] p'), null)
    },
  })
  await scene(browser, served.base, 'host-offline', {
    userAgent: undefined,
    close: undefined,
    settledOn: '[data-testid="host-offline"]',
    expect: async page => {
      check('host: calm view', await visibleText(page, '[data-testid="host-offline"] p'), "The gateway isn't running on this machine.")
      check('host: top bar keeps its notice', await visibleText(page, '[data-testid="topbar-offline-notice"]'), 'Gateway offline — reconnecting')
      check('host: panels inert', await page.locator('#main-content').getAttribute('inert'), '')
    },
  })

  // The desktop app's notice page, filled as its preload fills it.
  const { noticeText } = createRequire(import.meta.url)('../electron/relay-crew-notice.js')
  const text = noticeText('unavailable', 'test-crew')
  for (const scheme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 900, height: 560 }, deviceScaleFactor: 2, colorScheme: scheme })
    const page = await context.newPage()
    await page.goto(pathToFileURL(resolve('electron/relay-crew-notice.html')).href)
    await page.evaluate(t => {
      for (const id of ['title', 'body', 'check', 'close']) {
        const el = document.getElementById(id)
        el.textContent = t[id]
        el.hidden = !t[id]
      }
    }, text)
    check(`card ${scheme}: title`, await visibleText(page, '#title'), 'test-crew is currently unavailable')
    await page.screenshot({ path: `${OUT}/unavailable-card-${scheme}.png` })
    console.log(`wrote ${OUT}/unavailable-card-${scheme}.png`)
    await context.close()
  }
} finally {
  await browser.close()
  served.srv.close()
}
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
