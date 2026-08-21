/**
 * Screenshot harness for the Schedule page's owning-session line (#4815): the
 * Name cell renders the job's owning session as a second line — mono for an
 * owned job, explicit italic copy for an ownerless one — and the job detail
 * dialog shows the owner in full.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * The fixture deliberately pairs an OWNED and an OWNERLESS job in the same
 * table: the empty state is the point of the feature (it explains why a job is
 * invisible to cron_list in chat), and the two states must read differently on
 * one screen, not merely across two screenshots.
 *
 * Usage: node scripts/capture-schedule-cron-owner.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/schedule-cron-owner'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const JOBS = [
  {
    id: 'job-1', name: 'Nightly report', schedule: 'every 1d', timezone: 'America/Los_Angeles',
    message: 'Summarise yesterday\'s CI failures and post the digest to #build-health.',
    enabled: true, agent: 'kirocrew',
    session_key: 'web-4f2a9c81d7e3',
    last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200, has_result: true,
  },
  {
    id: 'job-2', name: 'Feed poller', schedule: 'every 300s', enabled: true,
    message: 'Poll the release feed and flag regressions.', agent: 'kirocrew',
    session_key: null,
    last_status: 'ok', last_run_ts: now - 240, next_run_ts: now + 60,
  },
  {
    // Long name AND long key: evidences that the name keeps its ellipsis with
    // the owner line underneath, and that a truncated key stays visually
    // distinct (mono) from the italic empty-state copy.
    id: 'job-3', name: 'Weekly compliance report for the infrastructure team', schedule: '0 9 * * 1',
    message: 'Compile the weekly compliance digest.', agent: 'kirocrew', enabled: true,
    session_key: 'slack:C0AP77JJSN6:1755772677.169219',
    last_status: 'ok', last_run_ts: now - 86400, next_run_ts: now + 6 * 86400,
  },
]

async function frameOwnerBlock(page) {
  // Scroll the dialog's scrollable body to its bottom, where the owner block
  // renders. Done by hand rather than scrollIntoViewIfNeeded because the
  // dialog body is the scroll container and Playwright's helper proved a
  // no-op against it; a `before` build simply shows the same tail fold.
  await page.locator('[role="dialog"]').first().evaluate(dialog => {
    for (const el of dialog.querySelectorAll('*')) {
      if (el.scrollHeight > el.clientHeight + 4) { el.scrollTop = el.scrollHeight; return }
    }
  })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    // 1x per the image-read ceiling (see capture-schedule-shadcn.mjs).
    viewport: { width: 1500, height: 900 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)

  // Fixture routes as a lookup map (the sibling harnesses use an if-chain;
  // a map keeps this block clone-free under the jscpd zero-duplication gate).
  const ROUTES = new Map([
    ['/api/crons', { jobs: JOBS }],
    ['/api/cron-folders', []],
    ['/api/crons/history', { runs: [] }],
    ['/api/agents', { agents: [{ name: 'kirocrew' }], default_agent: 'kirocrew' }],
    ['/api/models', []],
  ])
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (!ROUTES.has(path)) return false
      await json(route, ROUTES.get(path))
      return true
    },
  })

  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByRole('table').waitFor({ timeout: 15000 })
  await page.getByText('Nightly report').first().waitFor()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-table.png` })
  await page.locator('table').first().screenshot({ path: `${OUT}/${PREFIX}-table-crop.png` })

  // Detail dialog for the OWNED job: full un-truncated key. The owner block
  // sits below the form fold, so scroll it into view before framing.
  await page.getByRole('row').filter({ hasText: 'Nightly report' }).getByText('Nightly report').click()
  await page.locator('[role="dialog"]').first().waitFor({ timeout: 10000 })
  await frameOwnerBlock(page)
  await page.waitForTimeout(500)
  await page.locator('[role="dialog"]').first().screenshot({ path: `${OUT}/${PREFIX}-detail-owned.png` })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)

  // Detail dialog for the OWNERLESS job: the explicit empty-state copy.
  await page.getByRole('row').filter({ hasText: 'Feed poller' }).getByText('Feed poller').click()
  await page.locator('[role="dialog"]').first().waitFor({ timeout: 10000 })
  await frameOwnerBlock(page)
  await page.waitForTimeout(500)
  await page.locator('[role="dialog"]').first().screenshot({ path: `${OUT}/${PREFIX}-detail-ownerless.png` })

  await browser.close()
  srv.close()
  console.log(`wrote 4 frames to ${OUT}/ with prefix ${PREFIX}`)
}

main().catch(err => { console.error(err); process.exit(1) })
