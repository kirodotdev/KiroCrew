/**
 * Recording harness for the Crewmates roster (top-left vertical stack).
 *
 * Runs the REAL built SPA (website/dist) behind serveDist + stubDashboardApi
 * (no gateway/auth/kiro-cli, so it works on a host whose kernel refuses the
 * sandbox). The roster is a fixed two-element stack on the top-left (wide only),
 * with no modes and no collapse:
 *   - the identity PILL (avatar + name), click to edit;
 *   - directly below it, the always-on vertical SWITCHER — with >1 crewmate it
 *     is the vertical avatar strip (every crewmate, active ringed) + the add (+)
 *     at the bottom; with ONE crewmate it is just the add (+) button.
 * There is no chevron and no open/close card. A 1-crewmate context asserts the
 * gate: the solo add button, no strip.
 *
 * Usage: node scripts/capture-floating-roster.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'floating-roster')
mkdirSync(OUT, { recursive: true })

const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: `member-${name}`,
  model: '', last_active_ts: Math.floor(Date.now() / 1000) - 300,
  last_message: 'On it.', ...extra,
})
const MANY = [member('radar'), member('scribe'), member('courier')]
const ONE = [member('radar')]
const TEAMS = [{ id: 'team-ops', name: 'Operations', members: ['scribe', 'courier'] }]

let failed = false
const check = (name, ok, detail = '') => { console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`); if (!ok) failed = true; return ok }

const extraFor = (members, teams = []) => async (path, route) => {
  if (path === '/api/members') { await json(route, { members, default_agent: 'kirocrew' }); return true }
  if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
  if (path === '/api/cron-folders') { await json(route, []); return true }
  if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
  const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) { const slug = decodeURIComponent(thread[1]); await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false }); return true }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: '', member: '', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) { await json(route, { slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
  if (path === '/api/teams') { await json(route, { teams }); return true }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function newCtx(members, record, teams = []) {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 940 }, deviceScaleFactor: 1, colorScheme: 'dark',
    ...(record ? { recordVideo: { dir: OUT, size: { width: 1500, height: 940 } } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark', extra: extraFor(members, teams),
    localStorageEntries: { 'mc-lang': 'en', 'mc-crewmates-onboarded': '1' },
  })
  await page.goto(`${base}/members?member=radar`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-thread-header').waitFor({ state: 'visible', timeout: 30000 })
  return { context, page }
}

// ── >1 crewmate: pill + always-on vertical avatar strip + bottom add (+) ─────
{
  const { context, page } = await newCtx(MANY, true, TEAMS)
  const pill = page.getByTestId('member-identity-pill')
  const strip = page.getByTestId('member-roster-strip')
  await strip.waitFor({ state: 'visible', timeout: 20000 })
  check('identity pill shows', await pill.count() === 1)
  check('vertical strip shows with >1 mate', await strip.count() === 1)
  check('strip is vertical', (await strip.getAttribute('data-orientation')) === 'vertical')
  check('every crewmate avatar is present (active included)', await page.locator('[data-testid^="member-strip-avatar-"]').count() === MANY.length)
  check('the active crewmate is shown in the strip', await page.getByTestId('member-strip-avatar-radar').count() === 1)
  check('add (+) present in the strip', await page.getByTestId('member-add-strip').count() === 1)
  check('no chevron toggle', await page.getByTestId('member-roster-toggle').count() === 0)
  check('no collapsible card', await page.getByTestId('member-roster-card').count() === 0)
  // Teams appear as monogram markers at the top of the strip; clicking opens the team view.
  check('team marker shows in the strip', await page.getByTestId('member-strip-team-team-ops').count() === 1)
  check('team monogram is the name initials', (await page.getByTestId('member-strip-team-team-ops').innerText()).trim().toUpperCase() === 'OP')
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, 'stack-many.png') })
  // Hover the first OTHER avatar → the name+detail flyout appears.
  const firstAvatar = page.locator('[data-testid^="member-strip-avatar-"]').first()
  const flyout = page.locator('[data-testid^="member-strip-flyout-"]').first()
  check('a hover flyout exists per avatar', await page.locator('[data-testid^="member-strip-flyout-"]').count() === MANY.length)
  check('flyout shows the crewmate name', (await flyout.innerText()).trim().length > 0)
  await firstAvatar.hover()
  await page.waitForTimeout(400)
  // group-hover reveals it (opacity 0 → 1); assert it is now visibly opaque.
  const op = await flyout.evaluate((el) => getComputedStyle(el).opacity)
  check('flyout becomes visible on hover', Number(op) > 0.5, `(opacity=${op})`)
  await page.screenshot({ path: join(OUT, 'stack-many-hover.png') })
  await page.waitForTimeout(400)
  await context.close() // flushes the video
}

// ── 1 crewmate: pill + a solo add (+) button only, no strip ──────────────────
{
  const { context, page } = await newCtx(ONE, false)
  const solo = page.getByTestId('member-roster-solo-add')
  await solo.waitFor({ state: 'visible', timeout: 20000 })
  check('solo add (+) shows with a single crewmate', await solo.count() === 1)
  check('add (+) button present', await page.getByTestId('member-add-strip').count() === 1)
  check('no vertical strip with one crewmate', await page.getByTestId('member-roster-strip').count() === 0)
  check('no chevron with one crewmate', await page.getByTestId('member-roster-toggle').count() === 0)
  await page.screenshot({ path: join(OUT, 'stack-solo.png') })
  await context.close()
}

await browser.close()
srv.close()

// Rename the recorded video to a stable name.
try {
  const vids = readdirSync(OUT).filter((f) => f.endsWith('.webm'))
  if (vids.length) { renameSync(join(OUT, vids[0]), join(OUT, 'floating-roster.webm')); console.log('video:', join(OUT, 'floating-roster.webm')) }
} catch (e) { console.log('video rename skipped:', e.message) }

console.log(failed ? 'RESULT: MISMATCH' : 'RESULT: OK')
process.exit(failed ? 1 : 0)
