/**
 * Evidence harness for the job-detail dialog's Last Error / Last Output surface.
 *
 * Sibling of capture-schedule-column-resize.mjs and deliberately separate: that
 * one photographs the TABLE and measures resize arithmetic, this one photographs
 * the DIALOG. Same rig -- the real built SPA (website/dist) behind the shared
 * in-process static server, every /api/** answered from fixtures via Playwright
 * route interception, so gateway-free and auth-free.
 *
 * What it exists to show: the detail dialog used to gate its error notice and
 * its last-output block on `job.script`, so a `command` job -- which has no
 * script -- reached the dialog with no in-page surface for either, and the
 * failure was readable only as hover text on the row. Both gates are gone, so
 * BOTH shots are of `command` jobs: the gate's absence is the whole point, and
 * a script job would photograph identically before and after the change.
 *
 * It also pins the row tooltip, which a screenshot cannot show: a failed row
 * must carry NO title at all. `last_result` is the PREVIOUS run's output, so
 * leaving it on a failed row makes a red Error badge read as a success on
 * hover. The script exits non-zero if that regresses.
 *
 * Usage: node scripts/capture-schedule-job-error-surface.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/schedule-column-resize'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

/** Both are `command` jobs: no `script`, which is exactly the case the old guard excluded. */
const FAILED = {
  id: 'b4e1d7a0-31c2',
  name: 'Nightly database backup',
  schedule: '0 2 * * *',
  command: 'pg_dump --format=custom kirocrew > /backups/nightly.dump',
  message: '',
  enabled: true,
  last_status: 'error',
  last_error:
    'pg_dump: error: connection to server at "db.internal" (10.0.4.21), port 5432 failed:\n'
    + '        FATAL:  remaining connection slots are reserved for non-replication superuser connections\n'
    + 'pg_dump: error: could not write to output file: No space left on device\n'
    + 'exit status 1',
  last_result: 'backup completed: 412 MB in 38s',
  last_run_ts: now - 1800,
  next_run_ts: now + 84600,
}

const SUCCEEDED = {
  id: 'f09c2b56-8ad4',
  name: 'Log rotation and prune',
  schedule: 'every 6h',
  command: 'logrotate -f /etc/logrotate.d/kirocrew',
  message: '',
  enabled: true,
  last_status: 'ok',
  last_result:
    'rotating pattern: /var/log/kirocrew/*.log  weekly (4 rotations)\n'
    + 'considering log /var/log/kirocrew/gateway.log\n'
    + '  log needs rotating\n'
    + 'pruned 3 archives older than 28d, reclaimed 118 MB',
  last_run_ts: now - 900,
  next_run_ts: now + 20700,
}

const failures = []
function check(ok, message) {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${message}`)
  if (!ok) failures.push(message)
}

const VIEWPORT = { width: 1500, height: 1400 }

async function schedulePageWith(browser, jobs, theme) {
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    preserveStorage: true,
    extra: async (path, route) => {
      if (path === '/api/crons') { await json(route, { jobs }); return true }
      if (path === '/api/cron-folders') { await json(route, []); return true }
      if (path === '/api/crons/history') { await json(route, { runs: [] }); return true }
      if (path === '/api/agents') { await json(route, { agents: [{ name: 'kirocrew' }], default_agent: 'kirocrew' }); return true }
      if (path === '/api/models') { await json(route, []); return true }
      return false
    },
  })
  return { context, page }
}

/**
 * Make sure a surface will actually be IN the screenshot, and prove it.
 *
 * The first cut of this harness shipped a shot whose Last Error notice was
 * clipped to its top edge -- evidence of nothing, for the one claim the shot
 * exists to support. Scrolling alone is not the property: when the dialog fits
 * the viewport there is nothing to scroll and scrollTop stays 0, which an
 * assertion on scrolling reads as failure on the good case. What matters is
 * geometric -- the element's box lies inside the dialog's box -- so that is
 * what is asserted, after a best-effort scroll for when the dialog does not fit.
 */
async function revealInDialog(page, locator) {
  await page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]')
    const scroller = dialog && Array.from(dialog.querySelectorAll('*')).find(
      el => el.scrollHeight > el.clientHeight + 8 && getComputedStyle(el).overflowY !== 'visible')
    if (scroller) scroller.scrollTop = scroller.scrollHeight
  })
  await page.waitForTimeout(250)
  const box = await locator.boundingBox()
  const dlg = await page.getByRole('dialog').boundingBox()
  if (!box || !dlg) return false
  return box.y >= dlg.y && box.y + box.height <= dlg.y + dlg.height
}

/** Open a job's detail dialog the way a user does: click its row. */
async function openDetail(page, base, jobName) {
  await page.goto(`${base}/schedule`)
  await page.getByText(jobName).first().waitFor()
  await page.getByText(jobName).first().click()
  await page.getByRole('dialog').waitFor()
  await page.waitForTimeout(250)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  // --- a FAILED command job: the Last Error notice must be in the dialog -----
  {
    const { context, page } = await schedulePageWith(browser, [FAILED, SUCCEEDED], 'dark')
    await openDetail(page, base, FAILED.name)

    const notice = page.getByTestId('schedule-job-last-error')
    check(await notice.isVisible(), 'failed COMMAND job: Last Error notice is in the dialog')
    check(
      (await notice.textContent())?.includes('No space left on device'),
      'failed COMMAND job: the notice carries the real error text',
    )
    // The dialog body scrolls and the notice sits below the form, so an
    // unscrolled shot catches its top edge only -- which is no evidence of the
    // surface at all. Bring it into view before photographing it.
    check(await revealInDialog(page, notice), 'the Last Error notice is fully inside the captured frame')
    await page.getByRole('dialog').screenshot({ path: `${OUT}/${PREFIX}-9-command-last-error.png` })
    await context.close()
  }

  // --- a SUCCEEDED command job: the Last Output block must be in the dialog --
  {
    const { context, page } = await schedulePageWith(browser, [SUCCEEDED, FAILED], 'dark')
    await openDetail(page, base, SUCCEEDED.name)

    const body = await page.getByRole('dialog').textContent()
    check(body?.includes('reclaimed 118 MB'), 'succeeded COMMAND job: Last Output is in the dialog')
    check(
      !(await page.getByTestId('schedule-job-last-error').count()),
      'succeeded COMMAND job: no error notice on a job that did not fail',
    )
    check(
      await revealInDialog(page, page.getByText('reclaimed 118 MB')),
      'the Last Output block is fully inside the captured frame',
    )
    await page.getByRole('dialog').screenshot({ path: `${OUT}/${PREFIX}-10-command-last-output.png` })
    await context.close()
  }

  // --- the row tooltip, which no screenshot can show -------------------------
  {
    const { context, page } = await schedulePageWith(browser, [FAILED, SUCCEEDED], 'dark')
    await page.goto(`${base}/schedule`)
    await page.getByText(FAILED.name).first().waitFor()
    const titles = await page.evaluate(() =>
      Array.from(document.querySelectorAll('[title]')).map(el => el.getAttribute('title')))
    check(
      !titles.some(t => t && t.includes('No space left on device')),
      'row tooltip: a failed row does not carry the error text',
    )
    check(
      !titles.some(t => t && t.includes('backup completed')),
      'row tooltip: a failed row does not carry the PREVIOUS run\'s success either',
    )
    check(
      titles.some(t => t && t.includes('reclaimed 118 MB')),
      'row tooltip: a row that did not fail still carries its output',
    )
    await context.close()
  }

  await browser.close()
  srv.close()

  if (failures.length) {
    console.error(`\n${failures.length} check(s) failed:`)
    for (const f of failures) console.error(`  - ${f}`)
    process.exit(1)
  }
  console.log('\nAll checks passed.')
}

main()
