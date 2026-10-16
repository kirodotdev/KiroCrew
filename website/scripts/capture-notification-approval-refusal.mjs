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
 *                             refusal as an error notice with Dismiss next to
 *                             it, focus on the panel's Close (which only closes)
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
 *                             a neutral status says the decision was recorded
 *                             and the failed clean-up renders beneath it as an
 *                             error notice; Close stays Close, Dismiss sits by
 *                             the notices, and the open row does not repeat it
 *   18-feed-decided-dismiss-failed  the same failed DELETE, pressed from the
 *                             feed row: controls withdrawn, the same neutral
 *                             status and clean-up error notice on the row
 *   17b-detail-decided-dismiss-failed-light  frame 17 in the light theme
 *   19-feed-400-quoted        400 from the decide route: the row quotes its sentence
 *   20-detail-400-quoted      the same 400 in the detail panel
 *   21-popover-cron-approval-pending  a cron job's approval, which has no chat
 *                             slot, listed in the bell popover with its buttons
 *   22-popover-cron-slotless-expiry-removed  the coordinator's expiry frame for
 *                             that approval carries its instance and NO slot:
 *                             the row leaves, no DELETE sent
 *   23-mobile-dismiss-focus-list  mobile page, a retired approval's Dismiss in
 *                             the detail panel: the panel closes, the feed
 *                             column shows again and keyboard focus lands on
 *                             its list (role group, named), not on <body>
 *   24-feed-expiry-during-503  Approve pressed, the expiry frame lands while the
 *                             decide is in flight, then a 503: the row stays
 *                             retired, buttons withdrawn, the failure then "no
 *                             longer pending" as error notices, focus on its X
 *   25-detail-expiry-during-503  that row opened: the detail panel shows the
 *                             same two notices in the same order with Dismiss,
 *                             and the row does not repeat them
 *   26-detail-retired-dismiss-failed  a 404 retires the open row in the
 *                             detail panel, then its feed close X DELETE fails
 *                             (503): the panel says the dismiss failed under
 *                             "no longer pending", and the row does not repeat it
 *
 * Usage: npm run build && node scripts/capture-notification-approval-refusal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, rmSync } from 'node:fs'
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
// It hedges, and says the next step: pressing again is safe.
const HEDGED = 'may not have been recorded because the request failed. Try again.'
// The same failure on a request that ended meanwhile: nothing is left to
// press, so it does not say to try again.
const ENDED_FAILED = 'This decision may not have been recorded'
// The failed clean-up after a decision that landed.
const DISMISS_FAILED = "This notification couldn't be dismissed. Try again."
// Both views lead with the failure, then "no longer pending".
const failureFirst = text => text.includes(ENDED_FAILED) && text.indexOf(ENDED_FAILED) < text.indexOf(REFUSED)

const note = (ts, id, title, body) => ({
  kind: 'approval', source: 'system', channel: 'system.approval', priority: 'critical',
  title, body,
  ts, acked: false, approval_id: id,
})
const NOTES = [
  note('2026-09-28T02:00:00.000000+00:00', 'apr-backup', 'Tool approval: shell (Nightly backup)',
    'Nightly backup wants to run `rsync -a ~/data /mnt/backup`.'),
  note('2026-09-28T01:00:00.000000+00:00', 'apr-report', 'Tool approval: shell (Weekly report)',
    'Weekly report wants to run `python3 scripts/weekly_report.py`.'),
  { kind: 'cron', source: 'cron', channel: 'cron.done', title: 'Nightly report finished', body: 'All 14 checks passed.', ts: '2026-09-28T00:30:00.000000+00:00', acked: false },
]
const PRESSED = NOTES[0]
const OTHER = NOTES[1]
const CRON = NOTES[2]
// The server-issued instance a live approval frame names, and the
// approval_resolved frame that ends it names again: a row bound to an
// instance leaves only with a frame naming that instance.
const LIVE_INSTANCE = 'inst-live-expiry'

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false

async function open(theme, { status, error, popover = false, deleteStatus = 200, viewport = { width: 1280, height: 800 } }) {
  const page = await browser.newPage({ viewport, colorScheme: theme })
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
    dismiss: await page.getByTestId('notif-detail-dismiss').count(),
    close: await page.getByTestId('notif-detail-close').innerText(),
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
  s => s.alert.includes(REFUSED) && s.status === '' && s.dismiss === 1 && s.close.trim() === 'Close' && s.focused === 'notif-detail-close' && s.deletes === 0)
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
// detail panel stays open with the controls withdrawn, a neutral status says
// the decision was recorded, the failed clean-up is an error notice beneath it,
// Close stays Close and Dismiss sits by the notices. Shot dark (17) and light
// (17b), so the boxed status-over-error pair is seen in both themes.
async function decidedDismissFailedFrame(name, theme) {
  const RECORDED = 'Your decision was recorded.'
  const { page, counts } = await open(theme, { status: 200, deleteStatus: 500 })
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
    status: (await area.getByRole('status').allInnerTexts()).join(' | '),
    dismiss: await page.getByTestId('notif-detail-dismiss').count(),
    dismissGlyphs: await page.getByTestId('notif-detail-dismiss').locator('svg').count(),
    focused: await focusedTestId(page),
    panelButtons: await page.getByTestId('notif-approval-refusal-focus').locator('xpath=..').getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    pressedRowButtons: await rowOf(page, PRESSED.title).getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    close: await page.getByTestId('notif-detail-close').innerText(),
    rowRepeats: await rowOf(page, PRESSED.title).getByTestId('notif-dismiss-failed').count(),
    deletes: counts.deletes,
  }
  await shot(page, name,
    s.alert.includes(DISMISS_FAILED) && !s.alert.includes(RECORDED) && s.status.includes(RECORDED) && !s.status.includes(DISMISS_FAILED) && s.panelButtons === 0 && s.pressedRowButtons === 0 && s.close.trim() === 'Close' && s.dismiss === 1 && s.dismissGlyphs === 0 && s.focused !== 'notif-detail-dismiss' && s.rowRepeats === 0 && s.deletes === 1,
    JSON.stringify(s))
}
await decidedDismissFailedFrame('17-detail-decided-dismiss-failed', 'dark')
await decidedDismissFailedFrame('17b-detail-decided-dismiss-failed-light', 'light')

// 26: a 404 retires the row open in the detail panel; its feed close X then
// sends a DELETE that fails (503). The row made no decision, so the panel says
// the plain dismiss failure under "no longer pending", never "recorded", and
// the open row leaves that notice to the panel.
{
  const RECORDED = 'Your decision was recorded.'
  const PLAIN = "Couldn't dismiss this notification"
  const { page, counts } = await open('dark', { status: 404, error: 'not found or expired', deleteStatus: 503 })
  await page.getByText(PRESSED.title).first().click()
  const pressed = page.getByRole('button', { name: /^Approve$/ }).last()
  await pressed.waitFor()
  await pressed.focus()
  await page.keyboard.press('Enter')
  const area = page.getByTestId('notif-approval-refusal-focus')
  await area.getByTestId('notif-approval-retired').waitFor()
  const row = rowOf(page, PRESSED.title)
  await row.hover()
  await row.getByRole('button', { name: 'Dismiss notification' }).click()
  await area.getByTestId('notif-dismiss-failed').waitFor()
  await page.waitForTimeout(800)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  const alert = (await area.getByRole('alert').allInnerTexts()).join(' | ')
  const s = {
    alert,
    status: (await area.getByRole('status').allInnerTexts()).join(' | '),
    order: alert.indexOf(REFUSED) >= 0 && alert.indexOf(REFUSED) < alert.indexOf(PLAIN),
    listed: await page.locator('[data-notif-row]').filter({ hasText: PRESSED.title }).count(),
    rowRepeats: await row.getByTestId('notif-dismiss-failed').count(),
    dismiss: await page.getByTestId('notif-detail-dismiss').count(),
    deletes: counts.deletes,
  }
  await shot(page, '26-detail-retired-dismiss-failed',
    s.order && !s.alert.includes(DISMISS_FAILED) && !s.alert.includes(RECORDED) && s.status === '' && s.listed === 1 && s.rowRepeats === 0 && s.dismiss === 1 && s.deletes === 1,
    JSON.stringify(s))
}

// 23: the mobile page hides the feed column while a note is selected. A 404
// retires the approval in the detail panel; its Dismiss closes the panel and,
// once the column is shown again, keyboard focus lands on the feed's list
// (not on <body>), and the dismissed row is gone (its one DELETE).
{
  const { page, counts } = await open('dark', { status: 404, error: REFUSED, viewport: { width: 390, height: 844 } })
  await page.getByText(PRESSED.title).first().click()
  const pressed = page.getByRole('button', { name: /^Approve$/ }).last()
  await pressed.waitFor()
  await pressed.click()
  const dismiss = page.getByTestId('notif-detail-dismiss')
  await dismiss.waitFor()
  await dismiss.focus()
  await page.keyboard.press('Enter')
  await page.getByTestId('notification-feed-list').waitFor({ state: 'visible' })
  await page.waitForTimeout(500)
  const s = {
    focused: await focusedTestId(page),
    listName: await page.getByTestId('notification-feed-list').getAttribute('aria-label'),
    listRole: await page.getByTestId('notification-feed-list').getAttribute('role'),
    pressedRows: await page.locator('[data-notif-row]').filter({ hasText: PRESSED.title }).count(),
    panels: await page.getByTestId('notif-detail-dismiss').count(),
    deletes: counts.deletes,
  }
  await shot(page, '23-mobile-dismiss-focus-list',
    s.focused === 'notification-feed-list' && s.listRole === 'group' && s.listName === 'Notifications' && s.pressedRows === 0 && s.panels === 0 && s.deletes === 1,
    JSON.stringify(s))
}

// 18: the same landed-decision-with-failed-DELETE as 17, pressed from the feed
// row (the panel is closed): the row stays, Approve/Reject are withdrawn, and
// the row says the decision was recorded, with the clean-up error beneath.
{
  const { page, counts } = await open('dark', { status: 200, deleteStatus: 500 })
  const row = rowOf(page, PRESSED.title)
  await row.getByRole('button', { name: /^Approve$/ }).focus()
  await page.keyboard.press('Enter')
  await row.getByTestId('notif-dismiss-failed').waitFor()
  await page.waitForTimeout(500)
  const s = {
    recorded: (await row.getByTestId('notif-decision-recorded').allInnerTexts()).join(' | '),
    recordedRole: await row.getByTestId('notif-decision-recorded').getAttribute('role'),
    notice: (await row.getByTestId('notif-dismiss-failed').allInnerTexts()).join(' | '),
    noticeRole: await row.getByTestId('notif-dismiss-failed').getAttribute('role'),
    pressedButtons: await row.getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    otherButtons: await rowOf(page, OTHER.title).getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    deletes: counts.deletes,
  }
  await shot(page, '18-feed-decided-dismiss-failed',
    s.recorded.includes('Your decision was recorded') && s.recordedRole === 'status' && s.notice.includes(DISMISS_FAILED) && s.noticeRole === 'alert' && s.pressedButtons === 0 && s.otherButtons === 2 && s.deletes === 1,
    JSON.stringify(s))
}

// 19 and 20: a 400 from the decide route itself carries a sentence written
// for the user, so the feed row and the detail panel quote it.
{
  const SENTENCE = 'This approval needs a project to run in'
  const QUOTED = `This decision wasn't recorded: ${SENTENCE}`
  await feedFrame('19-feed-400-quoted', 'dark', { status: 400, error: SENTENCE },
    s => s.rowText.includes(QUOTED) && s.pressedButtons === 2 && s.deletes === 0)
  await detailFrame('20-detail-400-quoted', 'dark', { status: 400, error: SENTENCE },
    s => s.alert.includes(QUOTED) && s.buttons >= 2 && s.deletes === 0)
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
    tool_input: 'rsync -a ~/data /mnt/backup', ts: Number(tsSeconds), instance: LIVE_INSTANCE,
  })
  await push('approval_resolved', { id: PRESSED.approval_id, slot: SLOT, approved: false, decision: 'expired', instance: LIVE_INSTANCE })
  await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
  await page.waitForTimeout(600)
  const s = {
    rows: await page.locator('[data-notif-row]').count(),
    empty: await page.getByTestId('notification-feed-empty').count(),
    deletes: counts.deletes,
  }
  await shot(page, '13-popover-ws-expiry-removed', s.rows === 0 && s.empty === 1 && s.deletes === 0, JSON.stringify(s))
}

// 21 and 22: the headline case. A cron job's approval has no chat slot, so
// its expiry frame names only the approval id and the coordinator instance.
// The row must still leave. Frame 21 is the row before, 22 the popover after.
{
  const CRON_INSTANCE = 'inst-cron-expiry'
  const CRON_APPROVAL = {
    kind: 'approval', source: 'system', channel: 'system.approval', priority: 'critical',
    title: 'Tool approval: shell (Nightly cleanup)', body: 'Nightly cleanup wants to run `find ~/logs -mtime +30 -delete`.',
    ts: String(Date.parse('2026-09-28T03:00:00.000000+00:00') / 1000), acked: false,
    approval_id: 'apr-cron-cleanup', approval_instance: CRON_INSTANCE,
  }
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: 'dark' })
  logPageProblems(page)
  const counts = { deletes: 0 }
  // One ordinary chat open, which shares nothing with the cron approval.
  const slots = [{
    key: 'chat-notes', title: 'Notes', running: false, last_message: 'Done.',
    messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
    modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
  }]
  await stubDashboardApi(page, {
    theme: 'dark', slots,
    localStorageEntries: { 'mc-active-slot': 'chat-notes' },
    extra: async (path, route) => {
      if (path === '/api/notifications' && route.request().method() === 'DELETE') { counts.deletes += 1; await json(route, { ok: true }); return true }
      if (path === '/api/notifications') { await json(route, { notifications: [CRON_APPROVAL], unread: 1 }); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }); return true }
      return false
    },
  })
  let ws = null
  await page.routeWebSocket(/\/api\/ws/, s => { ws = s })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const push = async (type, data) => { ws?.send(JSON.stringify({ type, data })); await page.waitForTimeout(900) }
  await push('approval', {
    id: CRON_APPROVAL.approval_id, source: 'cron', tool: 'shell',
    tool_input: 'find ~/logs -mtime +30 -delete', ts: Number(CRON_APPROVAL.ts), instance: CRON_INSTANCE,
  })
  await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
  await page.waitForTimeout(600)
  const before = {
    rows: await page.locator('[data-notif-row]').count(),
    buttons: await rowOf(page, CRON_APPROVAL.title).getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
  }
  // Not shot(): it closes the page, and frame 22 continues on this one.
  const beforeOk = before.rows === 1 && before.buttons === 2
  console.log(`21-popover-cron-approval-pending: ${beforeOk ? 'OK' : 'MISMATCH'} ${JSON.stringify(before)}`)
  if (beforeOk) await page.screenshot({ path: `${OUT}/21-popover-cron-approval-pending.png` })
  else failed = true
  // No `slot` key at all: a cron approval has none.
  await push('approval_resolved', { id: CRON_APPROVAL.approval_id, approved: false, decision: 'expired', instance: CRON_INSTANCE })
  await page.waitForTimeout(600)
  const after = {
    rows: await page.locator('[data-notif-row]').count(),
    empty: await page.getByTestId('notification-feed-empty').count(),
    deletes: counts.deletes,
  }
  await shot(page, '22-popover-cron-slotless-expiry-removed', after.rows === 0 && after.empty === 1 && after.deletes === 0, JSON.stringify(after))
}

// 24 and 25: the race. Approve is pressed, the coordinator's expiry frame
// arrives while the decide is in flight, and then the decide answers 503. The
// request has ended, so the row stays retired with the failure: Approve/Reject
// withdrawn, the failure and "no longer pending" as error notices, in the feed
// row (24) and, once the row is opened, in the detail panel (25).
{
  const tsSeconds = String(Date.parse(PRESSED.ts) / 1000)
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: 'dark' })
  logPageProblems(page)
  const counts = { deletes: 0 }
  let held = null
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: async (path, route) => {
      if (path === '/api/notifications' && route.request().method() === 'DELETE') { counts.deletes += 1; await json(route, { ok: true }); return true }
      if (path === '/api/notifications') { await json(route, { notifications: [], unread: 0 }); return true }
      if (path.startsWith('/api/approvals/')) { held = route; return true }
      return false
    },
  })
  let ws = null
  await page.routeWebSocket(/\/api\/ws/, s => { ws = s })
  await page.goto(base + '/notifications', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const push = async (type, data) => { ws?.send(JSON.stringify({ type, data })); await page.waitForTimeout(900) }
  await push('approval', {
    id: PRESSED.approval_id, source: 'cron', tool: 'shell',
    tool_input: 'rsync -a ~/data /mnt/backup', ts: Number(tsSeconds), instance: LIVE_INSTANCE,
  })
  const row = page.locator('[data-notif-row]').first()
  const approve = row.getByRole('button', { name: /^Approve$/ })
  await approve.waitFor()
  await approve.focus()
  await page.keyboard.press('Enter')
  for (let i = 0; i < 50 && !held; i++) await page.waitForTimeout(100)
  await push('approval_resolved', { id: PRESSED.approval_id, approved: false, decision: 'expired', instance: LIVE_INSTANCE })
  const listedMidFlight = await page.locator('[data-notif-row]').count()
  await held?.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: REASON }) })
  await row.getByTestId('notif-approval-retired').waitFor()
  await page.waitForTimeout(500)
  const feed = {
    held: !!held,
    listedMidFlight,
    rows: await page.locator('[data-notif-row]').count(),
    alert: (await row.getByRole('alert').allInnerTexts()).join(' | '),
    buttons: await row.getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    focusedOnDismiss: await page.evaluate(() => !!document.activeElement?.hasAttribute('data-notif-dismiss')),
    deletes: counts.deletes,
  }
  const feedOk = feed.held && feed.listedMidFlight === 1 && feed.rows === 1 && failureFirst(feed.alert)
    && !feed.alert.includes(REASON) && !feed.alert.includes('Try again') && feed.buttons === 0 && feed.focusedOnDismiss && feed.deletes === 0
  console.log(`24-feed-expiry-during-503: ${feedOk ? 'OK' : 'MISMATCH'} ${JSON.stringify(feed)}`)
  if (feedOk) await page.screenshot({ path: `${OUT}/24-feed-expiry-during-503.png` })
  else failed = true
  await row.click({ position: { x: 40, y: 12 } })
  const area = page.getByTestId('notif-approval-refusal-focus')
  await area.getByRole('alert').first().waitFor()
  await page.waitForTimeout(500)
  const panel = {
    alert: (await area.getByRole('alert').allInnerTexts()).join(' | '),
    buttons: await page.getByRole('button', { name: /^(Approve|Reject)$/ }).count(),
    dismiss: await page.getByTestId('notif-detail-dismiss').count(),
    rowRepeats: await row.getByRole('alert').count(),
  }
  await shot(page, '25-detail-expiry-during-503',
    failureFirst(panel.alert) && panel.buttons === 0 && panel.dismiss === 1 && panel.rowRepeats === 0,
    JSON.stringify(panel))
}

// 15 (video): the live moment a still cannot show. The approval is on screen
// with Approve/Reject in the bell popover, then the coordinator's expiry frame
// arrives while the reader watches: the row leaves. Recorded as webm.
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
    tool_input: 'rsync -a ~/data /mnt/backup', ts: Number(tsSeconds), instance: LIVE_INSTANCE,
  })
  await page.getByRole('button', { name: 'Notifications', exact: true }).first().click()
  const row = page.locator('[data-notif-row]').first()
  await row.getByRole('button', { name: /^Approve$/ }).waitFor()
  await page.waitForTimeout(1800)
  await push('approval_resolved', { id: PRESSED.approval_id, slot: SLOT, approved: false, decision: 'expired', instance: LIVE_INSTANCE })
  await page.getByTestId('notification-feed-empty').waitFor()
  await page.waitForTimeout(2200)
  const rows = await page.locator('[data-notif-row]').count()
  const video = page.video()
  await context.close()
  const ok = rows === 0
  console.log(`15-popover-live-expiry.webm: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify({ rows })}`)
  if (!ok) failed = true
  else await video.saveAs(`${OUT}/15-popover-live-expiry.webm`)
  rmSync(`${OUT}/.video`, { recursive: true, force: true })
}

await browser.close()
srv.close()
process.exit(failed ? 1 : 0)
