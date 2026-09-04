/**
 * Screenshot harness for the "Agent Template is required" create-sheet change.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Shoots the three states that carry the change, because none alone shows it:
 * the sheet as it OPENS (the template now reads a placeholder instead of a
 * pre-filled `kirocrew`, which is the whole defect — a pre-filled built-in made
 * every untouched crew an alias for the default agent), the dropdown OPEN (so the
 * shot proves the real options are reachable rather than that the field is inert),
 * and the REFUSAL after pressing Create without choosing one.
 *
 * Each state gets a FRESH PAGE. Reusing one page across them does not work here:
 * Escape on an open Radix select inside this dialog closes the dialog too (see
 * components/ui/select.tsx), and the overlay teardown then keeps intercepting
 * pointer events, so the next `new-crew` click never lands.
 *
 * Usage: node scripts/capture-crew-template-required.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before). On a main
 *   build the placeholder and the refusal are both absent, so `before` shows the
 *   pre-filled template the change removes.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-template-required'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'default' },
]
const INSTALLED = ['kirocrew', 'oncall-agent', 'reviewer']

const API = crewsApi({
  crews: CREWS,
  defaultAgent: 'kirocrew',
  installed: INSTALLED,
  workspaces: ['default', 'oncall', 'research'],
  memoryStores: ['default', 'research'],
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })

  /** Fresh page with the create sheet already open through the real control. */
  async function openSheet() {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { extra: API })
    await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
    // Wait on a REAL locator so a blank page fails loudly instead of quietly
    // producing an empty screenshot.
    await page.locator('#main-content')
      .locator('[data-testid="crew-card"], tbody tr')
      .first().waitFor({ state: 'visible', timeout: 15000 })
    await page.getByTestId('new-crew').click()
    const sheet = page.getByRole('dialog', { name: 'Create a new crew' })
    await sheet.waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(400) // let the slide-in settle
    return { page, sheet }
  }

  const save = async (page, name) => {
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    console.log('wrote', `${PREFIX}-${name}.png`)
  }

  // 1. The sheet as it opens — template unselected, placeholder visible.
  {
    const { page } = await openSheet()
    await save(page, '01-sheet-template-unselected')
    await page.close()
  }

  // 2. The dropdown open — the real options are reachable.
  {
    const { page, sheet } = await openSheet()
    await sheet.getByRole('combobox', { name: 'Agent Template' }).click()
    await page.getByRole('option', { name: 'reviewer', exact: true })
      .waitFor({ state: 'visible', timeout: 10000 })
    await save(page, '02-template-options-open')
    await page.close()
  }

  // 3. The refusal — name filled, no template, Create pressed.
  {
    const { page, sheet } = await openSheet()
    await sheet.getByPlaceholder('e.g. oncall').fill('researcher')
    await sheet.getByRole('button', { name: 'Create', exact: true }).click()
    await sheet.getByText('Agent Template is required')
      .waitFor({ state: 'visible', timeout: 10000 })
    await save(page, '03-refusal-template-required')
    await page.close()
  }

  await browser.close()
  await new Promise(res => srv.close(res))
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
