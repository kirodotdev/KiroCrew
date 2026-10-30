/**
 * Layout-semantics gate for the job-detail dialog's run surfaces (#21061).
 *
 * Companion of capture-schedule-job-error-surface.mjs and deliberately
 * separate: that one photographs the dialog, this one MEASURES it. jsdom
 * cannot run the flex algorithm, so the vitest suite can only pin the DOM
 * shape the guarantee is made of; this harness asserts the guarantee itself
 * in a real browser, against the real built SPA (website/dist) behind the
 * shared in-process static server with every /api/** answered from fixtures.
 *
 * The collapse it exists to catch: the Last Error notice is a flex child of
 * the DialogBody column, and without a shrink floor the column crushes it to
 * a ~16px one-line sliver on a short viewport -- measured, not hypothetical
 * (tmack8001's number, reproduced at `1500x700` before the fix). Two guards
 * hold it now, and BOTH are asserted:
 *
 *   1. `shrink-0` on the notice itself -- the notice holds
 *      min(45vh, 420px) at every viewport height.
 *   2. the plain block wrapper around it -- a visible-overflow block has an
 *      automatic content-height `min-height`, so it reserves the notice's
 *      space in the flex column EVEN WHEN the notice's own `shrink-0` is
 *      lost. Simulated here by forcing `flex-shrink: 1 !important` back onto
 *      the notice: if the wrapper is ever removed, mode 2 goes red.
 *
 * Usage: node scripts/verify-schedule-error-surface-layout.mjs   (needs `npm run build` first)
 */
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const now = Math.floor(Date.now() / 1000)
/** Tall enough that the cap (not the content) decides the notice's height. */
const TRACEBACK = Array.from(
  { length: 40 },
  (_, i) => `File "/opt/app/pipeline/step_${i}.py", line ${100 + i}, in run_${i}\n    payload = transform(chunk_${i}, opts)`,
).join('\n') + '\nValueError: column "user_id" violates not-null constraint'

const FAILED = {
  id: 'b4e1d7a0-31c2',
  name: 'Nightly database backup',
  schedule: '0 2 * * *',
  command: 'pg_dump --format=custom kirocrew > /backups/nightly.dump',
  message: '',
  enabled: true,
  last_status: 'error',
  last_error: TRACEBACK,
  last_result: '',
  last_run_ts: now - 1800,
  next_run_ts: now + 84600,
}

/** Same shape on a succeeded job: the Last Output `<pre>` carries the same cap. */
const OUTPUT = Array.from({ length: 60 }, (_, i) => `rotating pattern: set ${i}, log /var/log/kirocrew/service-${i}.log rotated, ${120 + i} MB reclaimed`).join('\n')
const SUCCEEDED = {
  id: 'f09c2b56-8ad4',
  name: 'Log rotation and prune',
  schedule: 'every 6h',
  command: 'logrotate -f /etc/logrotate.d/kirocrew',
  message: '',
  enabled: true,
  last_status: 'ok',
  last_result: OUTPUT,
  last_run_ts: now - 900,
  next_run_ts: now + 20700,
}

const failures = []
function check(ok, message) {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${message}`)
  if (!ok) failures.push(message)
}

/** The cap's absolute arm. The viewport arm (45vh) is computed per height. */
const CAP_ABS = 420
const capFor = viewportHeight => Math.min(0.45 * viewportHeight, CAP_ABS)
/** Anything within 2px of the cap reads as "cap held"; sub-pixel rounding and
 *  borders eat a pixel or two. The collapse mode this gate exists for is 16px,
 *  three orders of magnitude below the tolerance. */
const TOLERANCE = 2

async function schedulePage(browser, jobs, viewport) {
  const context = await browser.newContext({ viewport })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: async (path, route) => {
      if (path === '/api/crons') { await json(route, { jobs }); return true }
      if (path === '/api/cron-folders') { await json(route, []); return true }
      if (path === '/api/crons/history') { await json(route, { runs: [] }); return true }
      if (path === '/api/agents') { await json(route, { agents: [{ name: 'kirocrew' }], default_agent: 'kirocrew' }); return true }
      if (path === '/api/models') { await json(route, []); return true }
      return false
    },
  })
  await page.goto(`${base}/schedule`)
  await page.getByText(jobs[0].name).first().waitFor()
  await page.getByText(jobs[0].name).first().click()
  await page.getByRole('dialog').waitFor()
  await page.waitForTimeout(250)
  return { context, page }
}

/**
 * Bring the surface into the scroller's viewport and measure it.
 * Returns { clientHeight, scrollHeight, overflowY } for the element.
 */
async function measure(page, selector) {
  await page.evaluate(sel => {
    const el = document.querySelector(sel)
    const body = el?.closest('.overflow-y-auto')
    if (body) body.scrollTop = body.scrollHeight
  }, selector)
  await page.waitForTimeout(150)
  return page.evaluate(sel => {
    const el = document.querySelector(sel)
    if (!el) return null
    const cs = getComputedStyle(el)
    return { clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, overflowY: cs.overflowY, flexShrink: cs.flexShrink }
  }, selector)
}

/** Force the notice's shrink floor off, the way a lost `shrink-0` class would. */
async function hostile(page) {
  await page.addStyleTag({ content: '[data-testid="schedule-job-last-error"], [data-testid="schedule-job-last-output"] { flex-shrink: 1 !important; }' })
  await page.waitForTimeout(150)
}

const NOTICE_SEL = '[data-testid="schedule-job-last-error"]'
const OUTPUT_SEL = '[data-testid="schedule-job-last-output"]'

const VIEWPORTS = [[1500, 1400], [1500, 900], [1500, 700], [1500, 500]]

const dist = await serveDist()
const base = dist.base
const browser = await chromium.launch()

for (const [width, height] of VIEWPORTS) {
  // --- mode 1: the notice's own shrink floor --------------------------------
  {
    const { context, page } = await schedulePage(browser, [FAILED, SUCCEEDED], { width, height })
    const m = await measure(page, NOTICE_SEL)
    const cap = capFor(height)
    check(m && m.flexShrink === '0', `${width}x${height}: the notice carries flex-shrink:0`)
    check(
      m && m.clientHeight >= cap - TOLERANCE,
      `${width}x${height}: the notice renders at the cap (clientHeight ${m?.clientHeight} >= ${cap - TOLERANCE})`,
    )
    check(
      m && (m.scrollHeight <= m.clientHeight || m.overflowY === 'auto'),
      `${width}x${height}: longer-than-cap content scrolls INSIDE the notice, not the page`,
    )
    await context.close()
  }

  // --- mode 2: the wrapper's structural floor, with shrink-0 lost -----------
  {
    const { context, page } = await schedulePage(browser, [FAILED, SUCCEEDED], { width, height })
    await hostile(page)
    const m = await measure(page, NOTICE_SEL)
    const cap = capFor(height)
    check(
      m && m.clientHeight >= cap - TOLERANCE,
      `${width}x${height}: with shrink-0 forced off, the block wrapper still holds the cap (clientHeight ${m?.clientHeight} >= ${cap - TOLERANCE})`,
    )
    await context.close()
  }

  // --- the Last Output pre: same cap, same structural floor ------------------
  if (height <= 900) {
    const { context, page } = await schedulePage(browser, [SUCCEEDED, FAILED], { width, height })
    const before = await measure(page, OUTPUT_SEL)
    const cap = capFor(height)
    check(
      before && before.clientHeight >= cap - TOLERANCE,
      `${width}x${height}: the Last Output pre renders at the cap (clientHeight ${before?.clientHeight} >= ${cap - TOLERANCE})`,
    )
    await hostile(page)
    const after = await measure(page, OUTPUT_SEL)
    check(
      after && after.clientHeight >= cap - TOLERANCE,
      `${width}x${height}: the Last Output pre with shrink-0 forced off still holds the cap (clientHeight ${after?.clientHeight} >= ${cap - TOLERANCE})`,
    )
    await context.close()
  }
}

await browser.close()
dist.srv.close()

if (failures.length) {
  console.error(`\n${failures.length} layout check(s) failed`)
  process.exit(1)
}
console.log('\nAll layout checks passed')
