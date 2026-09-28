/**
 * Recording harness for the Web Preview panel's blank-frame hint (#2134).
 *
 * The defect needs a target that is REACHABLE and UNFRAMEABLE at the same time,
 * which no fixture can fake: the panel's liveness probe must succeed (so the
 * panel frames rather than showing "not reachable") while the engine refuses the
 * document (so the frame paints nothing). So this harness starts two real
 * loopback servers and points the panel at them in turn:
 *
 *  - `refuses`  — answers 200 with `X-Frame-Options: DENY`, so Chromium blocks
 *                 the document and the frame is genuinely blank. This is the
 *                 surface the issue reports.
 *  - `healthy`  — answers the same 200 with no framing header, so the page
 *                 renders. Shot because the hint is UNCONDITIONAL: it has to be
 *                 checked against a working preview, where it must not read as
 *                 an error.
 *
 * Both servers bind every interface rather than `127.0.0.1`: the panel's
 * cookie-isolation swap rewrites a preview host equal to the dashboard's onto
 * the other loopback alias, and a server bound to IPv4 only would then be
 * unreachable at `localhost` when that resolves to `::1` -- which the panel
 * would correctly report as a dead server, capturing the wrong state.
 *
 * The probes assert the hint is present, that a frame element really is on
 * screen beside it, and that its link carries the pristine target address, so a
 * PNG cannot be a frame of the wrong surface.
 *
 * Usage: node scripts/capture-web-preview-frame-hint.mjs [outDir] [dark|light] [after|before]
 *   `before` is for a build of main, where the hint does not exist: the probes
 *   invert to assert the reported bug instead of failing on a missing locator --
 *   a frame on screen with NOTHING explaining it.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'node:http'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/web-preview-frame-hint'
const THEME = process.argv[3] || 'dark'
const PHASE = process.argv[4] || 'after'
const WANT_HINT = PHASE !== 'before'
const SLOT = 's-preview'

mkdirSync(OUT, { recursive: true })

// Generic titles: these strings land in a public PR, so they must not name
// anything from a private codebase.
const NOW = Date.parse('2026-09-15T18:00:00Z')
const SEED = [
  { key: SLOT, title: 'Checkout flow — responsive pass', hoursAgo: 0.02 },
  { key: 's2', title: 'Empty states for the settings pages', hoursAgo: 2 },
  { key: 's3', title: 'Token audit across the theme files', hoursAgo: 5 },
]
const slots = SEED.map(s => ({
  key: s.key,
  title: s.title,
  running: false,
  last_message: '',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  created: '2026-09-01T01:00:00Z',
  last_ts: new Date(NOW - s.hoursAgo * 3600_000).toISOString(),
  modified: Math.floor((NOW - s.hoursAgo * 3600_000) / 1000),
  folder_id: '',
  source_links: [],
  source_links_total: 0,
}))

const PAGE_HTML = `<!doctype html><html><head><meta charset="utf-8">
<title>Storefront</title><style>
 html,body{margin:0;height:100%;font:16px/1.5 system-ui,sans-serif;color:#0f172a}
 body{display:flex;align-items:center;justify-content:center;background:#f8fafc}
 .card{padding:28px 34px;border:1px solid #cbd5e1;border-radius:12px;background:#fff}
 h1{margin:0 0 6px;font-size:19px}
 p{margin:0;color:#475569;font-size:13px}
</style></head><body>
 <div class="card"><h1>Storefront</h1><p>Local dev server on port PORT_HERE</p></div>
</body></html>`

/** A real loopback page server. `deny` adds the framing refusal that makes the
 *  frame blank while every request still succeeds. Bound to all interfaces so
 *  either loopback alias reaches it (see the header comment). */
function servePage(deny) {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const headers = { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' }
      if (deny) headers['x-frame-options'] = 'DENY'
      res.writeHead(200, headers)
      const port = srv.address().port
      res.end(req.method === 'HEAD' ? '' : PAGE_HTML.replace('PORT_HERE', String(port)))
    })
    srv.listen(0, () => resolve({ srv, port: srv.address().port }))
  })
}

async function boot(context, previewUrl) {
  const page = await context.newPage()
  // Seeds go through the stub's own init script, after its clear: Playwright
  // does not define the evaluation order of separately registered init
  // scripts, so a second addInitScript would race that clear.
  await stubDashboardApi(page, {
    slots,
    theme: THEME,
    localStorageEntries: {
      'mc-active-slot': SLOT,
      ['mc-activity-open:' + SLOT]: 'true',
      'mc-privacy-notice-v1': '1',
      'mc-lang': 'en',
      ['mc-panel-tabs:' + SLOT]: JSON.stringify({
        activeId: 'browser', tabs: [{ id: 'browser', kind: 'browser' }],
      }),
      ['mc-webpreview-url:' + SLOT]: previewUrl,
    },
  })
  logPageProblems(page)
  return page
}

async function main() {
  const { srv, base } = await serveDist()
  const refuses = await servePage(true)
  const healthy = await servePage(false)
  // The panel isolates a preview host equal to the dashboard's onto the other
  // alias, so the address it ends up framing (and linking) is computed the same
  // way here rather than assumed.
  const dashHost = new URL(base).hostname
  const target = (port) => {
    const host = dashHost === '127.0.0.1' ? 'localhost' : '127.0.0.1'
    return `http://${host}:${port}/`
  }
  const SCENES = [
    { id: 'refuses', url: `http://127.0.0.1:${refuses.port}/`, framed: target(refuses.port) },
    { id: 'healthy', url: `http://127.0.0.1:${healthy.port}/`, framed: target(healthy.port) },
  ]

  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  })

  const probe = {}
  for (const scene of SCENES) {
    const page = await boot(context, scene.url)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    const hint = page.getByTestId('web-preview-frame-hint')
    const frame = page.locator('iframe[title="Web preview"]')
    // Fail the capture rather than shipping a frame of the wrong surface. On a
    // main build there is no hint to wait for, so the frame is the anchor.
    await (WANT_HINT ? hint : frame).waitFor({ state: 'visible', timeout: 20000 })
    // Past the probe's two-strike window, so a scene that would degrade to the
    // unreachable card is caught here instead of looking like a clean shot.
    await page.waitForTimeout(12000)
    probe[scene.id] = WANT_HINT
      ? {
        hintVisible: await hint.isVisible(),
        frameOnScreen: (await frame.count()) === 1,
        hintLinkIsPristineTarget:
          (await hint.getByRole('link', { name: 'Open in browser' }).getAttribute('href')) === scene.framed,
        // The states this hint must never be confused with: if the server had
        // been unreachable, or read as this gateway, the body would show a card
        // instead of a frame and the shot would be of the wrong thing.
        notUnreachableCard: (await page.getByText('Preview server not reachable').count()) === 0,
        stillHintedAfterProbeWindow: await hint.isVisible(),
        text: (await hint.textContent())?.trim(),
      }
      : {
        // The reported defect, asserted rather than assumed: a frame is on
        // screen, the server is demonstrably up, and nothing on the surface
        // accounts for what the reader sees.
        frameOnScreen: (await frame.count()) === 1,
        hintAbsent: (await hint.count()) === 0,
        notUnreachableCard: (await page.getByText('Preview server not reachable').count()) === 0,
        // Vocabulary-independent: main explains the state with neither word, and
        // pinning only the copy this branch happens to ship would let a reworded
        // hint pass the before check by accident.
        nothingExplainsTheBlank: await (async () => {
          const body = (await page.locator('#main-content').innerText()).toLowerCase()
          return !body.includes('embedding') && !body.includes('framed')
        })(),
      }
    await page.screenshot({ path: `${OUT}/${PHASE}-${THEME}-${scene.id}.png` })
    await page.close()
  }
  console.log(`PROBE ${PHASE} ${THEME} ${JSON.stringify(probe, null, 2)}`)

  const failed = Object.entries(probe)
    .flatMap(([scene, checks]) => Object.entries(checks)
      .filter(([, v]) => v === false)
      .map(([k]) => `${scene}.${k}`))
  await context.close()
  await browser.close()
  srv.close()
  refuses.srv.close()
  healthy.srv.close()
  if (failed.length) throw new Error(`probe failed: ${failed.join(', ')}`)
  console.log('wrote frames to', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
