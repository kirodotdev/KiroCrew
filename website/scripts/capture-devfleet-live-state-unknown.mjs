/**
 * Screenshot harness for "a pointer-state outage is rendered as an error notice,
 * not as an empty fleet" (PR #10330, live_state_known).
 *
 * The defect: when the gateway cannot report which checkout is live or staged,
 * `_build_fleet` still returns the rows (git enumerates worktrees independently
 * of the pointer) but no live/staged badge. Rendered bare, that reads as "nothing
 * is live" — the opposite remedy (stage a cutover) from the true one (check the
 * gateway). The fix carries `live_state_known: false` in the payload and renders
 * an ErrorNotice with the agent hand-off above the rows.
 *
 * Frame 1: the outage payload — notice above the rows, no badge on any row.
 * Frame 2: the same fleet with the pointer read — no notice, `main` badged live.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures: no gateway, no dashboard
 * credential, no provider CLI.
 *
 * Usage: node scripts/capture-devfleet-live-state-unknown.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/devfleet-live-state-unknown'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const ROWS = [
  { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', path: '/w/main', last_updated_at: now - 900 },
  { name: 'kirocrew-wt-widgets', is_main: false, running: true, port: 7780, health: 200, has_dist: true, behind: 2, branch: 'feat/widgets', path: '/w/kirocrew-wt-widgets', last_updated_at: now - 3600 },
]
// Outage: rows come back, nothing is badged, and the payload says the state is
// UNKNOWN rather than empty.
const FLEET_UNKNOWN = {
  base_branch: 'main',
  gateway_service_active: false,
  manual_restart: 'kirocrew restart',
  live_state_known: false,
  staged_target: null,
  staged_cancel_available: false,
  worktrees: ROWS.map(r => ({ ...r, is_live: false, is_staged: false })),
}
// Healthy: the pointer was read; `main` is live.
const FLEET_KNOWN = {
  ...FLEET_UNKNOWN,
  live_state_known: true,
  worktrees: ROWS.map(r => ({ ...r, is_live: r.is_main, is_staged: false })),
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)
  let fleet = FLEET_UNKNOWN
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/apps/dev-fleet/api/fleet') { await json(route, fleet); return true }
      if (path === '/apps/dev-fleet/api/disk') { await json(route, { total_mb: 2048 }); return true }
      if (path.startsWith('/apps/dev-fleet/api/')) { await json(route, {}); return true }
      return false
    },
  })

  await page.goto(base + '/dev-fleet', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('fleet-live-state-unknown').waitFor({ state: 'visible', timeout: 15000 })
  await page.getByText('kirocrew-wt-widgets').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.screenshot({ path: `${OUT}/01-outage-notice-above-rows.png`, fullPage: false })

  fleet = FLEET_KNOWN
  await page.goto(base + '/dev-fleet', { waitUntil: 'domcontentloaded' })
  await page.getByText('kirocrew-wt-widgets').first().waitFor({ state: 'visible', timeout: 15000 })
  if (await page.getByTestId('fleet-live-state-unknown').count() !== 0) {
    throw new Error('notice rendered although live_state_known is true')
  }
  await page.screenshot({ path: `${OUT}/02-pointer-read-no-notice.png`, fullPage: false })

  await browser.close()
  srv.close()
  console.log(`wrote 2 screenshots to ${OUT}/`)
}

main().catch((e) => { console.error(e); process.exit(1) })
