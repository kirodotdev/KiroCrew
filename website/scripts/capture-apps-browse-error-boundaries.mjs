/**
 * Screenshot harness for issue #3702 — /apps Browse tab with one card whose
 * render throws, degrading in place instead of blanking the route.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception —
 * gateway-free, no kiro-cli, no token.
 *
 * What one run cannot prove: no registry fixture makes a healthy build's card
 * throw (that is the point of #3702 — the boundary guards FUTURE regressions),
 * so the "degraded" frame comes from a dist built with a transient local patch
 * that makes AppListRow throw for the `zzq-broken-row` fixture. The "healthy"
 * frame comes from the unpatched build. The script takes the frame name as an
 * argument and is run once per build.
 *
 * Frames:
 *   degraded.png  patched build — one row renders the compact boundary
 *                 fallback while sibling rows and the page chrome survive
 *   healthy.png   fix build, no throw — Browse tab renders normally
 *
 * Usage: node scripts/capture-apps-browse-error-boundaries.mjs [outDir] [shotName]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/apps-browse-error-boundaries'
const SHOT = process.argv[3] || 'healthy'
mkdirSync(OUT, { recursive: true })

const R = (name, displayName) => ({
  name, displayName, version: '1.0.0', author: 'kirocrew',
  description: `${displayName} fixture registry app.`, tags: [],
})
const registryApps = [
  R('alpha-notes', 'Alpha Notes'),
  R('beta-board', 'Beta Board'),
  R('casa-clock', 'Casa Clock'),
  // The reproducer: the transient AppListRow patch throws for this name.
  R('zzq-broken-row', 'Zulu Tracker'),
]

const { srv, base } = await serveDist()

const browser = await chromium.launch()
// The dashboard shell scrolls in an inner container (body is 100vh), so a
// fullPage screenshot cannot expand it — use a viewport tall enough for all
// four fixture rows instead.
const context = await browser.newContext({ viewport: { width: 1520, height: 1750 }, deviceScaleFactor: 1 })
const page = await context.newPage()
logPageProblems(page)
await stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path === '/api/apps/registry') {
      return json(route, { apps: registryApps, serverPlatform: { os: 'darwin', arch: 'arm64' } }), true
    }
    if (path === '/api/apps/registries') return json(route, { registries: [] }), true
    if (path === '/api/apps') return json(route, []), true
    return false
  },
})
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
  sessionStorage.setItem('appstore-tab', 'discover')
})

await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })

// Assert what actually rendered before capturing, so a re-run on the wrong
// build fails instead of silently writing false evidence: the degraded frame
// requires the fallback notice (only present when the transient AppListRow
// throw-patch is built in), the healthy frame requires the intact Zulu row
// and NO fallback notice. This also replaces a fixed sleep — the assertion
// itself waits for the page to settle.
const notice = page.getByText('This app could not be displayed.')
if (SHOT === 'degraded') {
  await notice.waitFor({ state: 'visible', timeout: 15000 })
} else {
  await page.getByText('Zulu Tracker fixture registry app.').waitFor({ state: 'visible', timeout: 15000 })
  if (await notice.count() > 0) {
    console.error('healthy frame shows the fallback notice — wrong build (transient patch still applied?)')
    process.exit(1)
  }
}
await page.waitForTimeout(600) // let art/fonts settle after the assertion
await page.screenshot({ path: `${OUT}/${SHOT}.png`, fullPage: true })

await context.close()
await browser.close()
srv.close()
console.log(`done: ${OUT}/${SHOT}.png`)
