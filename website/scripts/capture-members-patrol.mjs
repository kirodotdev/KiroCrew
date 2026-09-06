/**
 * Screenshot + recording harness for the Crew Members auto-patrol (monitor
 * loop) status.
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi) with a
 * five-member roster and a stubbed `GET /api/autonudge` registry that covers
 * every verdict the drawer block renders:
 *
 *   radar   active loop, cycle 3/24, 20-minute interval, banner set   -> accent badge
 *   ledger  active loop, UNLIMITED cap (61 cycles so far), no banner   -> accent badge
 *   scout   loop stopped by its cycle cap                             -> warn badge
 *   scribe  loop stopped because a tool approval went unanswered      -> warn badge
 *   fixer   nothing armed                                             -> no badge
 *
 * Frames:
 *   01-active-dark      radar's drawer, dark: "Patrolling" block, badge, and the
 *                       patrol listed under Wake sources
 *   02-active-light     the same, light theme
 *   03-unlimited-dark   ledger's drawer: cycles read "61 · no cap"
 *   04-stopped-dark     scout's drawer: stopped at its cycle cap
 *   05-stalled-dark     scribe's drawer: stopped, approval went unanswered
 *   06-none-dark        fixer's drawer: "No patrol scheduled."
 *   09-loading-dark     fixer's drawer while the registry read is in flight
 *   10-error-dark       registry read failed: roster-level ErrorNotice + drawer block ErrorNotice
 *
 * Recordings (the two animated state changes):
 *   07-badge-arm-disarm.webm   fixer's avatar gains and loses the badge as the
 *                              registry flips and the page re-reads it
 *   08-block-crossfade.webm    fixer's open drawer block cross-fades none ->
 *                              active -> stopped
 *
 * The page re-reads the registry on every `autonudge_state` websocket frame
 * and on reconnect (useWebSocket). The recordings drive exactly that path: the
 * harness holds the page's `/api/ws` socket, mutates the stub's registry, and
 * pushes an `autonudge_state` frame the way the gateway does.
 *
 * Every frame asserts the block's `data-state` and the roster badges (two
 * active, two stopped) before capturing, so a frame can only be written from
 * the state its filename claims.
 *
 * Usage: node scripts/capture-members-patrol.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/members-patrol-status'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now() / 1000
const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew-autofix', workspace: 'autofix', memory_store: 'default', model: '',
  last_active_ts: NOW - 300, last_message: '', ...extra,
})
const MEMBERS = [
  member('radar', { running: true, last_active_ts: NOW - 120, last_message: 'Six new issues: four covered by open PRs.' }),
  member('ledger', { last_active_ts: NOW - 900, last_message: 'Work ledger reconciled, no drift.' }),
  member('scout', { last_active_ts: NOW - 4 * 3600, last_message: 'Queue scan finished, nothing new.' }),
  member('scribe', { last_active_ts: NOW - 26 * 3600, last_message: 'Release notes drafted.' }),
  member('fixer', { last_active_ts: NOW - 2 * 86400, last_message: 'Two PRs opened for the queue.' }),
]
const loopRecord = (slug, extra) => ({
  id: `loop-${slug}`, slot_key: `member-${slug}`, active: true, banner: '',
  idle_secs: 1200, max_cycles: 24, cycle_count: 3,
  last_fire_ts: NOW - 6 * 60, next_due_ts: NOW + 14 * 60,
  created_ts: NOW - 3600, max_runtime_secs: 0, gate: false, stopped_reason: '',
  ...extra,
})
const RADAR = loopRecord('radar', {
  message: 'Sweep the issue queue, claim anything auto-fixable, report only real signals.',
  banner: 'nightly triage patrol',
})
const LEDGER = loopRecord('ledger', {
  message: 'Reconcile the work ledger against open PRs every quarter hour.',
  idle_secs: 900, max_cycles: 0, cycle_count: 61, last_fire_ts: NOW - 4 * 60, next_due_ts: NOW + 11 * 60,
})
const SCOUT = loopRecord('scout', {
  message: 'Scan the queue every 15 minutes.', active: false,
  idle_secs: 900, cycle_count: 24, last_fire_ts: NOW - 4 * 3600, next_due_ts: 0, stopped_reason: 'cycle_cap',
})
const FIXER_ACTIVE = loopRecord('fixer', {
  message: 'Open a PR for every auto-fixable issue the triage lane queued.',
  banner: 'queue fixer', cycle_count: 1, last_fire_ts: NOW - 60, next_due_ts: NOW + 19 * 60,
})
const FIXER_STOPPED = { ...FIXER_ACTIVE, active: false, next_due_ts: 0, stopped_reason: 'runtime_budget' }
const SCRIBE = loopRecord('scribe', {
  message: 'Draft release notes for every merged PR.', active: false,
  idle_secs: 3600, cycle_count: 7, last_fire_ts: NOW - 26 * 3600, next_due_ts: 0, stopped_reason: 'approval_stalled',
})
const BASELINE = [RADAR, LEDGER, SCOUT, SCRIBE]

// Mutable registry: the recordings flip it between frames; `mode` lets the
// two evidence frames make the read hang or fail.
const registry = { loops: BASELINE, mode: 'ok' }
const SLOTS = MEMBERS.map((m) => ({
  key: m.slot_key, title: m.name, mode: 'member', running: m.running, pinned: true,
}))

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openMembers(theme, { record = false } = {}) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1,
    ...(record ? { recordVideo: { dir: OUT, size: { width: 1280, height: 800 } } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  // Registered AFTER stubDashboardApi's own swallow-all route so it wins: the
  // harness plays the gateway end of the socket.
  let wsServer = null
  await stubDashboardApi(page, {
    theme,
    slots: SLOTS,
    // The Crew Members surface is preview-gated (`utils/previewFlags.ts`).
    localStorageEntries: { 'mc-preview-crew': '1' },
    extra: async (path, route) => {
      if (path === '/api/members') {
        await json(route, { members: MEMBERS, default_agent: 'kirocrew' })
        return true
      }
      const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
      if (thread) {
        const slug = decodeURIComponent(thread[1])
        await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false })
        return true
      }
      const activity = path.match(/^\/api\/members\/([^/]+)\/activity$/)
      if (activity) {
        await json(route, { slug: activity[1], member: activity[1], capped: false, entries: [] })
        return true
      }
      if (path === '/api/autonudge') {
        if (registry.mode === 'hang') return true // never answered: the skeleton frame
        if (registry.mode === 'fail') {
          await json(route, { error: 'boom', code: 'boom' }, 500)
          return true
        }
        await json(route, { enabled: true, loops: registry.loops })
        return true
      }
      if (path === '/api/crons') {
        await json(route, { jobs: [] })
        return true
      }
      if (path === '/api/webhooks') {
        await json(route, { tokens: [] })
        return true
      }
      const slot = path.match(/^\/api\/chat\/slots\/(member-[^/]+)$/)
      if (slot) {
        await json(route, { key: slot[1], title: slot[1].slice('member-'.length), running: false, messages: [] })
        return true
      }
      // The SPA auto-creates a chat slot on first load; answer with a real
      // keyed slot so the recents provider does not crash on a keyless row.
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
        return true
      }
      return false
    },
  })
  await page.routeWebSocket(/\/api\/ws/, (ws) => { wsServer = ws })
  await page.goto(base + '/members')
  await page.getByText('radar', { exact: true }).first().waitFor({ timeout: 15000 })
  if (registry.mode === 'ok') await expectBadges(page, { active: 2, stopped: 2 })
  /** Push one `autonudge_state` frame for `loop` (gateway envelope). */
  const pushLoop = async (event, loop) => {
    if (!wsServer) throw new Error('the page never opened /api/ws')
    wsServer.send(JSON.stringify({ type: 'autonudge_state', data: { event, slot: loop.slot_key, loop } }))
  }
  return { page, context, pushLoop }
}

/** Exactly the badges the registry implies: accent per active loop, warn per
 *  stopped one, none for a member that never armed. */
async function expectBadges(page, want) {
  await page.waitForFunction(
    (w) => {
      const all = Array.from(document.querySelectorAll('[data-testid="member-patrol-dot"]'))
      const active = all.filter((b) => b.getAttribute('data-state') === 'active').length
      const stopped = all.filter((b) => b.getAttribute('data-state') === 'stopped').length
      return active === w.active && stopped === w.stopped
    },
    want,
    { timeout: 15000 },
  )
}

async function openDrawerFor(page, name, expectedState) {
  await page.getByText(name, { exact: true }).first().click()
  const drawer = page.locator('[data-testid="member-drawer"]')
  if (!(await drawer.isVisible().catch(() => false))) {
    await page.locator('[data-testid="member-drawer-toggle"]').click()
  }
  await drawer.waitFor({ state: 'visible', timeout: 15000 })
  await expectState(page, expectedState)
  // Let the block's cross-fade settle before the frame.
  await page.waitForTimeout(300)
}

async function expectState(page, expectedState) {
  const block = page.locator('[data-testid="member-patrol"]')
  await block.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForFunction(
    (want) => document.querySelector('[data-testid="member-patrol"]')?.getAttribute('data-state') === want,
    expectedState,
    { timeout: 15000 },
  )
  if (await page.getByText('Something went wrong').isVisible().catch(() => false)) {
    throw new Error('ErrorBoundary visible — frame would show a crash, not the feature')
  }
}

// ── Still frames ─────────────────────────────────────────────────────────────
const { page: dark, context: darkCtx } = await openMembers('dark')
await openDrawerFor(dark, 'radar', 'active')
await dark.locator('[data-testid="member-wake-patrol"]').waitFor({ timeout: 15000 })
await dark.screenshot({ path: `${OUT}/01-active-dark.png` })
console.log('01-active-dark: radar patrolling 3/24, badge on the avatar, patrol listed under Wake sources')

const { page: light, context: lightCtx } = await openMembers('light')
await openDrawerFor(light, 'radar', 'active')
await light.screenshot({ path: `${OUT}/02-active-light.png` })
console.log('02-active-light: same state, light theme')
await lightCtx.close()

await openDrawerFor(dark, 'ledger', 'active')
const cycles = await dark.locator('[data-testid="member-patrol-cycles"]').textContent()
if (!/61/.test(cycles || '') || /\{\{/.test(cycles || '')) throw new Error(`unlimited-cap cycles read "${cycles}"`)
await dark.screenshot({ path: `${OUT}/03-unlimited-dark.png` })
console.log(`03-unlimited-dark: ledger cycles read "${cycles}"`)

await openDrawerFor(dark, 'scout', 'stopped')
await dark.screenshot({ path: `${OUT}/04-stopped-dark.png` })
console.log('04-stopped-dark: scout stopped at its cycle cap')

await openDrawerFor(dark, 'scribe', 'stopped')
await dark.screenshot({ path: `${OUT}/05-stalled-dark.png` })
console.log('05-stalled-dark: scribe stopped, approval went unanswered')

await openDrawerFor(dark, 'fixer', 'none')
await dark.screenshot({ path: `${OUT}/06-none-dark.png` })
console.log('06-none-dark: fixer has no patrol scheduled')
await darkCtx.close()

// 09/10: the two non-verdict states of the block — read in flight, read failed.
{
  registry.mode = 'hang'
  const { page, context } = await openMembers('dark')
  await page.getByText('fixer', { exact: true }).first().click()
  await page.locator('[data-testid="member-drawer"]').waitFor({ state: 'visible', timeout: 15000 })
  await page.locator('[data-testid="member-patrol-loading"]').waitFor({ state: 'visible', timeout: 15000 })
  // DetailPanel's width spring is still running when the skeleton first
  // shows; let it settle so the frame is the resting layout, not mid-tween.
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/09-loading-dark.png` })
  console.log('09-loading-dark: registry read in flight, skeleton')
  await context.close()
  registry.mode = 'fail'
  const { page: p2, context: c2 } = await openMembers('dark')
  await p2.getByText('fixer', { exact: true }).first().click()
  await p2.locator('[data-testid="member-drawer"]').waitFor({ state: 'visible', timeout: 15000 })
  await p2.locator('[data-testid="member-patrol-error"]').waitFor({ state: 'visible', timeout: 30000 })
  await p2.locator('[data-testid="member-roster-patrol-error"]').waitFor({ state: 'visible', timeout: 15000 })
  await p2.waitForTimeout(600)
  await p2.screenshot({ path: `${OUT}/10-error-dark.png` })
  console.log('10-error-dark: registry read failed, ErrorNotice')
  await c2.close()
  registry.mode = 'ok'
}

// ── Recordings ───────────────────────────────────────────────────────────────
// 07: the roster badge arming and disarming on fixer. Drawer closed so the
// avatar is the only thing that changes.
{
  const { page, context, pushLoop } = await openMembers('dark', { record: true })
  await page.waitForTimeout(800)
  registry.loops = [...BASELINE, FIXER_ACTIVE]
  await pushLoop('added', FIXER_ACTIVE)
  await expectBadges(page, { active: 3, stopped: 2 })
  await page.waitForTimeout(1200)
  registry.loops = [...BASELINE, FIXER_STOPPED]
  await pushLoop('expired', FIXER_STOPPED)
  await expectBadges(page, { active: 2, stopped: 3 })
  await page.waitForTimeout(1200)
  registry.loops = BASELINE
  await pushLoop('removed', FIXER_STOPPED)
  await expectBadges(page, { active: 2, stopped: 2 })
  await page.waitForTimeout(800)
  const video = page.video()
  await context.close()
  if (video) { await video.saveAs(`${OUT}/07-badge-arm-disarm.webm`); await video.delete() }
  console.log('07-badge-arm-disarm.webm: fixer badge in (accent), stopped (warn), then out')
}

// 08: the drawer block cross-fading none -> active -> stopped on fixer.
{
  const { page, context, pushLoop } = await openMembers('dark', { record: true })
  await openDrawerFor(page, 'fixer', 'none')
  await page.waitForTimeout(800)
  registry.loops = [...BASELINE, FIXER_ACTIVE]
  await pushLoop('added', FIXER_ACTIVE)
  await expectState(page, 'active')
  await page.waitForTimeout(1200)
  registry.loops = [...BASELINE, FIXER_STOPPED]
  await pushLoop('expired', FIXER_STOPPED)
  await expectState(page, 'stopped')
  await page.waitForTimeout(1000)
  const video = page.video()
  await context.close()
  if (video) { await video.saveAs(`${OUT}/08-block-crossfade.webm`); await video.delete() }
  console.log('08-block-crossfade.webm: fixer block none -> active -> stopped')
}
// Leave the registry as the stills saw it.
registry.loops = BASELINE

await browser.close()
srv.close()
