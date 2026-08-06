/**
 * Screenshot harness for the crew-bindings isolation preview notice.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Shoots the three surfaces the notice reaches, because no single one covers it:
 * the card roster (page-level banner), the list view (a "?" on the Workspace and
 * Memory Store column headers), and the editor sheet with the Workspace tip
 * open — the sheet is an overlay, so the banner is unreadable from there and the
 * tooltip is the only path the caveat has to a user mid-edit.
 *
 * Usage: node scripts/capture-crew-preview-notice.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before). On a main
 *   build every locator here is absent, so `before` shoots the plain roster.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../.github/screenshots/crew-preview-notice'
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
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, { extra: crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' }) })

  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  const main$ = page.locator('#main-content')
  await main$.locator('[data-testid="crew-card"], tbody tr')
    .first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400) // let the roster settle before the shot

  const shot = []
  const save = async (name) => {
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    shot.push(`${PREFIX}-${name}.png`)
  }

  await save('cards')

  // List view: the caveat rides the two column headers it is about.
  const list = main$.getByRole('button', { name: 'List' })
  if (await list.count()) {
    await list.click()
    await main$.locator('table').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(300)
    await save('list')
    await main$.getByRole('button', { name: 'Cards' }).click()
    await main$.locator('[data-testid="crew-card"]').first().waitFor({ state: 'visible' })
  }

  // Editor sheet with the Workspace tip expanded. Guarded so a `before` run
  // against main, which has neither the sheet nor the tip, still finishes.
  const firstCard = main$.locator('[data-testid="crew-card"]').first()
  if (await firstCard.count()) {
    await firstCard.click()
    const sheet = page.getByRole('dialog')
    await sheet.waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(400) // the sheet slides in over 240ms
    const tips = sheet.locator('button[title*="Isolated memory per crew"]')
    if (await tips.count()) {
      await tips.first().click()
      await page.waitForTimeout(200)
    }
    await save('editor')
  }

  // The STOCK install: one memory store, so the pre-existing "only the default
  // store is available today" note under the select is visible. That note and
  // this change's tip make adjacent claims, and the fixture above hides the
  // state entirely because it declares two stores — so shoot it explicitly
  // rather than leaving the common case unevidenced.
  const stock = await context.newPage()
  logPageProblems(stock)
  await stubDashboardApi(stock, {
    extra: crewsApi({
      crews: [{ name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' }],
      defaultAgent: 'kirocrew',
    }),
  })
  await stock.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  const stockCard = stock.locator('#main-content [data-testid="crew-card"]').first()
  // waitFor BEFORE count(): a locator that has not rendered yet counts 0, so
  // counting first would silently skip the shot on a slower boot.
  await stockCard.waitFor({ state: 'visible', timeout: 15000 })
  await stockCard.click()
  await stock.getByRole('dialog').waitFor({ state: 'visible', timeout: 15000 })
  await stock.waitForTimeout(500)
  await stock.screenshot({ path: `${OUT}/${PREFIX}-editor-single-store.png` })
  shot.push(`${PREFIX}-editor-single-store.png`)

  console.log(`wrote ${shot.map(f => `${OUT}/${f}`).join(', ')}`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
