/**
 * Recording harness for the Browser panel's expand mode and the two controls it
 * must keep live: the left-nav brand toggle and the chat sessions toggle.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent. A second
 * loopback server stands in for the previewed app, so the panel's iframe and its
 * liveness probe both see a real origin rather than a dead one.
 *
 * A still frame cannot prove this change: the behaviour under test is a SEQUENCE
 * (expand → click → observe), so the primary artifact is video. The discrete
 * frames are emitted for side-by-side review, and the probes below cover what
 * pixels cannot show — that the sessions toggle is mounted while expanded, that
 * the rail really changes width rather than merely re-rendering, and that a
 * whole expand cycle writes neither persisted layout key.
 *
 * Usage: node scripts/capture-preview-expand-toggles.mjs [outDir] [dark|light]
 */
import { chromium } from 'playwright'
import { createServer } from 'node:http'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/expand-mode-nav-toggles'
const THEME = process.argv[3] || 'dark'
const SLOT = 's-preview'

mkdirSync(OUT, { recursive: true })
mkdirSync(`${OUT}/video`, { recursive: true })

// Generic titles: these strings land in a public PR, so they must not name
// anything from a private codebase.
const NOW = Date.parse('2026-08-10T18:00:00Z')
const SEED = [
  { key: SLOT, title: 'Checkout flow — responsive pass', hoursAgo: 0.02 },
  { key: 's2', title: 'Empty states for the settings pages', hoursAgo: 2 },
  { key: 's3', title: 'Token audit across the theme files', hoursAgo: 5 },
  { key: 's4', title: 'Table density options', hoursAgo: 27 },
  { key: 's5', title: 'Icon set, second pass', hoursAgo: 31 },
]
const slots = SEED.map(s => ({
  key: s.key,
  title: s.title,
  running: false,
  last_message: '',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  created: '2026-08-01T01:00:00Z',
  last_ts: new Date(NOW - s.hoursAgo * 3600_000).toISOString(),
  modified: Math.floor((NOW - s.hoursAgo * 3600_000) / 1000),
  folder_id: '',
  source_links: [],
  source_links_total: 0,
}))

/** The stand-in for "the app you are previewing". A real server, so the panel's
 *  liveness probe passes and the iframe paints instead of falling back to the
 *  unreachable state. */
const PREVIEW_PAGE = `<!doctype html><html><head><meta charset="utf-8">
<title>Sample app</title><style>
  :root { color-scheme: light }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#f6f7f9; color:#1c1f23 }
  header { padding:14px 20px; background:#fff; border-bottom:1px solid #e3e6ea; font-weight:600 }
  main { padding:20px; display:grid; gap:14px; grid-template-columns:repeat(3,1fr) }
  .card { background:#fff; border:1px solid #e3e6ea; border-radius:10px; padding:16px; min-height:82px }
  .card b { display:block; font-size:20px; margin-bottom:4px }
  .bar { height:8px; border-radius:4px; background:#dfe3e8; margin-top:10px }
  .bar i { display:block; height:100%; border-radius:4px; background:#5b7cfa }
</style></head><body>
<header>Sample app &middot; localhost preview</header>
<main>
  <div class="card"><b>1,284</b>Sessions<div class="bar"><i style="width:72%"></i></div></div>
  <div class="card"><b>96%</b>Pass rate<div class="bar"><i style="width:96%"></i></div></div>
  <div class="card"><b>18</b>Open items<div class="bar"><i style="width:34%"></i></div></div>
</main></body></html>`

function servePreview() {
  return new Promise(resolve => {
    const srv = createServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      res.end(PREVIEW_PAGE)
    })
    // "localhost" rather than the dashboard's 127.0.0.1: the panel deliberately
    // isolates the preview onto the OTHER loopback name, and the seeded URL has
    // to match what the app itself would have stored.
    srv.listen(0, '127.0.0.1', () =>
      resolve({ srv, url: `http://localhost:${srv.address().port}/` }))
  })
}

const EXPAND = 'button[aria-label="Expand preview"]'
const COLLAPSE = 'button[aria-label="Collapse"]'
const LOGO_EXPAND = 'button[aria-label="Expand sidebar"]'
const LOGO_COLLAPSE = 'button[aria-label="Collapse sidebar"]'
const SESSIONS_SHOW = 'button[aria-label="Show sessions sidebar"]'
const SESSIONS_HIDE = 'button[aria-label="Hide sessions sidebar"]'

/** Rail width + the two persisted layout keys, read off the live page. */
const readLayout = page => page.evaluate(() => {
  const nav = document.querySelector('nav[aria-label="Main navigation"]')
  return {
    railW: nav ? Math.round(nav.getBoundingClientRect().width) : null,
    mcNav: localStorage.getItem('mc-nav'),
    mcSidebar: localStorage.getItem('mc-sidebar-pinned'),
  }
})

async function boot(context, base, previewUrl) {
  const page = await context.newPage()
  await stubDashboardApi(page, { slots, theme: THEME })
  // Registered AFTER the stub, whose init script clears localStorage.
  await page.addInitScript(([slot, url]) => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-lang', 'en')
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      activeId: 'browser', tabs: [{ id: 'browser', kind: 'browser' }],
    }))
    localStorage.setItem('mc-webpreview-url:' + slot, url)
  }, [SLOT, previewUrl])
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  // Fail the capture rather than shipping a frame of the wrong surface.
  await page.locator(EXPAND).waitFor({ state: 'visible', timeout: 15000 })
  return page
}

async function main() {
  const { srv, base } = await serveDist()
  const { srv: psrv, url: previewUrl } = await servePreview()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    ...(THEME === 'dark'
      ? { recordVideo: { dir: `${OUT}/video`, size: { width: 1440, height: 900 } } }
      : {}),
  })

  // ── Scene A: the fix — both controls work inside expand mode ─────────────
  const page = await boot(context, base, previewUrl)
  const atRest = await readLayout(page)
  await page.screenshot({ path: `${OUT}/${THEME}-01-normal.png` })

  await page.locator(EXPAND).click()
  await page.waitForTimeout(900)
  const expanded = await readLayout(page)
  // The load-bearing assertion: the sessions toggle stays mounted inside expand
  // mode, so there is something to click where the user expects it.
  const toggleWhileExpanded = await page.locator(SESSIONS_SHOW).count()
  const toggleHasFlyout = toggleWhileExpanded
    ? await page.locator(SESSIONS_SHOW).getAttribute('aria-haspopup')
    : null
  await page.screenshot({ path: `${OUT}/${THEME}-02-expanded.png` })

  // The logo: clicked, and observed to change the rail's measured width.
  await page.locator(LOGO_EXPAND).click()
  await page.waitForTimeout(700)
  const railBack = await readLayout(page)
  await page.screenshot({ path: `${OUT}/${THEME}-03-expanded-rail-reopened.png` })

  // The sessions toggle, from that same state.
  await page.locator(SESSIONS_SHOW).click()
  await page.waitForTimeout(900)
  const listBack = await readLayout(page)
  const listVisible = await page.locator(SESSIONS_HIDE).count()
  await page.screenshot({ path: `${OUT}/${THEME}-04-expanded-sessions-reopened.png` })

  // Leaving expand mode must keep both choices the user just made.
  await page.locator(COLLAPSE).click()
  await page.waitForTimeout(900)
  const afterCollapse = await readLayout(page)
  const keptRail = await page.locator(LOGO_COLLAPSE).count()   // rail still open
  const keptList = await page.locator(SESSIONS_HIDE).count()   // list still open
  await page.screenshot({ path: `${OUT}/${THEME}-05-collapsed-choices-kept.png` })

  // ── Scene B: an untouched cycle restores the pre-expand layout ───────────
  const page2 = await boot(context, base, previewUrl)
  const bRest = await readLayout(page2)
  await page2.locator(EXPAND).click()
  await page2.waitForTimeout(800)
  const bExpanded = await readLayout(page2)
  await page2.locator(COLLAPSE).click()
  await page2.waitForTimeout(900)
  const bRestored = await readLayout(page2)
  const bRailOpen = await page2.locator(LOGO_COLLAPSE).count()
  const bListOpen = await page2.locator(SESSIONS_HIDE).count()
  await page2.screenshot({ path: `${OUT}/${THEME}-06-untouched-cycle-restored.png` })

  const probe = {
    sessionsToggleExistsWhileExpanded: toggleWhileExpanded === 1,
    sessionsToggleOffersFlyout: toggleHasFlyout === 'menu',
    railCollapsedOnExpand: expanded.railW < atRest.railW,
    logoReopenedRail: railBack.railW === atRest.railW,
    sessionsToggleReopenedList: listVisible === 1,
    choicesSurvivedCollapse: keptRail === 1 && keptList === 1,
    untouchedCycleRestoredRail: bRailOpen === 1 && bRestored.railW === bRest.railW,
    untouchedCycleRestoredList: bListOpen === 1,
    // The invariant: an expand cycle is transient and must not rewrite either
    // persisted layout preference. Both start null (never set by the fixture).
    persistedKeysUntouchedByExpandCycle:
      bRest.mcNav === null && bRest.mcSidebar === null
      && bExpanded.mcNav === null && bExpanded.mcSidebar === null
      && bRestored.mcNav === null && bRestored.mcSidebar === null,
    railWidths: { atRest: atRest.railW, expanded: expanded.railW, afterLogo: railBack.railW },
    persisted: { atRest, expanded, listBack, afterCollapse, bRestored },
  }
  console.log(`PROBE ${THEME} ${JSON.stringify(probe, null, 2)}`)

  const failed = Object.entries(probe).filter(([k, v]) => v === false).map(([k]) => k)
  await context.close()   // flushes the video
  await browser.close()
  srv.close()
  psrv.close()
  if (failed.length) throw new Error(`probe failed: ${failed.join(', ')}`)
  console.log('wrote frames + video to', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
