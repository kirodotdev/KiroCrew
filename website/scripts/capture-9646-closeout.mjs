/**
 * Screenshots for the PR #9646 close-out batch: two dashboard surfaces
 * changed by the PR, each shot from its isolated capture entry under
 * website/capture/ with every /api/** call answered from fixtures here
 * (gateway-free). Every frame ASSERTS the text it documents before writing,
 * so a frame cannot silently show the wrong state.
 *
 *   schedule-retry-note-{dark,light}   SchedulePage Last run column: "Retried
 *                                      3 times" / "Retried 1 time" / no note
 *   setup-check-probe-error-dark       KiroPrerequisiteGate "Setup check
 *                                      unavailable" with probe_error/probe_status
 *
 * Usage:
 *   npx vite --port 5199 --strictPort   # in another shell
 *   node scripts/capture-9646-closeout.mjs http://127.0.0.1:5199 ../temp-screenshots/9646-closeout-dashboard
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:5199'
const OUT = process.argv[3] || '../temp-screenshots/9646-closeout-dashboard'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// Shapes follow website/src/test/SchedulePage.retryNote.test.tsx. The note
// renders from `last_retry_count` ONLY when `last_retry_run_ts` equals the
// row's `last_run_ts` (the shipped SchedulePage gate), so every fixture carries
// the stamp bound the way `CronService._execute` writes it. Plural, singular
// and zero in one table; job-4 has a count from an earlier run and must NOT
// render a note.
const JOBS = [
  {
    id: 'job-1', name: 'Flaky feed poller', schedule: 'every 300s', enabled: true, agent: 'kirocrew',
    message: 'Poll the release feed and flag regressions.',
    last_status: 'ok', last_run_ts: now - 420, last_retry_count: 3, last_retry_run_ts: now - 420,
    next_run_ts: now + 180,
  },
  {
    id: 'job-2', name: 'Nightly report', schedule: 'every 1d', timezone: 'America/Los_Angeles', enabled: true, agent: 'kirocrew',
    message: "Summarise yesterday's CI failures and post the digest to #build-health.",
    last_status: 'ok', last_run_ts: now - 3600, last_retry_count: 1, last_retry_run_ts: now - 3600,
    next_run_ts: now + 7200,
  },
  {
    id: 'job-3', name: 'Weekly compliance digest', schedule: '0 9 * * 1', cron_expr: '0 9 * * 1', enabled: true, agent: 'kirocrew',
    message: 'Compile the weekly compliance digest.',
    last_status: 'ok', last_run_ts: now - 86400, last_retry_count: 0, last_retry_run_ts: now - 86400,
    next_run_ts: now + 6 * 86400,
  },
  {
    id: 'job-4', name: 'Cancelled sync', schedule: 'every 900s', enabled: true, agent: 'kirocrew',
    message: 'Mirror the shared drive index.',
    // The last COMPLETED run retried twice; the most recent run was cancelled and
    // advanced `last_run_ts` on its own. The stamps disagree, so no note.
    last_status: 'error', last_error: 'Cancelled', last_run_ts: now - 600, last_retry_count: 2,
    last_retry_run_ts: now - 1500,
    next_run_ts: now + 300,
  },
]

// Fixture from website/src/test/KiroPrerequisiteGate.test.tsx `status()`, with
// the probe diagnostic the PR surfaces. `installed: true` keeps the earlier
// first-run branches from claiming the render before the probe_error branch.
const PREREQ = {
  platform: 'Linux',
  installed: true,
  authenticated: false,
  ready: false,
  initial_setup_complete: false,
  repair_required: false,
  docs_url: 'https://kiro.dev/cli/',
  login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  setup_allowed: true,
  sandbox_unavailable: false,
  sandbox_failure_kind: '',
  sandbox_detail: '',
  sandbox_remedy: '',
  missing_agent_specs: [],
  agent_spec_repair_error: '',
  probe_error: 'error: failed to load ~/.kiro/settings.json',
  probe_status: 1,
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

/** Answer every real API call the mounted pages make. Predicate on the
 *  pathname — a glob would also swallow vite-served source modules. */
async function stubApi(page) {
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/crons') return json(route, { jobs: JOBS })
    if (path === '/api/cron-folders') return json(route, [])
    if (path === '/api/agents') return json(route, { agents: [{ name: 'kirocrew', description: '' }], default_agent: 'kirocrew' })
    if (path === '/api/config/default-agent') return json(route, { default_agent: 'kirocrew' })
    if (path === '/api/models') return json(route, [])
    if (path === '/api/kiro-prerequisite') return json(route, PREREQ)
    if (path === '/api/crons/history') return json(route, { runs: [] })
    return json(route, {})
  })
}

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(viewport = { width: 1000, height: 700 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.error('pageerror:', e.message))
  await stubApi(page)
  return page
}

// 1. Schedule page retry note, dark + light. The jobs table is wider than
// 1000px (Status / Last run / Next run sit between Message and the pinned
// Actions column and scroll horizontally), so this scene uses the width the
// page needs to show the Last run column unscrolled — and asserts the note is
// actually inside the viewport, not merely in the DOM.
const SCHEDULE_WIDTH = 1500
for (const theme of ['dark', 'light']) {
  const page = await newPage({ width: SCHEDULE_WIDTH, height: 560 })
  await page.goto(`${BASE}/capture/schedule-retry-note.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Flaky feed poller').first().waitFor()
  const plural = page.getByText('Retried 3 times', { exact: true })
  const singular = page.getByText('Retried 1 time', { exact: true })
  const any = await page.getByText(/^Retried /).count()
  const box = await plural.boundingBox()
  const visible = !!box && box.x >= 0 && box.x + box.width <= SCHEDULE_WIDTH
  // The cell is `truncate` inside a fixed `w-[82px]` Last Run column, so at
  // 11px "Retried 3 times" does not fit and clips to "Retried 3 ti…" while the
  // DOM text stays intact. Measured and REPORTED (not asserted): the column
  // width is SchedulePage's, not this harness's, and the frame must show the
  // shipped rendering — a clipped note is the honest evidence.
  const untruncated = await plural.evaluate(el => el.scrollWidth <= el.clientWidth)
  if (!untruncated) console.warn(`schedule-retry-note-${theme}: WARN note is clipped by the 82px Last Run column (shipped rendering)`)
  const ok = check(`schedule-retry-note-${theme}`, await plural.count() === 1 && await singular.count() === 1 && any === 2 && visible,
    `plural=${await plural.count()} singular=${await singular.count()} total=${any} inViewport=${visible} untruncated=${untruncated}`)
  if (ok) await page.screenshot({ path: `${OUT}/schedule-retry-note-${theme}.png` })
  await page.close()
}

// 2. Setup gate probe diagnostic.
{
  const page = await newPage()
  await page.goto(`${BASE}/capture/setup-check-probe-error.html?theme=dark`)
  await page.waitForSelector('[data-testid="kiro-gate-status-error"]')
  const text = (await page.locator('[data-capture-root]').innerText()).replace(/\s+/g, ' ')
  const heading = /Setup check unavailable/i.test(text) && text.includes('We could not check Kiro CLI.')
  const diag = text.includes(PREREQ.probe_error) && text.includes(`(exit ${PREREQ.probe_status})`)
  const blocked = (await page.locator('[data-capture-dashboard]').count()) === 0
  const ok = check('setup-check-probe-error-dark', heading && diag && blocked,
    `heading=${heading} diag=${diag} dashboardHidden=${blocked}`)
  if (ok) await page.screenshot({ path: `${OUT}/setup-check-probe-error-dark.png` })
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
