/**
 * Screenshot harness for #4120: the jobs-table Delete button's armed state at a
 * phone viewport. The armed state used to explain itself only through a hover
 * `title` tooltip; on touch there is no hover, so the user saw a bare "Confirm"
 * with no statement of what the second tap destroys. The fix puts the
 * explanation in the visible label (`confirm_delete_job` -> "Delete?"),
 * which is deliberately also the accessible name (no aria-label — it would
 * override the label a sighted user reads and break WCAG 2.5.3).
 *
 * Captured at 390x844 (the narrow-viewport baseline in AUTOSDE), where the
 * sticky Actions column keeps Delete visible at rest — the exposure that raised
 * this issue. Runs the REAL built SPA (website/dist) with every /api/** call
 * answered from fixtures — gateway-free. Same technique as
 * capture-schedule-shadcn.mjs.
 *
 * Labels are read from the CATALOG, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-armed-delete-touch.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/armed-delete-touch-4120'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const sp = manual.pages.schedulePage
const ARMED_LABEL = sp.confirm_delete_job          // "Delete?"
if (!ARMED_LABEL) throw new Error('catalog key confirm_delete_job missing — renamed?')

const now = Math.floor(Date.now() / 1000)
const JOBS = [
  {
    id: 'job-1', name: 'Nightly report', schedule: 'every 1d', timezone: 'America/Los_Angeles',
    message: 'Summarise yesterday\'s CI failures and post the digest to #build-health.',
    enabled: true, agent: 'kirocrew', model: 'claude-opus-5',
    last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200, has_result: true,
  },
  {
    id: 'job-2', name: 'Feed poller', schedule: 'every 300s', enabled: true,
    script: '~/.kiro/crew/crons/feed.py:check', message: '',
    last_status: 'ok', last_run_ts: now - 240, next_run_ts: now + 60,
  },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2, // phone-density; frames stay ~780px wide, well under the 2000px read ceiling
    hasTouch: true,
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/crons') { await json(route, { jobs: JOBS }); return true }
      if (path === '/api/cron-folders') { await json(route, []); return true }
      if (path === '/api/crons/history') { await json(route, { runs: [] }); return true }
      if (path === '/api/agents') { await json(route, { agents: [{ name: 'kirocrew' }], default_agent: 'kirocrew' }); return true }
      if (path === '/api/models') { await json(route, []); return true }
      return false
    },
  })

  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByRole('table').waitFor({ timeout: 15000 })
  await page.getByText('Nightly report').first().waitFor()
  await page.waitForTimeout(400)

  const row = page.getByRole('row').filter({ hasText: 'Nightly report' })

  // Disarmed: plain Delete, visible at rest thanks to the sticky Actions column.
  await page.screenshot({ path: `${OUT}/disarmed-phone.png` })

  // Arm it. On this touch context the tap is the first of the two clicks.
  await row.getByRole('button', { name: 'Delete', exact: true }).tap()

  // The visible label IS the accessible name (deliberately no aria-label —
  // WCAG 2.5.3 Label in Name) and must state the action — assert it so the
  // capture cannot silently photograph a regression.
  const armed = row.getByRole('button', { name: ARMED_LABEL, exact: true })
  await armed.waitFor({ timeout: 5000 })
  const aria = await armed.getAttribute('aria-label')
  if (aria !== null) throw new Error(`armed button carries aria-label ${JSON.stringify(aria)} — overrides the visible label (WCAG 2.5.3)`)

  await page.screenshot({ path: `${OUT}/armed-phone.png` })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/disarmed-phone.png and ${OUT}/armed-phone.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
