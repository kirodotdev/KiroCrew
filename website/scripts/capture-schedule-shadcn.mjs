/**
 * Screenshot harness for the Schedule page's shadcn migration: the jobs table
 * built out of `ui/table` primitives, and the job detail view as a `ui/dialog`
 * modal instead of the old resizable side panel.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * The fixture deliberately spans all three job kinds (agent / script / command)
 * and both a foldered and an ungrouped group: the folder header, the ungrouped
 * divider and the data rows are three different row shapes in one table, and a
 * single-shape fixture would not show whether they still share one grid after
 * the migration.
 *
 * Usage: node scripts/capture-schedule-shadcn.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/schedule-shadcn'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const FOLDERS = [{ id: 'fld-prod', name: 'Production', color: 'accent' }]

const JOBS = [
  {
    id: 'job-1', name: 'Nightly report', schedule: 'every 1d', timezone: 'America/Los_Angeles',
    message: 'Summarise yesterday\'s CI failures and post the digest to #build-health.',
    enabled: true, folder_id: 'fld-prod', agent: 'kirocrew', model: 'claude-opus-5',
    last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200, has_result: true,
  },
  {
    id: 'job-2', name: 'Feed poller', schedule: 'every 300s', enabled: true, folder_id: 'fld-prod',
    script: '~/.kiro/crew/crons/feed.py:check', message: '',
    last_status: 'error', last_error: 'HTTP 502 from upstream', last_run_ts: now - 240,
    next_run_ts: now + 60, is_running: true, running_since: now - 20,
  },
  {
    id: 'job-3', name: 'Disk check', schedule: '0 9 * * 1', enabled: false,
    command: 'df -h / | tail -1', message: '',
    last_run_ts: now - 86400 * 2, next_run_ts: null,
  },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 900 },
    // 1x, not 2x. At 2x this harness produced 3000x1800 frames and 2500px-wide
    // element crops — past the 2000px-per-edge ceiling an agent's image read has
    // to respect (see the web-verify skill). 1500px is legible for review, and a
    // committed screenshot that nobody can safely read back is worse than a
    // slightly softer one.
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    // The stub awaits this handler's return value, so a bare `json(route, …)`
    // resolves to undefined and the default map fulfils the route a second
    // time ("Route is already handled!"). Await, then report handled.
    extra: async (path, route) => {
      if (path === '/api/crons') { await json(route, { jobs: JOBS }); return true }
      if (path === '/api/cron-folders') { await json(route, FOLDERS); return true }
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
  await page.screenshot({ path: `${OUT}/${PREFIX}-table.png` })
  // Tight crop of the jobs card: the full-page shot is mostly chrome, and the
  // header type scale / row separators are the thing being compared.
  await page.locator('table').first().screenshot({ path: `${OUT}/${PREFIX}-table-crop.png` })

  // Row click opens the detail view. Waits for EITHER DOM so a `before` run
  // against main finishes instead of timing out: there the detail view is a
  // side panel with no dialog role, which is the difference being captured.
  await page.getByRole('row').filter({ hasText: 'Nightly report' }).getByText('Nightly report').click()
  const detail = page.locator('[role="dialog"], [aria-label="Resize panel"]').first()
  await detail.waitFor({ timeout: 10000 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${PREFIX}-detail-dialog.png` })
  // The side panel has no role; crop its container by walking up from the
  // resize handle. The dialog crops directly.
  const detailSurface = page.locator('[role="dialog"]').first()
  const surface = (await detailSurface.count())
    ? detailSurface
    : page.locator('[aria-label="Resize panel"]').first().locator('..')
  await surface.screenshot({ path: `${OUT}/${PREFIX}-detail-crop.png` })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-table.png and ${OUT}/${PREFIX}-detail-dialog.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
