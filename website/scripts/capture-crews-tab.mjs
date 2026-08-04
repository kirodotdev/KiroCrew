/**
 * Screenshot harness for Agent Capabilities > Crews (the renamed first tab).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Proves the two things the rename touches: the side-nav tab label ("Crews")
 * and the tab description under the content header. Fixtures seed a few crews
 * so the roster is populated rather than an empty state.
 *
 * Usage: node scripts/capture-crews-tab.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../.github/screenshots/crews-tab'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'oncall', workspace: 'oncall', memory_store: 'default' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'research', memory_store: 'research' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px nav type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' }),
  })

  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  // The tab label is the assertion, so fail loudly rather than shoot a blank page.
  const tab = page.locator('#main-content nav').getByRole('button', { name: 'Crews', exact: true })
  await tab.waitFor({ state: 'visible', timeout: 15000 })
  await page.locator('#main-content').getByText('Crews you chat with', { exact: false })
    .first().waitFor({ state: 'visible', timeout: 15000 })
  // Roster content, in whichever DOM the build under test uses: the redesign's
  // cards or main's table rows. Matching both is deliberate -- the `before` run
  // builds from main, and a card-only wait would hang it for 15s and then fail,
  // which would defeat the prefix argument this script exists for.
  await page.locator('#main-content [data-testid="crew-card"], #main-content tbody tr')
    .first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400) // let the roster settle before the shot

  await page.screenshot({ path: `${OUT}/${PREFIX}-crews-tab.png` })
  await page.locator('#main-content nav').screenshot({ path: `${OUT}/${PREFIX}-crews-nav.png` })

  // The editor sheet is the other half of the redesign, so shoot it too --
  // guarded, because main has no sheet and the `before` run must still finish.
  const shot = [`${PREFIX}-crews-tab.png`, `${PREFIX}-crews-nav.png`]
  const firstCard = page.locator('[data-testid="crew-card"]').first()
  if (await firstCard.count()) {
    await firstCard.click()
    await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(400) // the sheet slides in over 240ms
    await page.screenshot({ path: `${OUT}/${PREFIX}-crews-editor.png` })
    shot.push(`${PREFIX}-crews-editor.png`)
  }

  console.log(`wrote ${shot.map(f => `${OUT}/${f}`).join(', ')}`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
