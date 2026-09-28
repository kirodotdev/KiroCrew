/**
 * Screenshot harness for the stale-session collapse feature.
 *
 * Serves the REAL built SPA (website/dist) with /api/** stubbed. Fixture has
 * an expanded "Kiro" folder (fresh + stale sessions, one pinned-old exempt),
 * a "KAS" subfolder with its own stale rows, and ungrouped root sessions with
 * stale ones — so all three container levels show their own expander row.
 *
 * Captures: default collapsed state, all expanders expanded, feature off
 * (persisted 0), and the threshold picker submenu.
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-stale-collapse.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || (process.env.KIROCREW_SCRATCH || '/tmp') + '/stale-collapse'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now()
const hoursAgo = h => new Date(NOW - h * 3600_000).toISOString()

const folders = [
  { id: 'kiro', name: 'Kiro', order: 1, collapsed: false },
  { id: 'kas', name: 'KAS', order: 2, collapsed: false, parent_id: 'kiro' },
]

const slot = (key, title, folder_id, ageHours, extra = {}) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew',
  created: hoursAgo(ageHours + 2), last_turn_ts: hoursAgo(ageHours), folder_id, ...extra,
})

const slots = [
  // Ungrouped root: 2 fresh + 3 stale
  slot('s-r1', 'Fix sidebar drag bug', '', 2),
  slot('s-r2', 'KAS login UI', '', 20),
  slot('s-r3', 'Bedrock payments research', '', 75),
  slot('s-r4', 'DSH session store deep dive', '', 120),
  slot('s-r5', 'Desktop update channels', '', 200),
  // Kiro folder: 2 fresh + 1 pinned-old (exempt) + 3 stale
  slot('s-k1', 'Settings path navigation', 'kiro', 1),
  slot('s-k2', 'App Store split PR1', 'kiro', 26),
  slot('s-k3', 'Long-running: CI health', 'kiro', 140, { pinned: true }),
  slot('s-k4', 'Theme pack debugging', 'kiro', 90),
  slot('s-k5', 'i18n conflict fix', 'kiro', 130),
  slot('s-k6', 'Sidebar scrollbar issue', 'kiro', 160),
  // KAS subfolder: 1 fresh + 2 stale
  slot('s-a1', 'KAS auth module', 'kas', 5),
  slot('s-a2', 'OIDC device-code flow', 'kas', 80),
  slot('s-a3', 'Token store encryption', 'kas', 110),
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 980 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await stubDashboardApi(page, { folders, slots })
  logPageProblems(page)

  async function load(thresholdMs) {
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-privacy-notice-v1', '1')
      if (t !== null) localStorage.setItem('mc-session-stale-collapse-ms', String(t))
    }, thresholdMs)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  // 1. Default: threshold 2d, every level collapsed.
  await load(null)
  await page.screenshot({ path: `${OUT}/01-default-collapsed.png` })

  // 2. All expanders open.
  const expanders = page.locator('[data-testid^="stale-expander-"]')
  const n = await expanders.count()
  console.log('expanders:', n)
  for (let i = 0; i < n; i++) await expanders.nth(i).click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/02-expanded.png` })

  // 3. Threshold picker submenu (open the sort/filter menu, hover the row).
  await load(null)
  await page.locator('button[aria-label]').filter({ has: page.locator('svg.lucide-list-filter') }).first().click()
  const menuTrigger = page.getByTestId('stale-collapse-menu')
  if (await menuTrigger.count()) {
    await menuTrigger.hover()
    await page.waitForTimeout(500)
    await page.screenshot({ path: `${OUT}/03-threshold-menu.png` })
  } else {
    console.log('menu trigger not visible; skipping menu shot')
  }

  // 4. Feature off (persisted 0): everything visible, no expanders.
  await load(0)
  await page.screenshot({ path: `${OUT}/04-off.png` })
  console.log('off-state expanders:', await expanders.count())

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
