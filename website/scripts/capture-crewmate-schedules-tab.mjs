/**
 * Screenshot harness for CREW-18721: the Schedules tab in the crewmate side panel.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every `/api/**` call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli — which is also what makes it work on a host whose
 * kernel refuses `unshare(CLONE_NEWUSER)`, where a real instance renders the
 * "Sandbox unavailable" prerequisite gate instead of the dashboard.
 *
 * The cron fixture is the shape the feature exists to tell apart:
 *   radar   — "Triage new issues" active + "Sweep stale PRs" paused  => chip reads 1/2
 *   scribe  — "Weekly notes" active                                  => chip reads 1/1
 *   courier — nothing at all                                         => no chip badge
 *   nobody  — "Nightly backup check" + "Morning digest", no member_id => on /schedule
 *             only, never on a crewmate's tab
 *
 * Every frame asserts its own state before it is written, so a frame cannot document
 * the wrong thing:
 *   01-radar-schedules    radar's two schedules, chip 1/2, no ownerless job listed
 *   02-courier-empty      a crewmate nothing wakes: quiet pane, no badge on the chip
 *   03-scribe-schedules   a second crewmate: its own single schedule, chip 1/1
 *   04-schedule-page      /schedule still lists all five jobs, unchanged
 *
 * Usage: node scripts/capture-crewmate-schedules-tab.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'crew18721-shots')
mkdirSync(OUT, { recursive: true })

const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: `member-${name}`,
  model: '', ...extra,
})
const MEMBERS = [
  member('radar', { last_active_ts: Math.floor(Date.now() / 1000) - 240, last_message: 'Six new issues, four already covered.' }),
  member('scribe', { last_active_ts: Math.floor(Date.now() / 1000) - 5400, last_message: 'Weekly summary filed.' }),
  // Nothing wakes this one. Present so a frame can show the empty state a crewmate
  // with no schedules gets, which is most of the roster on a fresh install.
  member('courier', { last_active_ts: Math.floor(Date.now() / 1000) - 900, last_message: 'Delivered.' }),
]

const now = Math.floor(Date.now() / 1000)
const JOBS = [
  { id: 'cron-triage', name: 'Triage new issues', message: 'Triage anything new in the issue queue and label it.', enabled: true, schedule: 'At 9:00 AM UTC', agent: 'kirocrew', member_id: 'radar', last_run_ts: now - 10800, next_run_ts: now + 75600, last_status: 'ok' },
  { id: 'cron-sweep', name: 'Sweep stale PRs', message: 'List pull requests with no activity for seven days.', enabled: false, schedule: 'every 6h', agent: 'kirocrew', member_id: 'radar', last_run_ts: now - 540000, last_status: 'ok' },
  { id: 'cron-notes', name: 'Weekly notes', message: 'Write the weekly summary into the crew notes.', enabled: true, schedule: 'At 5:00 PM UTC, only on Friday', agent: 'kirocrew', member_id: 'scribe', last_run_ts: now - 172800, next_run_ts: now + 259200, last_status: 'ok' },
  // No `member_id`: these belong to nobody, so no crewmate's tab may claim them. They
  // are here to prove that, and to keep /schedule's own count at five.
  { id: 'cron-backup', name: 'Nightly backup check', message: 'Confirm last night backup completed.', enabled: true, schedule: 'every 24h', agent: 'kirocrew', member_id: '', next_run_ts: now + 43200 },
  { id: 'cron-digest', name: 'Morning digest', message: 'Post the overnight digest.', enabled: true, schedule: 'At 7:30 AM UTC', agent: 'kirocrew', member_id: '', next_run_ts: now + 19800 },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

/** Answers the routes this feature reads; everything else falls to the shared stub. */
const extra = async (path, route) => {
  if (path === '/api/members') { await json(route, { members: MEMBERS, default_agent: 'kirocrew' }); return true }
  if (path === '/api/crons') { await json(route, { jobs: JOBS }); return true }
  // A bare ARRAY: `api.cronFolders()` returns the parsed body straight to
  // `groupJobsByFolder`, which maps over it. `{folders: []}` crashes the Schedule
  // page into its route error boundary — and the harness would still write a PNG of
  // that boundary, so the wrong shape here fails toward a false pass.
  if (path === '/api/cron-folders') { await json(route, []); return true }
  // Answered because other surfaces on the page read it. The Schedules tab does NOT:
  // it lists only what is attributed to the open crewmate, so which crew is the
  // default changes nothing there.
  if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
  const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) {
    const slug = decodeURIComponent(thread[1])
    await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false })
    return true
  }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: '', member: '', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) { await json(route, { slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  return false
}

async function openCrewmate(theme, name) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 1, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme, extra, localStorageEntries: { 'mc-lang': 'en' } })
  await page.goto(`${base}/members?member=${name}`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-roster').waitFor({ state: 'visible', timeout: 30000 })
  // The row click, not just the URL: the panel's strip is keyed on the slot the
  // thread POST confirms, so a frame taken without the real open would show the
  // slot-free bucket instead.
  await page.locator('#main-content li button', { hasText: name }).first().click()
  const chip = page.getByTestId('side-panel-leading-tab-crew-schedules')
  await chip.waitFor({ state: 'visible', timeout: 20000 })
  return { context, page, chip }
}

/** Frame 01: radar's own two schedules, and neither of the ownerless jobs. */
{
  const { context, page, chip } = await openCrewmate('light', 'radar')
  const badge = page.getByTestId('member-schedules-count')
  await badge.waitFor({ state: 'visible', timeout: 20000 })
  check('radar chip counts live/total', (await badge.innerText()).trim() === '1/2', await badge.innerText())
  await chip.click()
  const body = page.getByTestId('member-schedules')
  await body.waitFor({ state: 'visible', timeout: 20000 })
  await page.getByTestId('crew-wake-section').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  check('radar pane lists its two schedules', await body.getByTestId('wake-row').count() === 2)
  const listed = await body.getByTestId('wake-row').allInnerTexts()
  check('radar pane lists no ownerless job', !listed.join(' ').includes('Nightly backup'))
  const panel = page.getByTestId('side-panel-leading-body')
  await panel.screenshot({ path: join(OUT, '01-radar-schedules.png') })
  console.log('wrote', join(OUT, '01-radar-schedules.png'))
  await context.close()
}

/** Frame 02: a crewmate nothing wakes — the state most of a fresh roster is in. */
{
  const { context, page, chip } = await openCrewmate('light', 'courier')
  check('a crewmate with no schedules carries no badge', await page.getByTestId('member-schedules-count').count() === 0)
  await chip.click()
  const body = page.getByTestId('member-schedules')
  await body.waitFor({ state: 'visible', timeout: 20000 })
  await page.getByTestId('crew-wake-section').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  check('and its pane lists nothing', await body.getByTestId('wake-row').count() === 0)
  const panel = page.getByTestId('side-panel-leading-body')
  await panel.screenshot({ path: join(OUT, '02-courier-empty.png') })
  console.log('wrote', join(OUT, '02-courier-empty.png'))
  await context.close()
}

/**
 * Frames 05-07: the chip WITH its count in the strip, then the two writes the tab
 * offers — create a schedule bound to the open crewmate, and run one now.
 *
 * The backend here is the stub, so what these prove is the UI's half: which endpoint
 * the control calls and with what body. That a real gateway actually FIRES the job is
 * proven separately, against a live isolated instance, in the PR body.
 */
{
  const { context, page, chip } = await openCrewmate('light', 'radar')
  await chip.click()
  const body = page.getByTestId('member-schedules')
  await body.waitFor({ state: 'visible', timeout: 20000 })
  await page.getByTestId('crew-wake-section').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)

  // The strip and the body together: the badge lives on the chip, so a frame of
  // the body alone cannot show the count at all. Clipped to the union of the two
  // boxes rather than screenshotting a shared ancestor, which would drag in the
  // roster and the thread column.
  const strip = page.getByTestId('side-panel-leading-tabs')
  const bodyEl = page.getByTestId('side-panel-leading-body')
  const a = await strip.boundingBox()
  const b = await bodyEl.boundingBox()
  const clip = {
    x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
    width: Math.max(a.x + a.width, b.x + b.width) - Math.min(a.x, b.x),
    height: Math.max(a.y + a.height, b.y + b.height) - Math.min(a.y, b.y),
  }
  check('chip shows the count in the strip', (await strip.innerText()).includes('1/2'), await strip.innerText())
  await page.screenshot({ path: join(OUT, '05-strip-with-count.png'), clip })
  console.log('wrote', join(OUT, '05-strip-with-count.png'))

  // Create, from inside the tab. The assertion that matters is the POST body's
  // `member_id`: the form is pinned to the crewmate whose panel it opened in, so a
  // schedule made here cannot land on somebody else.
  let createdBody = null
  await page.route('**/api/crons', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    createdBody = JSON.parse(route.request().postData() || '{}')
    await json(route, { ok: true, id: 'cron-new' })
  })
  await body.getByTestId('crew-wake-add').click()
  await page.locator('#jobform-name').waitFor({ state: 'visible', timeout: 10000 })
  await page.locator('#jobform-name').fill('Check the release board')
  await page.locator('#jobform-message').fill('Read the release board and flag anything stuck.')
  await page.screenshot({ path: join(OUT, '06-create-in-tab.png'), clip })
  console.log('wrote', join(OUT, '06-create-in-tab.png'))
  await body.getByTestId('crew-wake-create-submit').click()
  await page.waitForTimeout(1200)
  check('create posts against the open crewmate', createdBody?.member_id === 'radar', JSON.stringify(createdBody?.member_id))
  check('create carries the typed name', createdBody?.name === 'Check the release board', String(createdBody?.name))

  // Run now, from the same rows. The refreshed list is what carries the result, so
  // the reply to the refetch is a job that has just run.
  let ranPath = ''
  await page.route('**/api/crons/**', async route => {
    const u = new URL(route.request().url())
    if (route.request().method() === 'POST' && u.pathname.endsWith('/run')) {
      ranPath = u.pathname
      return json(route, { ok: true, name: 'Triage new issues' })
    }
    return route.fallback()
  })
  await page.unroute('**/api/crons')
  const justRan = Math.floor(Date.now() / 1000) - 2
  await page.route('**/api/crons', async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    await json(route, {
      jobs: JOBS.map(j => (j.id === 'cron-triage'
        ? { ...j, last_run_ts: justRan, last_status: 'ok', last_result: 'Labelled 3 new issues.', last_result_ts: justRan }
        : j)),
    })
  })
  const triage = body.getByTestId('wake-row').filter({ hasText: 'Triage new issues' }).first()
  await triage.getByRole('button', { name: /Run .*now/i }).click()
  await page.waitForTimeout(1500)
  check('run posts to the job run route', ranPath.endsWith('/run'), ranPath)
  check('the row reports the fresh run', (await triage.innerText()).includes('ago'), (await triage.innerText()).replace(/\s+/g, ' '))
  await page.screenshot({ path: join(OUT, '07-run-now-result.png'), clip })
  console.log('wrote', join(OUT, '07-run-now-result.png'))
  await context.close()
}

/** Frame 03: a SECOND crewmate, so the frames prove per-mate grouping and not one list. */
{
  const { context, page, chip } = await openCrewmate('light', 'scribe')
  const badge = page.getByTestId('member-schedules-count')
  await badge.waitFor({ state: 'visible', timeout: 20000 })
  check('scribe chip counts its own schedule only', (await badge.innerText()).trim() === '1/1', await badge.innerText())
  await chip.click()
  const body = page.getByTestId('member-schedules')
  await body.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  check('scribe pane lists one schedule', await body.getByTestId('wake-row').count() === 1)
  check('and it is scribe\'s', (await body.getByTestId('wake-row').innerText()).includes('Weekly notes'))
  await page.getByTestId('side-panel-leading-body').screenshot({ path: join(OUT, '03-scribe-schedules.png') })
  console.log('wrote', join(OUT, '03-scribe-schedules.png'))
  await context.close()
}

/** Frame 04: /schedule is untouched and still the cross-mate view. */
{
  const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 1, colorScheme: 'light' })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme: 'light', extra, localStorageEntries: { 'mc-lang': 'en' } })
  await page.goto(`${base}/schedule`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Triage new issues').first().waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(600)
  const text = await page.locator('#main-content').innerText()
  for (const n of ['Triage new issues', 'Sweep stale PRs', 'Weekly notes', 'Nightly backup check', 'Morning digest']) {
    check(`/schedule still lists ${n}`, text.includes(n))
  }
  await page.locator('#main-content').screenshot({ path: join(OUT, '04-schedule-page.png') })
  console.log('wrote', join(OUT, '04-schedule-page.png'))
  await context.close()
}

await browser.close()
srv.close()
console.log(failed ? 'FAILED' : 'ALL CHECKS OK')
process.exit(failed ? 1 : 0)
