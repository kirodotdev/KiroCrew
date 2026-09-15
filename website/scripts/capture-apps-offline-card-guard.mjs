/**
 * Screenshot harness for issue #3689 — /apps Library with an installed app
 * whose manifest declares `ui.entry` but NO `ui.pages` array.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception —
 * gateway-free, no kiro-cli, no token.
 *
 * What one run cannot prove: the "before" frame must come from a dist built at
 * main (the page-count badge dereferences `m.ui!.pages!.length` and the whole
 * route is replaced by the error fallback), the "after" frame from a dist built
 * with the fix (the entry-only card renders, its badge simply hidden, siblings
 * and page chrome intact). So the script takes the frame name as an argument
 * and is run once per build.
 *
 * Frames:
 *   before.png  main build — route-level "Something went wrong" fallback
 *   after.png   fixed build — Library list renders all three fixture cards
 *
 * Usage: node scripts/capture-apps-offline-card-guard.mjs [outDir] [shotName]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/apps-offline-card-guard'
const SHOT = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const I = (name, displayName, manifestExtra = {}) => ({
  name, displayName, version: '1.0.0', enabled: true,
  installedAt: '2026-07-20T10:00:00Z', origin: 'registry', resources: 'gateway',
  lifecycle: 'gateway',
  manifest: {
    name, version: '1.0.0', displayName,
    description: `${displayName} fixture app.`, author: 'kirocrew', tags: [],
    ...manifestExtra,
  },
})
const installedApps = [
  // The reproducer: ui.entry present, ui.pages ABSENT (the shape a registered
  // app's manifest takes when its backend is offline).
  I('offline-widget', 'Offline Widget', { ui: { entry: 'index.html' } }),
  // Healthy siblings that must survive the reproducer card.
  I('workflows', 'Workflows', { ui: { pages: [{ route: '/workflows', label: 'Workflows', icon: 'Zap' }] } }),
  I('secretary', 'Secretary', {}),
]

const { srv, base } = await serveDist()

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1520, height: 1000 }, deviceScaleFactor: 1 })
const page = await context.newPage()
logPageProblems(page)
await stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path === '/api/apps/registry') {
      return json(route, { apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } }), true
    }
    if (path === '/api/apps/registries') return json(route, { registries: [] }), true
    if (path === '/api/apps') return json(route, installedApps), true
    return false
  },
})
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
  sessionStorage.setItem('appstore-tab', 'library')
})

await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2600)
await page.screenshot({ path: `${OUT}/${SHOT}.png` })

await context.close()
await browser.close()
srv.close()
console.log(`done: ${OUT}/${SHOT}.png`)
