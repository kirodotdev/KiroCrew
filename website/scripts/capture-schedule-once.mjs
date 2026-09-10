/**
 * Screenshot harness for scheduling a one-shot from the UI.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * Four frames, covering the surfaces this change adds:
 *   1. plus menu open               -> the "Send later" row among its peers
 *   2. picker open                  -> the time control and its confirm
 *   3. scheduled-message banner     -> what makes a pending message findable again
 *   4. Schedule form in once mode   -> the fourth schedule arm, seeded from a job
 *
 * The Schedule frame is driven from a FIXTURE job carrying `at_ts` rather than by filling the
 * create form, because the defect this change closes is on the READ path: before
 * the API reported `at_ts`, such a job opened in cron mode with an empty
 * expression and could not be saved at all. Photographing the seeded job is what
 * shows the arm doing its job.
 *
 * Usage: node scripts/capture-schedule-once.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/schedule-once-shots'
mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
// Fixed so the frames are reproducible: a relative time would render a different
// wall clock on every run and make two captures of unchanged code differ.
// A fixed FUTURE instant: the banner shows only jobs whose fire time has not
// passed, so a fixture in the past renders no banner at all. Pinned rather than
// relative so two captures of unchanged code compare equal.
const AT_TS = Date.parse('2027-06-01T09:30:00Z') / 1000

const ONE_SHOT = {
  id: 'j-once',
  name: 'remind me about the release',
  message: 'Check whether the release notes landed and summarize what shipped.',
  schedule: 'once at 2027-06-01 09:30',
  at_ts: AT_TS,
  delete_after_run: true,
  enabled: true,
  cron_expr: null,
  every_secs: null,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function newPage({ crons = [] } = {}) {
  // Timezone pinned with the locale: the banner and the picker both render a wall
  // clock, so an unpinned runner zone would change the frames without any code
  // change.
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1, timezoneId: 'UTC' })
  const page = await context.newPage()
  const extra = async (path, route) => {
    if (path === '/api/crons') {
      await json(route, { jobs: crons })
      return true
    }
    return false
  }
  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 2, running: false, agent: '', mode: '' }],
    extra,
  })
  // Pin the locale: without it the SPA negotiates one from the environment and the
  // shot comes out in whatever language the runner happens to pick.
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-lang', 'en')
  }, SLOT)
  return { context, page }
}

async function shot(page, name) {
  await page.screenshot({ path: join(OUT, name) })
  console.log('wrote', join(OUT, name))
}

// --- Frames 1-2: the plus-menu row and its picker ---------------------------
{
  const { context, page } = await newPage()
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  // The row requires a draft, since the scheduled job's message IS the draft.
  const composer = page.locator('textarea').first()
  await composer.fill('Ping the team about the release notes')
  await page.waitForTimeout(300)

  // Send later lives in the plus menu rather than on a caret beside Send: the idle
  // action row already carries mic + Optimize + Send, and max-two-buttons-per-row
  // rejects widening or wrapping it.
  await page.getByRole('button', { name: 'Add files & options' }).click()
  await page.waitForTimeout(400)
  await shot(page, '01-plus-menu-send-later.png')

  await page.getByTestId('plus-menu-send-later').click()
  await page.waitForTimeout(400)
  await shot(page, '02-picker.png')
  await context.close()
}

// --- Frame 3: the scheduled-message banner ----------------------------------
{
  // Seeded as an existing pending job for THIS slot, which is the state the banner
  // exists for: a message scheduled earlier is otherwise invisible in the chat.
  const { context, page } = await newPage({
    crons: [{ ...ONE_SHOT, session_key: `dashboard:${SLOT}` }],
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2800)
  await shot(page, '03-scheduled-banner.png')
  await context.close()
}

// --- Frame 4: the Schedule form's once arm ----------------------------------
{
  const { context, page } = await newPage({ crons: [ONE_SHOT] })
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  // Open the seeded job's editor. Clicking its row is how a user gets here.
  await page.getByText(ONE_SHOT.name, { exact: false }).first().click()
  await page.waitForTimeout(800)
  await shot(page, '04-schedule-once-arm.png')
  await context.close()
}

await browser.close()
srv.close()
