/**
 * Screenshot harness for the Dev Fleet "Cancel staged cutover" controls.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Shoots the three surfaces the cancel reaches: the live main row's inline
 * "Cancel cutover" button while a cutover is staged, its confirm dialog, and
 * the "Cancel staged cutover" item in a live FEATURE row's overflow menu (the
 * live checkout is not always main, so the menu path needs its own evidence).
 *
 * Usage: node scripts/capture-devfleet-cancel-cutover.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/devfleet-cancel-cutover-1724'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

/** Main is live, a feature worktree is staged: the inline-button surface. */
const FLEET_MAIN_LIVE = {
  base_branch: 'main',
  gateway_service_active: false,
  manual_restart: 'kirocrew restart',
  staged_target: '/w/kirocrew-wt-widgets',
  staged_cancel_available: true,
  worktrees: [
    { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', is_live: true, path: '/w/main', last_updated_at: now - 900 },
    { name: 'kirocrew-wt-widgets', is_main: false, running: false, has_dist: true, behind: 2, branch: 'feat/widgets', is_live: false, is_staged: true, path: '/w/kirocrew-wt-widgets', last_updated_at: now - 3600 },
  ],
}

/** A feature worktree is live, main is staged: the row-menu surface. */
const FLEET_FEATURE_LIVE = {
  base_branch: 'main',
  gateway_service_active: false,
  manual_restart: 'kirocrew restart',
  staged_target: '/w/main',
  staged_cancel_available: true,
  worktrees: [
    { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', is_live: false, is_staged: true, path: '/w/main', last_updated_at: now - 900 },
    { name: 'kirocrew-wt-widgets', is_main: false, running: false, has_dist: true, behind: 2, branch: 'feat/widgets', is_live: true, is_staged: false, path: '/w/kirocrew-wt-widgets', last_updated_at: now - 3600 },
  ],
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

  let fleet = FLEET_MAIN_LIVE
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/apps/dev-fleet/api/fleet') { await json(route, fleet); return true }
      if (path === '/apps/dev-fleet/api/disk') { await json(route, { total_mb: 2048 }); return true }
      if (path.startsWith('/apps/dev-fleet/api/')) { await json(route, {}); return true }
      return false
    },
  })

  // 1) Live main row: inline "Cancel cutover" button next to the staged fleet.
  await page.goto(base + '/dev-fleet', { waitUntil: 'domcontentloaded' })
  const cancelBtn = page.getByRole('button', { name: 'Cancel staged cutover' }).first()
  await cancelBtn.waitFor({ state: 'visible', timeout: 15000 })
  await page.screenshot({ path: `${OUT}/${PREFIX}-live-main-row-button.png`, fullPage: false })

  // 2) The confirm dialog: names the staged worktree and promises no restart.
  await cancelBtn.click()
  await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 5000 })
  await page.screenshot({ path: `${OUT}/${PREFIX}-cancel-confirm-dialog.png`, fullPage: false })
  await page.getByRole('dialog').getByRole('button', { name: 'Keep cutover' }).click()

  // 3) Live feature row: "Cancel staged cutover" in the overflow menu.
  fleet = FLEET_FEATURE_LIVE
  await page.goto(base + '/dev-fleet', { waitUntil: 'domcontentloaded' })
  await page.getByText('kirocrew-wt-widgets').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.getByLabel('More actions').click()
  await page.getByRole('menu').getByText('Cancel staged cutover').waitFor({ state: 'visible', timeout: 5000 })
  await page.screenshot({ path: `${OUT}/${PREFIX}-live-feature-row-menu.png`, fullPage: false })
  await page.keyboard.press('Escape')

  // 3b) The STAGED row's own menu offers the same cancel — co-located with
  // the "Restart pending" badge that prompts it.
  fleet = FLEET_MAIN_LIVE
  await page.goto(base + '/dev-fleet', { waitUntil: 'domcontentloaded' })
  await page.getByText('kirocrew-wt-widgets').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.getByLabel('More actions').first().click()
  await page.getByRole('menu').getByText('Cancel staged cutover').waitFor({ state: 'visible', timeout: 5000 })
  await page.screenshot({ path: `${OUT}/${PREFIX}-staged-row-menu.png`, fullPage: false })
  await page.keyboard.press('Escape')

  // 4) Foreground-eligible host (Restart offered AND cancel accepted): the two
  // gateway actions collapse into one overflow trigger beside Pull+Build,
  // keeping the main row under the two-button cap.
  fleet = { ...FLEET_MAIN_LIVE, gateway_service_active: true }
  await page.goto(base + '/dev-fleet', { waitUntil: 'domcontentloaded' })
  await page.getByText('kirocrew-wt-widgets').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.getByLabel('More actions').first().click()
  await page.getByRole('menu').getByText('Cancel staged cutover').waitFor({ state: 'visible', timeout: 5000 })
  await page.screenshot({ path: `${OUT}/${PREFIX}-live-main-row-collapsed-menu.png`, fullPage: false })

  await browser.close()
  srv.close()
  console.log(`wrote 5 screenshots to ${OUT}/`)
}

main().catch((e) => { console.error(e); process.exit(1) })
