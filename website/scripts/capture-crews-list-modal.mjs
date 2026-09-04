/**
 * Screenshot harness for the Crews roster's list view, description clamp and
 * modal editor.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Fixtures deliberately mix description lengths: one that overflows two lines,
 * one that fits on one, and one with none at all. A uniform fixture would hide
 * the two defects this change is about — the clamp leaking a third line, and
 * cards with short descriptions sitting shorter than their neighbours.
 *
 * Usage: node scripts/capture-crews-list-modal.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crews-list-modal'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  {
    name: 'kirocrew',
    kiro_agent: 'kirocrew',
    workspace: 'default',
    memory_store: 'default',
    description:
      'Paged-alert triage crew — owns the runbooks, keeps the escalation ladder ' +
      'warm, and files the follow-up tickets after every page so nothing falls ' +
      'through the gaps overnight.',
  },
  {
    name: 'oncall',
    kiro_agent: 'oncall',
    workspace: 'oncall',
    memory_store: 'default',
    description: 'Long-horizon literature and competitor scanning.',
    model: 'claude-opus-5',
  },
  {
    name: 'research',
    kiro_agent: 'kirocrew',
    workspace: 'research',
    memory_store: 'research',
  },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' }),
  })

  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  // Match either DOM so a `before` run against main still finishes instead of
  // hanging for the timeout and failing — that prefix argument is the point.
  await page.locator('#main-content [data-testid="crew-card"], #main-content tbody tr')
    .first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)

  const shot = []
  const save = async (name, locator) => {
    await (locator ?? page).screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    shot.push(`${PREFIX}-${name}.png`)
  }

  await save('cards', page.locator('#main-content'))

  // List view. Guarded so a `before` run against main, which has no toggle,
  // skips it rather than dying.
  const listBtn = page.getByRole('button', { name: 'List', exact: true })
  if (await listBtn.count()) {
    await listBtn.click()
    await page.getByRole('table').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(400)
    await save('list', page.locator('#main-content'))

    // Modal opened FROM a row — the path the row-vs-control click guard covers.
    await page.getByRole('button', { name: 'Edit crew oncall' }).click()
    await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(400)
    await save('modal-edit')

    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
    await page.getByRole('button', { name: 'Cards', exact: true }).click()
    await page.waitForTimeout(300)
  }

  // Create mode: the one path that shows the Name field, and the shortest modal.
  const newCrew = page.locator('[data-testid="new-crew"]')
  if (await newCrew.count()) {
    await newCrew.click()
    await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(400)
    await save('modal-create')
  }

  console.log(`wrote ${shot.map(f => `${OUT}/${f}`).join(', ')}`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
