/**
 * Screenshot harness for #38258: the Shared Crew row on Settings > Developer >
 * Feature Previews without the always-on warn "Restart" badge.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, answers /api/** from fixtures, and answers /api/shared-crew/preview
 * with a stored choice that matches the running gateway (nothing pending) or
 * one that does not (the pending notice shows).
 *
 * Frames: idle-{light,dark}.png, pending-{light,dark}.png, each cropped to the
 * Crew and Bots preview group.
 *
 * Usage: node scripts/capture-38258-shared-crew-restart-hint.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/38258-shared-crew-restart-hint'
mkdirSync(OUT, { recursive: true })

const SETTINGS = {
  idle: { enabled: true, active: true, env_override: false, restart_required: false },
  pending: { enabled: true, active: false, env_override: false, restart_required: true },
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []
  for (const [state, setting] of Object.entries(SETTINGS)) {
    for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, { theme })
      // Registered after the stub, so it is matched first.
      await page.route('**/api/shared-crew/preview', route => route.fulfill({ json: setting }))
      await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
      const row = page.locator('[data-setting-label="Shared Crew"]')
      await row.waitFor({ state: 'visible', timeout: 15000 })
      await page.waitForTimeout(600) // let the cards' rise animation finish
      const group = row.locator('xpath=ancestor::div[.//h3 or .//h4][1]')
      const target = (await group.count()) ? group : row.locator('xpath=../..')
      await target.scrollIntoViewIfNeeded()
      const name = `${state}-${theme}.png`
      await target.screenshot({ path: `${OUT}/${name}` })
      shot.push(name)
      await context.close()
    }
  }
  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
}

main().catch(err => { console.error(err); process.exit(1) })
