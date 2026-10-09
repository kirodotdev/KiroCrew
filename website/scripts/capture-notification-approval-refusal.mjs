/**
 * Frames of the notifications feed and detail panel handling a tool approval
 * that can no longer be decided (#14711), rendered from the REAL built SPA
 * (website/dist) gateway-free with stubDashboardApi.
 *
 * An approval whose decide the server refused (404) is RETIRED: kept,
 * Approve/Reject withdrawn, no critical border or unread dot, and its ordinary
 * close X still dismisses it. The refusal is an error, so its sentence is an
 * alert. An expiry the server reports removes the row. Two approval rows
 * are seeded, so every feed frame also shows the other row keeps its buttons.
 * Each frame asserts its state before it is written.
 *
 * Frames:
 *   01-feed-404-retired       404 on decide: the row stays, retired, with the
 *                             refusal as an error notice and focus on its close X;
 *                             no DELETE was sent
 *   03-feed-503-plain         503 with the layer's own text: buttons kept, the
 *                             notice on that row says it in plain words and
 *                             never quotes that text
 *   04-detail-503-plain       same failure in the detail panel, buttons kept
 *   05-detail-404-retired     404 in the detail panel: buttons withdrawn, the
 *                             refusal as an error notice, focus on the panel's
 *                             Close; the feed row's close X removes it
 *   06-feed-offline-light     no response at all: hedged notice on the row, light
 *   09-popover-404-retired    frame 01 in the topbar bell popover
 *   10-popover-503-plain      frame 03 in the bell popover: buttons kept, the
 *                             notice renders in the card footer
 *   13-popover-ws-expiry-removed  the approval expires server-side (socket
 *                             `approval_resolved`, decision `expired`): the
 *                             row leaves the bell popover, no DELETE sent
 *   15-popover-live-expiry.webm  recording of the expiry arriving while the
 *                             approval is on screen: the row leaves
 *   16-feed-dismiss-failed    a stored note's close X is pressed and the
 *                             DELETE fails (500): the row comes back with the
 *                             dismiss-failed notice and its close X to retry
 *   17-detail-decided-dismiss-failed  a stored approval note's decision lands
 *                             in the detail panel and its DELETE fails (500):
 *                             the panel stays open, Approve/Reject withdrawn,
 *                             saying the decision was recorded; its button
 *                             reads Dismiss notification, and the open row does not repeat it
 *
 * Usage: npm run build && node scripts/capture-notification-approval-refusal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

// A refused press reads the sentence every approval surface uses for it.
const REFUSED = 'This approval has expired or was already decided'
// A retryable failure: the decide route answers only 400 or 404, so a 503
// comes from the layer in between, in that layer's words. The row says it in
// plain words and never quotes this text.
const REASON = 'gateway busy, try again'
const HEDGED = 'may not have been recorded — the request failed. Try again.'

// Every approval row names the request it was raised for: the id plus the
// coordinator's server-issued instance (and its slot, '' here).
const note = (ts, id, title) => ({
  kind: 'approval', source: 'system', channel: 'system.approval', priority: 'critical',
  title, body: 'Nightly backup wants to run `rsync -a ~/data /mnt/backup`.',
  ts, acked: false, approval_id: id, approval_instance: `inst-${id}`,
})
// The authority's answer for requests still pending: the reconcile retires
// any listed row whose request is not in it.
const pendingOf = (rows, slot = '') => rows.filter(n => n.kind === 'approval').map(n => ({
  id: n.approval_id, instance: n.approval_instance, slot, source: 'agent', tool: 'shell',
  tool_input: 'rsync -a ~/data /mnt/backup', ts: Date.parse(n.ts) / 1000,
}))
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

async function open(theme, { status, error, popover = false, deleteStatus = 200 }) {
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
        if (deleteStatus !== 200) { await json(route, { error: 'store write failed' }, deleteStatus); return true }
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
      if (path === '/api/approvals') { await json(route, pendingOf(notes)); return true }
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
    focusedOnDismiss: await page.evaluate(() => !!document.activeElement?.hasAttribute('data-notif-dismiss')),
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
  s => s.listed === 1 && s.alert.includes(REFUSED) && s.pressedButtons === 0 && s.focusedOnDismiss && s.deletes === 0 && !s.dangerBorder && s.dot === 0)
await feedFrame('03-feed-503-plain', 'dark', { status: 503, error: REASON },
  s => s.rowText.includes(HEDGED) && !s.rowText.includes(REASON) && s.pressedButtons === 2)
await detailFrame('04-detail-503-plain', 'dark', { status: 503, error: REASON },
  s => s.alert.includes(HEDGED) && !s.alert.includes(REASON) && s.buttons >= 2)
await detailFrame('05-detail-404-retired', 'dark', { status: 404, error: 'not found or expired' },
  s => s.alert.includes(REFUSED) && s.status === '' && s.dismiss === 0 && s.focused === 'notif-detail-close' && s.deletes === 0)
await feedFrame('06-feed-offline-light', 'light', {},
  s => s.rowText.includes(HEDGED) && s.pressedButtons === 2)
await feedFrame('09-popover-404-retired', 'dark', { status: 404, error: 'not found or expired', popover: true },
  s => s.listed === 1 && s.alert.includes(REFUSED) && s.pressedButtons === 0 && s.focusedOnDismiss && s.dot === 0 && s.closeOpacity > 0.5)
await feedFrame('10-popover-503-plain', 'dark', { status: 503, error: REASON, popover: true },
  s => s.listed === 1 && s.alert.includes(HEDGED) && !s.alert.includes(REASON) && s.pressedButtons === 2)

// 16: a stored note whose DELETE fails comes back with the dismiss-failed
// notice; its close X stays, so the reader can retry.
{
  const DISMISS_FAILED = "Couldn't dismiss this notification"
  const { page, counts } = await open('dark', { status: 200, deleteStatus: 500 })
  const row = rowOf(page, CRON.title)
  await row.hover()
  await row.getByRole('button', { name: 'Dismiss notification' }).click()
  await page.getByTestId('notif-dismiss-failed').first().waitFor()
  await page.waitForTimeout(800)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  const s = {
    listed: await page.locator('[data-notif-row]').filter({ hasText: CRON.title }).count(),
    notice: (await row.getByTestId('notif-dismiss-failed').allInnerTexts()).join(' | '),
    close: await row.getByRole('button', { name: 'Dismiss notification' }).count(),
    closeOpacity: await row.getByRole('button', { name: 'Dismiss notification' }).evaluate(el => Number(getComputedStyle(el).opacity)),
    opacity: await row.evaluate(el => Number(getComputedStyle(el).opacity)),
    deletes: counts.deletes,
  }
  await shot(page, '16-feed-dismiss-failed',
    s.listed === 1 && s.notice.includes(DISMISS_FAILED) && s.close === 1 && s.closeOpacity > 0.5 && s.opacity > 0.9 && s.deletes === 1,
    JSON.stringify(s))
}

// 17: a decision on a stored approval note lands and its DELETE fails: the
// detail panel stays open with the controls withdrawn, says the decision was
// recorded, and its one button is Dismiss.
{
  const DISMISS_FAILED = 'Your decision was recorded, but this notification couldn'
  const { page, counts } = await open('dark', { status: 200, deleteStatus: 500 })
  await page.getByText(PRESSED.title).first().click()
  const pressed = page.getByRole('button', { name: /^Approve$/ }).last()
  await pressed.waitFor()
  await pressed.focus()
  await page.keyboard.press('Enter')
  const area = page.getByTestId('notif-approval-refusal-focus')
  await area.getByTestId('notif-dismiss-failed').waitFor()
  await page.waitForTimeout(500)
  const s = {
    alert: (await area.getByRole('alert').allInnerTexts()).join(' | '),
    panelButtons: await page.getByTestId('notif-approval-refusal-focus').locator('xpath=..').getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    pressedRowButtons: await rowOf(page, PRESSED.title).getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    close: await page.getByTestId('notif-detail-close').innerText(),
    rowRepeats: await rowOf(page, PRESSED.title).getByTestId('notif-dismiss-failed').count(),
    deletes: counts.deletes,
  }
  await shot(page, '17-detail-decided-dismiss-failed',
    s.alert.includes(DISMISS_FAILED) && s.alert.includes("couldn't be dismissed") && s.panelButtons === 0 && s.pressedRowButtons === 0 && s.close.includes('Dismiss notification') && s.rowRepeats === 0 && s.deletes === 1,
    JSON.stringify(s))
}

// 13: the approval expires server-side while the reader is in its chat. The
// coordinator's `approval_resolved` frame (decision `expired`) removes the row,
// as it always has for a chat-owned one: nothing this view pressed, so there
// is nothing to report on it. Driven through the real socket handlers: an
// `approval` frame, then its expiry.
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
  // The row the tab holds for this approval.
  const listed = [{ ...PRESSED, ts: tsSeconds, slot: SLOT }]
  await stubDashboardApi(page, {
    theme: 'dark', slots,
    localStorageEntries: { 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      if (path === '/api/notifications' && route.request().method() === 'DELETE') { counts.deletes += 1; await json(route, { ok: true }); return true }
      if (path === '/api/notifications') { await json(route, { notifications: listed, unread: 1 }); return true }
      // Still pending when the tab opens: the reconcile keeps the row live.
      if (path === '/api/approvals') { await json(route, pendingOf(listed, SLOT)); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, { running: true, has_more: false, total: 0, queue: [], messages: [] }); return true }
      return false
    },
  })
  let ws = null
  await page.routeWebSocket(/\/api\/ws/, s => { ws = s })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  // Let the first-connect reconcile settle first.
  await page.waitForTimeout(2500)
  const push = async (type, data) => { ws?.send(JSON.stringify({ type, data })); await page.waitForTimeout(900) }
  await push('approval', {
    id: PRESSED.approval_id, instance: PRESSED.approval_instance, slot: SLOT, source: 'agent', tool: 'shell',
    tool_input: 'rsync -a ~/data /mnt/backup', ts: Number(tsSeconds),
  })
  await push('approval_resolved', { id: PRESSED.approval_id, origin: 'coordinator', instance: PRESSED.approval_instance, slot: SLOT, approved: false, decision: 'expired' })
  await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
  await page.waitForTimeout(600)
  const s = {
    rows: await page.locator('[data-notif-row]').count(),
    empty: await page.getByTestId('notification-feed-empty').count(),
    deletes: counts.deletes,
  }
  await shot(page, '13-popover-ws-expiry-removed', s.rows === 0 && s.empty === 1 && s.deletes === 0, JSON.stringify(s))
}

// 15 (video): the live moment a still cannot show. The approval is on screen
// with Approve/Reject in the bell popover, then the coordinator's expiry frame
// arrives while the reader watches: the row leaves. Recorded as webm.
{
  const SLOT = 'chat-nightly-backup'
  const VIDEO_DIR = mkdtempSync(`${OUT}/.video-`)
  const tsSeconds = String(Date.parse(PRESSED.ts) / 1000)
  const slots = [{
    key: SLOT, title: 'Nightly backup', running: true, last_message: 'Waiting on your approval.',
    messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
    modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
  }]
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }, colorScheme: 'dark',
    // A run-private directory: the recording is moved out of it, and only it
    // is removed afterwards, never a directory the caller already had.
    recordVideo: { dir: VIDEO_DIR, size: { width: 1280, height: 800 } },
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
      // Still pending when the tab opens: the reconcile keeps the row live.
      if (path === '/api/approvals') { await json(route, pendingOf(listed, SLOT)); return true }
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
    id: PRESSED.approval_id, instance: PRESSED.approval_instance, slot: SLOT, source: 'agent', tool: 'shell',
    tool_input: 'rsync -a ~/data /mnt/backup', ts: Number(tsSeconds),
  })
  await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
  const row = page.locator('[data-notif-row]').first()
  await row.getByRole('button', { name: /^Approve$/ }).waitFor()
  await page.waitForTimeout(1800)
  await push('approval_resolved', { id: PRESSED.approval_id, origin: 'coordinator', instance: PRESSED.approval_instance, slot: SLOT, approved: false, decision: 'expired' })
  await page.getByTestId('notification-feed-empty').waitFor()
  await page.waitForTimeout(2200)
  const rows = await page.locator('[data-notif-row]').count()
  const video = page.video()
  await context.close()
  const ok = rows === 0
  console.log(`15-popover-live-expiry.webm: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify({ rows })}`)
  if (!ok) failed = true
  else await video.saveAs(`${OUT}/15-popover-live-expiry.webm`)
  rmSync(VIDEO_DIR, { recursive: true, force: true })
}

await browser.close()
srv.close()
process.exit(failed ? 1 : 0)
