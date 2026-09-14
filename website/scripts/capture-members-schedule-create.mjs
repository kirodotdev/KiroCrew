/**
 * Screenshot harness for the Crew Members drawer's "new schedule" affordance.
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi) with a
 * three-member roster and one schedule already bound to the open member, so the
 * frames show the `+` beside a POPULATED wake list rather than an empty one.
 *
 * Frames:
 *   01-wake-create-dark    the Wake sources block: one schedule listed, the new
 *                          `+` control beside the existing Schedule jump
 *   02-dialog-dark         the dialog it opens — titled for the member, crew
 *                          rendered as a FIXED value (no picker)
 *   03-wake-create-light   frame 01 again, light theme
 *
 * Both frames assert before capturing: 01 that the create control exists next
 * to the jump, 02 that the locked-crew value reads the open member's name — so
 * a frame cannot be written from a state its filename does not claim.
 *
 * Usage: node scripts/capture-members-schedule-create.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/members-schedule-create'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now() / 1000
const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew-autofix', workspace: 'autofix', memory_store: 'default', model: '',
  last_active_ts: NOW - 300, last_message: '', ...extra,
})
const MEMBERS = [
  member('oncall', { last_active_ts: NOW - 240, last_message: 'Queue swept, two PRs opened.' }),
  member('ledger', { last_active_ts: NOW - 900, last_message: 'Work ledger reconciled, no drift.' }),
  member('scribe', { last_active_ts: NOW - 26 * 3600, last_message: 'Release notes drafted.' }),
]
// One member-bound schedule and one bound elsewhere: the block filters through
// the shared wakesCrew predicate, so the second must NOT appear.
const JOBS = [
  {
    id: 'j1', name: 'nightly-triage', message: 'Sweep the issue queue.', enabled: true,
    schedule: '0 2 * * *', last_status: 'ok', agent: 'kirocrew-autofix', member_id: 'oncall',
    last_run_ts: NOW - 8 * 3600, next_run_ts: NOW + 16 * 3600,
  },
  {
    id: 'j2', name: 'ledger-reconcile', message: 'Reconcile the ledger.', enabled: true,
    schedule: '@hourly', last_status: 'ok', agent: 'kirocrew-autofix', member_id: 'ledger',
  },
]
const SLOTS = MEMBERS.map((m) => ({
  key: m.slot_key, title: m.name, mode: 'member', running: m.running, pinned: true,
}))

/** POST /api/crons routes held open while `hangCreate` is on. */
const pendingCreates = []

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openMembers(theme, { jobs = JOBS, hangCreate = false } = {}) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await page.exposeFunction('__failPendingCreate', async (reason) => {
    const route = pendingCreates.shift()
    if (!route) throw new Error('no create in flight to fail')
    // 400: the server DECIDING, which is the only case that proves nothing was
    // created. A 5xx would be an unknown — frame 08 covers that.
    await json(route, { error: reason }, 400)
  })
  await page.exposeFunction('__dropPendingCreate', async () => {
    const route = pendingCreates.shift()
    if (!route) throw new Error('no create in flight to drop')
    await route.abort('connectionfailed')
  })
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
        await json(route, { enabled: true, loops: [] })
        return true
      }
      if (path === '/api/crons' && route.request().method() === 'POST') {
        if (!hangCreate) { await json(route, { ok: true }); return true }
        // Held open. `window.__failPendingCreate(reason)` resolves it as a
        // refusal, so the late-failure frame shows a real server answer rather
        // than a hand-drawn state.
        pendingCreates.push(route)
        return true
      }
      if (path === '/api/crons') {
        await json(route, { jobs })
        return true
      }
      if (path === '/api/webhooks') {
        await json(route, { tokens: [] })
        return true
      }
      // The dialog's JobForm reads the model list.
      if (path === '/api/models') {
        await json(route, { models: [] })
        return true
      }
      const slot = path.match(/^\/api\/chat\/slots\/(member-[^/]+)$/)
      if (slot) {
        await json(route, { key: slot[1], title: slot[1].slice('member-'.length), running: false, messages: [] })
        return true
      }
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'chat-1', name: 'chat-1', title: 'New Session…', messages: [], running: false })
        return true
      }
      return false
    },
  })
  await page.goto(base + '/members')
  await page.getByText('oncall', { exact: true }).first().waitFor({ timeout: 15000 })
  // The wake block lives on the Crew summary tab, which the drawer opens on.
  if (jobs.length) await page.getByTestId('member-wake-sources').waitFor({ timeout: 15000 })
  // The header control is withheld while the list is empty, so the empty case
  // waits for the affordance that IS offered there.
  else await page.getByTestId('member-wake-create-empty').waitFor({ timeout: 15000 })
  return { context, page }
}

async function shotWakeBlock(page, name) {
  const create = page.getByTestId('member-wake-create')
  await create.waitFor({ timeout: 10000 })
  const panel = page.getByTestId('member-side-panel')
  await panel.screenshot({ path: `${OUT}/${name}.png` })
}

// 01 + 02 — dark
{
  const { context, page } = await openMembers('dark')
  const list = page.getByTestId('member-wake-sources')
  const text = await list.innerText()
  if (!text.includes('nightly-triage')) throw new Error('member-bound schedule missing from the block')
  if (text.includes('ledger-reconcile')) throw new Error('another member\'s schedule leaked into the block')
  await shotWakeBlock(page, '01-wake-create-dark')

  await page.getByTestId('member-wake-create').click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor({ timeout: 10000 })
  const locked = page.getByTestId('jobform-locked-agent')
  await locked.waitFor({ timeout: 10000 })
  const lockedText = await locked.innerText()
  if (!lockedText.includes('oncall')) {
    throw new Error(`crew is not locked to the open member: ${JSON.stringify(lockedText)}`)
  }
  await dialog.screenshot({ path: `${OUT}/02-dialog-dark.png` })
  await context.close()
}

// 04 — the empty case carries the action in words (UX round 1)
{
  const { context, page } = await openMembers('dark', { jobs: [] })
  const empty = page.getByTestId('member-wake-create-empty')
  await empty.waitFor({ timeout: 10000 })
  if (!(await empty.innerText()).includes('New schedule')) {
    throw new Error('empty state does not name the action')
  }
  // The header control is withheld here, so the action is offered exactly once.
  if (await page.getByTestId('member-wake-create').count()) {
    throw new Error('the action is offered twice in the empty state')
  }
  const panel = page.getByTestId('member-side-panel')
  await panel.screenshot({ path: `${OUT}/04-wake-empty-dark.png` })
  await context.close()
}

// 05 / 06 / 07 — the three states the UX lane could not evaluate for want of a
// still: the in-flight footer, the discard confirm, and the late-failure notice.
{
  const { context, page } = await openMembers('dark', { hangCreate: true })
  const openDialog = async () => {
    await page.getByTestId('member-wake-create').click()
    await page.getByTestId('jobform-locked-agent').waitFor({ timeout: 10000 })
  }
  const dialog = () => page.getByRole('dialog').first()
  const fill = async (name) => {
    await dialog().getByLabel('Name').fill(name)
    await dialog().getByLabel('Message').fill('Sweep the queue and report only real signals.')
  }

  // 05 — mid-flight: the exit still offered, relabelled Close, beside the caveat
  // that says what closing does and does not do.
  await openDialog()
  await fill('nightly-sweep')
  await page.getByTestId('member-schedule-submit').click()
  const caveat = page.getByTestId('member-schedule-inflight')
  await caveat.waitFor({ timeout: 10000 })
  const dismissLabel = await page.getByTestId('member-schedule-dismiss').innerText()
  if (!/close/i.test(dismissLabel)) {
    throw new Error(`in-flight dismiss control should read Close, got ${JSON.stringify(dismissLabel)}`)
  }
  if (!/may still be created/i.test(await caveat.innerText())) {
    throw new Error('in-flight caveat does not state that the schedule may still be created')
  }
  await dialog().screenshot({ path: `${OUT}/05-dialog-inflight-dark.png` })

  // 06 — the discard confirm, reached by dismissing a TYPED draft. Escape rather
  // than the footer, since the gesture paths are the ones that used to destroy it.
  await page.getByTestId('member-schedule-dismiss').click()
  await openDialog()
  await dialog().getByLabel('Name').fill('half-typed')
  // The footer control rather than Escape: every dismissal path funnels through
  // one rule, and the vitest cases already pin the gesture paths. This frame's
  // job is to SHOW the confirm, so it takes the most reliable way to raise it.
  await page.getByTestId('member-schedule-dismiss').click()
  const confirm = page.getByTestId('member-schedule-discard')
  await confirm.waitFor({ timeout: 10000 })
  const confirmPanel = page.locator('[role="dialog"]').last()
  await confirmPanel.screenshot({ path: `${OUT}/06-discard-confirm-dark.png` })
  await confirm.click()

  // 07 — the late-failure notice: submit, dismiss mid-flight, then let the
  // request answer with a refusal.
  await openDialog()
  await fill('doomed-sweep')
  await page.getByTestId('member-schedule-submit').click()
  await page.getByTestId('member-schedule-inflight').waitFor({ timeout: 10000 })
  await page.getByTestId('member-schedule-dismiss').click()
  await page.evaluate(() => window.__failPendingCreate?.('cron store is read-only'))
  const late = page.getByTestId('member-schedule-late-error')
  await late.waitFor({ timeout: 10000 })
  if (!/wasn't created/i.test(await late.innerText())) {
    throw new Error('late-failure notice does not lead with the outcome')
  }
  await page.getByTestId('member-side-panel').screenshot({ path: `${OUT}/07-late-failure-dark.png` })

  // 08 — the same path, but the request gets NO answer: the notice must say the
  // schedule may still exist rather than claiming it does not.
  await page.getByTestId('member-schedule-late-error').getByRole('button', { name: /dismiss|close/i }).first().click().catch(() => {})
  await openDialog()
  await fill('no-answer-sweep')
  await page.getByTestId('member-schedule-submit').click()
  await page.getByTestId('member-schedule-inflight').waitFor({ timeout: 10000 })
  await page.getByTestId('member-schedule-dismiss').click()
  await page.evaluate(() => window.__dropPendingCreate?.())
  const unknown = page.getByTestId('member-schedule-late-error')
  await unknown.waitFor({ timeout: 10000 })
  if (!/may not have been created/i.test(await unknown.innerText())) {
    throw new Error('no-answer notice should not claim the schedule was not created')
  }
  await page.getByTestId('member-side-panel').screenshot({ path: `${OUT}/08-late-unknown-dark.png` })
  await context.close()
}

// 03 — light
{
  const { context, page } = await openMembers('light')
  await shotWakeBlock(page, '03-wake-create-light')
  await context.close()
}

await browser.close()
srv.close()
console.log(`wrote frames to ${OUT}`)
