/**
 * Recording harness for the Web Preview panel's refused-navigation notice (#22572).
 *
 * The defect needs three real things no unit fixture fakes: the dashboard's own
 * `frame-src` policy, a local app the policy admits, and a navigation INSIDE
 * that frame to an origin the policy refuses. So the harness serves the real
 * built SPA, stamps the dashboard document with the same `frame-src` shape the
 * gateway sends (`server.py`), and points the panel at a loopback app whose
 * "Continue with Google" button sends the frame to accounts.google.com, the
 * way an OAuth redirect does.
 *
 * Nothing reaches Google: the engine refuses the navigation against the
 * parent's policy before any request leaves, which is the bug itself.
 *
 * The probes assert the notice is on screen, names the refused origin, links
 * the app's own address, and that the preview frame is still mounted under it,
 * so a PNG cannot be a frame of the wrong surface.
 *
 * Usage: node scripts/capture-web-preview-frame-blocked.mjs [outDir] [dark|light]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'node:http'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/web-preview-frame-blocked'
const THEME = process.argv[3] || 'dark'
const SLOT = 's-preview'

mkdirSync(OUT, { recursive: true })

/** The gateway's frame-src, loopback part included (`server.py`). */
const FRAME_SRC = "frame-src 'self' blob: https://*.cloudfront.net http://127.0.0.1:* http://localhost:* "
  + 'http://0.0.0.0:* https://127.0.0.1:* https://localhost:* https://0.0.0.0:* http://*.localhost:*'

const NOW = Date.parse('2026-10-20T18:00:00Z')
const slots = [{
  key: SLOT,
  title: 'Sign-in page for the storefront',
  running: false,
  last_message: '',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  created: '2026-10-01T01:00:00Z',
  last_ts: new Date(NOW - 60_000).toISOString(),
  modified: Math.floor((NOW - 60_000) / 1000),
  folder_id: '',
  source_links: [],
  source_links_total: 0,
}]

const APP_HTML = `<!doctype html><html><head><meta charset="utf-8">
<title>Storefront</title><style>
 html,body{margin:0;height:100%;font:16px/1.5 system-ui,sans-serif;color:#0f172a}
 body{display:flex;align-items:center;justify-content:center;background:#f8fafc}
 button{padding:10px 18px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;font:inherit;cursor:pointer}
</style></head><body>
 <button id="google" onclick="location.href='https://accounts.google.com/o/oauth2/auth?client_id=demo'">Continue with Google</button>
</body></html>`

/** The local app, on loopback only. The panel's cookie-isolation swap frames it
 *  at `localhost`, which may resolve to `::1`, so the same port is also bound on
 *  the IPv6 loopback when the host has one. */
function serveApp() {
  const handler = (req, res) => {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
    res.end(req.method === 'HEAD' ? '' : APP_HTML)
  }
  return new Promise(resolve => {
    const v4 = createServer(handler)
    v4.listen(0, '127.0.0.1', () => {
      const port = v4.address().port
      const v6 = createServer(handler)
      const done = () => resolve({ port, close: () => { v4.close(); v6.close() } })
      v6.once('error', () => resolve({ port, close: () => v4.close() }))
      v6.listen(port, '::1', done)
    })
  })
}

async function main() {
  const { srv, base } = await serveDist()
  const app = await serveApp()
  const dashHost = new URL(base).hostname
  const appUrl = `http://127.0.0.1:${app.port}/`
  const framed = `http://${dashHost === '127.0.0.1' ? 'localhost' : '127.0.0.1'}:${app.port}/`

  // Chromium's local-network-access check treats the panel's swapped loopback
  // alias as a cross-address-space request and leaves the frame blank, which
  // a real desktop or a dashboard on loopback does not do. Off for the harness
  // only, so the shot shows the app the reader would see.
  const browser = await chromium.launch({
    args: ['--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessPermissionPrompt'],
  })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots,
    theme: THEME,
    localStorageEntries: {
      'mc-active-slot': SLOT,
      ['mc-activity-open:' + SLOT]: 'true',
      'mc-privacy-notice-v1': '1',
      'mc-lang': 'en',
      ['mc-panel-tabs:' + SLOT]: JSON.stringify({ activeId: 'browser', tabs: [{ id: 'browser', kind: 'browser' }] }),
      ['mc-webpreview-url:' + SLOT]: appUrl,
    },
  })
  // The dashboard document carries the gateway's frame-src; nothing else does.
  await page.route(`${base}/chat`, async route => {
    const res = await route.fetch()
    await route.fulfill({ response: res, headers: { ...res.headers(), 'content-security-policy': FRAME_SRC } })
  })
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })

  const frame = page.locator('iframe[title="Web preview"]')
  await frame.waitFor({ state: 'visible', timeout: 20000 })
  await page.screenshot({ path: `${OUT}/${THEME}-1-app-framed.png` })
  await page.frameLocator('iframe[title="Web preview"]').getByRole('button', { name: 'Continue with Google' }).click()

  const card = page.getByTestId('web-preview-frame-blocked')
  await card.waitFor({ state: 'visible', timeout: 5000 })
  const probe = {
    cardVisible: await card.isVisible(),
    namesRefusedOrigin:
      (await page.getByTestId('web-preview-frame-blocked-origin').textContent())?.trim()
        === 'Blocked: https://accounts.google.com',
    linksTheApp: (await card.getByRole('link', { name: 'Open in browser' }).getAttribute('href')) === framed,
    // Announced above the frame, never in its place.
    frameKept: (await frame.count()) === 1,
  }
  await page.screenshot({ path: `${OUT}/${THEME}-2-sign-in-refused.png` })
  console.log(`PROBE ${THEME} ${JSON.stringify(probe, null, 2)}`)

  await context.close()
  await browser.close()
  srv.close()
  app.close()
  const failed = Object.entries(probe).filter(([, v]) => v === false).map(([k]) => k)
  if (failed.length) throw new Error(`probe failed: ${failed.join(', ')}`)
  console.log('wrote frames to', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
