/**
 * Frames of the notifications feed and detail panel handling a tool approval
 * that can no longer be decided (#14711), rendered from the REAL built SPA
 * (website/dist) gateway-free with stubDashboardApi.
 *
 * A settled approval is RETIRED: kept, Approve/Reject withdrawn, no critical
 * border or unread dot, and its ordinary close X still dismisses it. A decide
 * the server refused (404) is an error, so its sentence is an alert; an
 * outcome that landed or an expiry is a neutral status line. Two approval rows
 * are seeded, so every feed frame also shows the other row keeps its buttons.
 * Each frame asserts its state before it is written.
 *
 * Frames:
 *   01-feed-404-retired       404 on decide: the row stays, retired, with the
 *                             refusal as an error notice and focus on the row;
 *                             no DELETE was sent
 *   03-feed-403-reason        403 with a server reason: buttons kept, the
 *                             notice on that row quotes the reason
 *   04-detail-403-reason      same refusal in the detail panel, buttons kept
 *   05-detail-404-retired     404 in the detail panel: buttons withdrawn, the
 *                             refusal as an error notice and a Dismiss, focus
 *                             on the notice
 *   06-feed-offline-light     no response at all: hedged notice on the row, light
 *   09-popover-404-retired    frame 01 in the topbar bell popover
 *   13-popover-ws-expiry-gone the approval expires server-side (socket
 *                             `approval_resolved`, decision `expired`): the
 *                             row stays with the neutral muted status line,
 *                             no error notice, in the bell popover
 *   14-feed-decide-in-flight  a decide still in flight: both buttons disabled
 *                             and busy, nothing retired yet
 *   15-popover-live-expiry.webm  recording of the expiry arriving while the
 *                             approval is on screen: the buttons leave and
 *                             the neutral line takes their place
 *   16-feed-approved-dismiss-failed  Approve lands but the DELETE that removes
 *                             the row is refused: the row stays with the
 *                             muted Approved line and an error notice saying
 *                             the dismiss failed, in both feeds' styling
 *   17-detail-approved        Approve lands in the detail panel while the
 *                             DELETE is still pending: the Approved line and
 *                             the Dismiss under it
 *   18-detail-dismiss-failed-light  Dismiss on a retired row is refused: the
 *                             panel stays open with the notice under Dismiss
 *
 * Usage: npm run build && node scripts/capture-notification-approval-refusal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, rmSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const GONE = 'This approval has expired or was already decided'
// A refused press opens on the decision not being recorded.
const REFUSED = "Your decision wasn't recorded"
const REASON = 'only the session owner can decide this approval'
const HEDGED = 'may not have been recorded'

const note = (ts, id, title) => ({
  kind: 'approval', source: 'system', channel: 'system.approval', priority: 'critical',
  title, body: 'Nightly backup wants to run `rsync -a ~/data /mnt/backup`.',
  ts, acked: false, approval_id: id,
})
const NOTES = [
  note('2026-09-28T02:00:00.000000+00:00', 'apr-backup', 'Tool approval: shell (Nightly backup)'),
  note('2026-09-28T01:00:00.000000+00:00', 'apr-report', 'Tool approval: shell (Weekly report)'),
  { kind: 'cron', source: 'cron', channel: 'cron.done', title: 'Nightly report finished', body: 'All 14 checks passed.', ts: '2026-09-28T00:30:00.000000+00:00', acked: false },
]
const PRESSED = NOTES[0]
const OTHER = NOTES[1]
const CRON = NOTES[2]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false

async function open(theme, { status, error, popover = false, del = 'ok' }) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: theme })
  logPageProblems(page)
  page.on('dialog', d => { void d.accept() })
  let notes = NOTES.map(n => ({ ...n }))
  const counts = { deletes: 0 }
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/notifications' && method === 'DELETE') {
        counts.deletes += 1
        // 'hang': the DELETE is still pending when the frame is taken; 500:
        // the server refused it, so the row stays.
        if (del === 'hang') return true
        if (del === 500) { await json(route, { error: 'notification store busy' }, 500); return true }
        const { ts } = JSON.parse(route.request().postData() || '{}')
        notes = notes.filter(n => n.ts !== ts)
        await json(route, { ok: true })
        return true
      }
      if (path === '/api/notifications/clear') {
        notes = []
        await json(route, { ok: true })
        return true
      }
      if (path === '/api/notifications') {
        await json(route, { notifications: notes, unread: notes.filter(n => !n.acked).length })
        return true
      }
      if (path.startsWith('/api/approvals/')) {
        // No status: the request never gets a response (a dropped connection).
        if (!status) { await route.abort('connectionreset'); return true }
        // 'hang': the decide is still in flight when the frame is taken.
        if (status === 'hang') return true
        await json(route, status === 200 ? { ok: true } : { error }, status)
        return true
      }
      return false
    },
  })
  // The popover frame opens the bell from another page, so only one feed (the
  // narrow `mac` one) is mounted and every locator below means that feed.
  await page.goto(base + (popover ? '/settings' : '/notifications'))
  await page.waitForFunction(t => (document.documentElement.dataset.theme || '').includes(t), theme, { timeout: 15000 })
  if (popover) {
    await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
    await page.waitForTimeout(600)
  }
  await page.getByText(PRESSED.title).first().waitFor()
  return { page, counts }
}

const rowOf = (page, title) => page.locator('[data-notif-row]').filter({ hasText: title }).first()

async function shot(page, name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) { failed = true; await page.close(); return }
  await page.screenshot({ path: `${OUT}/${name}.png` })
  await page.close()
}

const focusedTestId = page => page.evaluate(() => {
  const a = document.activeElement
  return a?.getAttribute('data-testid') || a?.closest('[data-testid]')?.getAttribute('data-testid') || ''
})

async function feedFrame(name, theme, opts, check, { action = 'Approve' } = {}) {
  const { page, counts } = await open(theme, opts)
  const row = rowOf(page, PRESSED.title)
  await row.getByRole('button', { name: new RegExp(`^${action}$`) }).focus()
  await page.keyboard.press('Enter')
  await page.locator('[data-testid="notif-approval-notice"], [data-testid="notif-approval-retired"]').first().waitFor()
  await page.waitForTimeout(500)
  const notice = page.getByTestId('notif-approval-notice')
  const s = {
    text: (await notice.count()) ? await notice.innerText() : '',
    listed: await page.locator('[data-notif-row]').filter({ hasText: PRESSED.title }).count(),
    rowText: await row.innerText(),
    pressedButtons: await row.getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    otherButtons: await rowOf(page, OTHER.title).getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    focused: await focusedTestId(page),
    focusedOnRow: await page.evaluate(() => !!document.activeElement?.hasAttribute('data-notif-open')),
    alert: (await row.getByRole('alert').allInnerTexts()).join(' | '),
    // Panel rows carry the priority border; a retired row must not.
    dangerBorder: await row.evaluate(el => el.className.includes('border-l-danger')),
    dot: await row.locator('[data-priority]').count(),
    // The close X is shown without a hover on a retired row (mouse is away).
    closeOpacity: await row.getByRole('button', { name: 'Dismiss notification' }).evaluate(el => Number(getComputedStyle(el).opacity)),
    deletes: counts.deletes,
  }
  await shot(page, name, s.otherButtons === 2 && check(s), JSON.stringify(s))
}

async function detailFrame(name, theme, opts, check, { action = 'Approve' } = {}) {
  const { page, counts } = await open(theme, opts)
  await page.getByText(PRESSED.title).first().click()
  const pressed = page.getByRole('button', { name: new RegExp(`^${action}$`) }).last()
  await pressed.waitFor()
  await pressed.focus()
  await page.keyboard.press('Enter')
  const area = page.getByTestId('notif-approval-refusal-focus')
  await area.locator('[role="alert"], [role="status"]').first().waitFor()
  await page.waitForTimeout(500)
  const s = {
    alert: (await area.getByRole('alert').allInnerTexts()).join(' | '),
    status: (await area.getByRole('status').allInnerTexts()).join(' | '),
    buttons: await page.getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    dismiss: await page.getByTestId('notif-decided-dismiss').count(),
    focused: await focusedTestId(page),
    deletes: counts.deletes,
  }
  await shot(page, name, check(s), JSON.stringify(s))
}

await feedFrame('01-feed-404-retired', 'dark', { status: 404, error: 'not found or expired' },
  s => s.listed === 1 && s.alert.includes(REFUSED) && s.pressedButtons === 0 && s.focusedOnRow && s.deletes === 0 && !s.dangerBorder && s.dot === 0)
await feedFrame('03-feed-403-reason', 'dark', { status: 403, error: REASON },
  s => s.rowText.includes(REASON) && s.pressedButtons === 2)
await detailFrame('04-detail-403-reason', 'dark', { status: 403, error: REASON },
  s => s.alert.includes(REASON) && s.buttons >= 2)
await detailFrame('05-detail-404-retired', 'dark', { status: 404, error: 'not found or expired' },
  s => s.alert.includes(REFUSED) && s.status === '' && s.dismiss === 1 && s.focused === 'notif-approval-refusal-focus' && s.deletes === 0)
await feedFrame('06-feed-offline-light', 'light', {},
  s => s.rowText.includes(HEDGED) && s.pressedButtons === 2)
await feedFrame('09-popover-404-retired', 'dark', { status: 404, error: 'not found or expired', popover: true },
  s => s.listed === 1 && s.alert.includes(REFUSED) && s.pressedButtons === 0 && s.focusedOnRow && s.dot === 0 && s.closeOpacity > 0.5)

// 13: the approval expires server-side while the reader is in its chat. The
// coordinator's `approval_resolved` frame (decision `expired`) retires the row
// as `gone`, which is neutral: nothing this view sent failed, so the row shows
// the muted status line, with no error notice. Driven through
// the real socket handlers: an `approval` frame, then its expiry.
{
  const SLOT = 'chat-nightly-backup'
  const TS = PRESSED.ts
  const tsSeconds = String(Date.parse(TS) / 1000)
  const slots = [{
    key: SLOT, title: 'Nightly backup', running: true, last_message: 'Waiting on your approval.',
    messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
    modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
  }]
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: 'dark' })
  logPageProblems(page)
  const counts = { deletes: 0 }
  // The server still lists the note after the expiry: only a DELETE removes it.
  const listed = [{ ...PRESSED, ts: tsSeconds, slot: SLOT }]
  await stubDashboardApi(page, {
    theme: 'dark', slots,
    localStorageEntries: { 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      if (path === '/api/notifications' && route.request().method() === 'DELETE') { counts.deletes += 1; await json(route, { ok: true }); return true }
      if (path === '/api/notifications') { await json(route, { notifications: listed, unread: 1 }); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, { running: true, has_more: false, total: 0, queue: [], messages: [] }); return true }
      return false
    },
  })
  let ws = null
  await page.routeWebSocket(/\/api\/ws/, s => { ws = s })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  // Let the first-connect reconcile (an empty /api/approvals) settle first.
  await page.waitForTimeout(2500)
  const push = async (type, data) => { ws?.send(JSON.stringify({ type, data })); await page.waitForTimeout(900) }
  await push('approval', {
    id: PRESSED.approval_id, slot: SLOT, source: 'agent', tool: 'shell',
    tool_input: 'rsync -a ~/data /mnt/backup', ts: Number(tsSeconds),
  })
  await push('approval_resolved', { id: PRESSED.approval_id, slot: SLOT, approved: false, decision: 'expired' })
  await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
  await page.waitForTimeout(600)
  const row = page.locator('[data-notif-row]').first()
  await row.waitFor()
  const s = {
    status: (await row.getByRole('status').allInnerTexts()).join(' | '),
    alert: (await row.getByRole('alert').allInnerTexts()).join(' | '),
    buttons: await row.getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    dismiss: await row.getByRole('button', { name: 'Dismiss notification' }).count(),
    dot: await row.locator('[data-priority]').count(),
    deletes: counts.deletes,
  }
  await shot(page, '13-popover-ws-expiry-gone',
    s.status.includes(GONE) && s.alert === '' && s.buttons === 0 && s.dismiss === 1 && s.dot === 0 && s.deletes === 0,
    JSON.stringify(s))
}

// 14: a decide still in flight: both buttons are disabled and busy, the row
// keeps them in place, and nothing has been retired yet.
{
  const { page } = await open('dark', { status: 'hang' })
  const row = rowOf(page, PRESSED.title)
  await row.getByRole('button', { name: /^Approve$/ }).click()
  await page.waitForTimeout(500)
  const s = {
    disabled: await row.locator('button[aria-busy="true"]:disabled').count(),
    retired: await row.getByTestId('notif-approval-retired').count(),
  }
  await shot(page, '14-feed-decide-in-flight', s.disabled === 2 && s.retired === 0, JSON.stringify(s))
}

// 16: a decision that landed, whose cleanup DELETE the server refused. The row
// keeps its recorded outcome (muted) and says the dismiss failed (an alert).
{
  const { page, counts } = await open('dark', { status: 200, del: 500 })
  const row = rowOf(page, PRESSED.title)
  await row.getByRole('button', { name: /^Approve$/ }).click()
  await row.getByTestId('notif-dismiss-failed').waitFor()
  await page.waitForTimeout(400)
  const s = {
    status: (await row.getByRole('status').allInnerTexts()).join(' | '),
    alert: (await row.getByRole('alert').allInnerTexts()).join(' | '),
    buttons: await row.getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    otherButtons: await rowOf(page, OTHER.title).getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    deletes: counts.deletes,
  }
  await shot(page, '16-feed-approved-dismiss-failed',
    s.status.includes('Approved') && s.alert.includes('Could not dismiss') && s.buttons === 0 && s.otherButtons === 2 && s.deletes === 1,
    JSON.stringify(s))
}

// 17: a decision that landed in the detail panel, its DELETE still pending:
// the Approved line, with the Dismiss under it. The two buttons left on the
// page are the other row's, in the feed behind the panel.
await detailFrame('17-detail-approved', 'dark', { status: 200, del: 'hang' },
  s => s.status.includes('Approved') && s.alert === '' && s.buttons === 2 && s.dismiss === 1 && s.deletes === 1)

// 18: Dismiss on a retired row is refused: the panel stays open and the notice
// under Dismiss says so.
{
  const { page, counts } = await open('light', { status: 404, error: 'not found or expired', del: 500 })
  await page.getByText(PRESSED.title).first().click()
  const approve = page.getByRole('button', { name: /^Approve$/ }).last()
  await approve.waitFor()
  await approve.click()
  const dismiss = page.getByTestId('notif-decided-dismiss')
  await dismiss.waitFor()
  await dismiss.click()
  const area = page.getByTestId('notif-approval-refusal-focus')
  await area.getByTestId('notif-dismiss-failed').waitFor()
  await page.waitForTimeout(400)
  const s = {
    alert: (await area.getByRole('alert').allInnerTexts()).join(' | '),
    dismiss: await dismiss.count(),
    deletes: counts.deletes,
  }
  await shot(page, '18-detail-dismiss-failed-light',
    s.alert.includes('Could not dismiss') && s.dismiss === 1 && s.deletes === 1,
    JSON.stringify(s))
}

// 15 (video): the live moment a still cannot show. The approval is on screen
// with Approve/Reject in the bell popover, then the coordinator's expiry frame
// arrives while the reader watches: the buttons leave and the neutral line
// takes their place. Recorded as webm.
{
  const SLOT = 'chat-nightly-backup'
  const tsSeconds = String(Date.parse(PRESSED.ts) / 1000)
  const slots = [{
    key: SLOT, title: 'Nightly backup', running: true, last_message: 'Waiting on your approval.',
    messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
    modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
  }]
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }, colorScheme: 'dark',
    recordVideo: { dir: `${OUT}/.video`, size: { width: 1280, height: 800 } },
  })
  const page = await context.newPage()
  logPageProblems(page)
  const listed = [{ ...PRESSED, ts: tsSeconds, slot: SLOT }]
  await stubDashboardApi(page, {
    theme: 'dark', slots,
    localStorageEntries: { 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      if (path === '/api/notifications' && route.request().method() === 'DELETE') { await json(route, { ok: true }); return true }
      if (path === '/api/notifications') { await json(route, { notifications: listed, unread: 1 }); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, { running: true, has_more: false, total: 0, queue: [], messages: [] }); return true }
      return false
    },
  })
  let ws = null
  await page.routeWebSocket(/\/api\/ws/, s => { ws = s })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const push = async (type, data) => { ws?.send(JSON.stringify({ type, data })); await page.waitForTimeout(900) }
  await push('approval', {
    id: PRESSED.approval_id, slot: SLOT, source: 'agent', tool: 'shell',
    tool_input: 'rsync -a ~/data /mnt/backup', ts: Number(tsSeconds),
  })
  await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
  const row = page.locator('[data-notif-row]').first()
  await row.getByRole('button', { name: /^Approve$/ }).waitFor()
  await page.waitForTimeout(1800)
  await push('approval_resolved', { id: PRESSED.approval_id, slot: SLOT, approved: false, decision: 'expired' })
  await row.getByTestId('notif-approval-retired').waitFor()
  await page.waitForTimeout(2200)
  const buttons = await row.getByRole('button', { name: /^(Approve|Reject)$/ }).count()
  const video = page.video()
  await context.close()
  const ok = buttons === 0
  console.log(`15-popover-live-expiry.webm: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify({ buttons })}`)
  if (!ok) failed = true
  else await video.saveAs(`${OUT}/15-popover-live-expiry.webm`)
  rmSync(`${OUT}/.video`, { recursive: true, force: true })
}

await browser.close()
srv.close()
process.exit(failed ? 1 : 0)
