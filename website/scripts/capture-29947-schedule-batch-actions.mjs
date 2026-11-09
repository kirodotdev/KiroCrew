/**
 * Screenshot harness for #29947: the Schedule batch bar's Actions menu.
 *
 * Runs the built SPA behind the shared static server with every /api/** call
 * answered from fixtures. Captures the menu open, the Run Now confirm, and the
 * partial-failure notice, in dark and light themes.
 *
 * Usage: node scripts/capture-29947-schedule-batch-actions.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/29947-schedule-batch-actions'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const SEED_JOBS = [
  { id: 'a1', name: 'Morning digest', schedule: '0 8 * * 1-5', message: 'Summarise overnight activity.', enabled: true, agent: 'kirocrew', last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200 },
  { id: 'b2', name: 'PR review sweep', schedule: 'every 1h', message: 'Check open PRs for new comments.', enabled: true, strict_schedule: true, agent: 'kirocrew', last_status: 'ok', last_run_ts: now - 600, next_run_ts: now + 3000 },
  { id: 'c3', name: 'Weekly report', schedule: '0 17 * * 5', message: 'Write the weekly report.', enabled: false, agent: 'kirocrew', last_run_ts: now - 86400 * 3, next_run_ts: null },
  { id: 'd4', name: 'Feed poller', schedule: 'every 300s', script: '~/.kiro/crew/crons/feed.py:check', message: '', enabled: true, is_running: true, last_run_ts: now - 30, next_run_ts: now + 270 },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function open(theme, { failToggle = false, folders = [], check = true } = {}) {
  // Each page gets its own copy, so an applied pause shows on the next /api/crons read.
  const JOBS = SEED_JOBS.map(j => ({ ...j }))
  const context = await browser.newContext({ viewport: { width: 1400, height: 560 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      if (path === '/api/crons') { await json(route, { jobs: JOBS }); return true }
      if (path === '/api/cron-folders') { await json(route, folders); return true }
      if (path === '/api/crons/history') { await json(route, { runs: [] }); return true }
      if (path === '/api/agents') { await json(route, { agents: [{ name: 'kirocrew' }], default_agent: 'kirocrew' }); return true }
      if (path === '/api/models') { await json(route, []); return true }
      if (path === '/api/members') { await json(route, { members: [] }); return true }
      if (/^\/api\/crons\/[^/]+\/enable$/.test(path)) {
        if (failToggle && path.includes('/d4/')) { await route.fulfill({ status: 500, body: 'boom' }); return true }
        const job = JOBS.find(j => path === `/api/crons/${j.id}/enable`)
        if (job) job.enabled = JSON.parse(route.request().postData() || '{}').enabled
        await json(route, {}); return true
      }
      return false
    },
  })
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByRole('table').waitFor({ timeout: 60000 })
  await page.getByText('Feed poller').first().waitFor()
  for (const name of check ? ['Morning digest', 'PR review sweep', 'Weekly report', 'Feed poller'] : []) {
    await page.getByRole('row').filter({ hasText: name }).getByRole('checkbox').click()
  }
  return { context, page }
}

const menuButton = (page) => page.getByRole('button', { name: 'Actions for the checked jobs' })

for (const theme of ['dark', 'light']) {
  const { context, page } = await open(theme)
  await menuButton(page).click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/1-menu-open-${theme}.png` })

  await page.getByRole('menuitem', { name: /Run Now/ }).click()
  await page.getByRole('dialog').waitFor()
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/2-run-now-confirm-${theme}.png` })
  await context.close()
}

{
  const { context, page } = await open('dark', { failToggle: true })
  await menuButton(page).click()
  await page.getByRole('menuitem', { name: /Pause/ }).click()
  await page.getByTestId('schedule-batch-action-error').waitFor()
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/3-partial-failure-dark.png` })
  await context.close()
}

{
  // A row's own ··· menu, on a strict job: its timing item uses the batch menu's names.
  const { context, page } = await open('light', { check: false })
  await page.getByRole('row').filter({ hasText: 'PR review sweep' }).getByRole('button', { name: 'Actions' }).click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/4-row-menu-light.png` })
  await context.close()
}

{
  const folders = [{ id: 'f1', name: 'Daily', order: 0 }, { id: 'f2', name: 'Reports', order: 1 }]
  const { context, page } = await open('dark', { folders })
  await menuButton(page).click()
  await page.getByRole('menuitem', { name: 'Move to folder' }).hover()
  await page.getByRole('menuitem', { name: 'New folder' }).waitFor()
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/5-move-to-folder-dark.png` })
  await context.close()
}

await browser.close()
srv.close()
console.log(`wrote ${OUT}`)
