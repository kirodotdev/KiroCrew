/**
 * Screenshot harness, and behaviour check, for the close-tree guard.
 *
 * Option B: the ✕ REFUSES while any descendant is running, and closes the whole
 * subtree silently when none are. There is no "close anyway" button.
 *
 * This ASSERTS as well as photographs. Every beat checks the behaviour before
 * photographing it; a wrong frame exits non-zero rather than ships as evidence.
 *
 * Beats:
 *   1. the conductor lane, tree expanded, nothing asked          tree.png
 *   2. ✕ on the lead (3 sessions mid-turn) → blocked notice      blocked.png
 *   3. dismiss the notice → nothing closed, tree unchanged        dismissed.png
 *   4. ✕ on the same tree with every worker now finished
 *      → closes whole subtree with NO prompt (pref off)           idle-before.png / idle-after.png
 *   4b. same tree, preference ON → in-app confirm, then closes    pref-confirm.png
 *   5. ✕ on a leaf card → no prompt                             (assertion only)
 *   6. one close refused by the server → partial-failure notice   refused.png
 *   7. phone: row menu close → same blocked notice               phone-menu.png / phone-blocked.png
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6186 --strictPort      # in another shell
 *   node scripts/capture-close-session-tree.mjs http://127.0.0.1:6186 [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6186'
const OUT = process.argv[3] || '../temp-screenshots/close-session-tree'
const LEAD = 'chat-lead'
const SOLO = 'chat-solo'
const SUBTREE = ['chat-babysit', 'chat-rerun', 'chat-logs', 'chat-locale']

mkdirSync(OUT, { recursive: true })

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed = true
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
// Wide enough that the sidebar renders its DESKTOP row cluster: below the mobile
// breakpoint a row drops the hover ✕ for a single ⋯ menu.
const context = await browser.newContext({ viewport: { width: 1040, height: 780 }, deviceScaleFactor: 2 })
const page = await context.newPage()
page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })

/** Every DELETE the close path sends, in order. */
const deleted = []
/** Every resume (Undo) the notice sends, in order. */
const resumed = []
/** The history key each resume carried in its body. */
const resumedHistoryKeys = []
/** Keys whose DELETE the stub refuses (server-side close refusal). */
const refuse = new Set()
/** Slot keys whose resume (Undo) the stub refuses. */
const refuseResume = new Set()
// A refused child keeps its lead open, so the refetch still nests it under the lead.
const KEPT_LEAD_ROW = {
  key: LEAD, title: 'Conductor: dashboard lane', messages: 12, running: false,
  agent: 'kirocrew', last_ts: new Date().toISOString(), last_message: 'Three workers reporting.',
  parent: null,
}
const REFUSED_ROW = {
  key: 'chat-babysit', title: 'worker: pull-request babysit', messages: 12, running: false,
  agent: 'kirocrew-worker', last_ts: new Date().toISOString(), last_message: 'Waiting on the review lane.',
  parent: { slot: LEAD, key: LEAD },
}
const SOLO_ROW = {
  key: SOLO, title: 'Notes: release checklist', messages: 12, running: false,
  agent: 'kirocrew', last_ts: new Date().toISOString(), last_message: 'Nothing under this one.',
  parent: null,
}
const PHONE = { width: 400, height: 820 }

await stubDashboardApi(page, {
  theme: 'dark',
  folders: [],
  extra: async (path, route) => {
    if (route.request().method() === 'POST' && /^\/api\/chat\/slots\/[^/]+\/resume$/.test(path)) {
      // Undo sends the HISTORY key (`dashboard_<slot>`), the same as an Older
      // sessions row; the backend answers with the bare slot it opened.
      const historyKey = JSON.parse(route.request().postData() || '{}').key || ''
      resumedHistoryKeys.push(historyKey)
      const key = historyKey.replace(/^dashboard_/, '')
      resumed.push(key)
      if (refuseResume.has(key)) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'resume failed', code: 'resume_failed' }) })
        return true
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, key, mode: 'default', messages: [] }) })
      return true
    }
    if (route.request().method() === 'DELETE' && path.startsWith('/api/chat/slots/')) {
      const key = decodeURIComponent(path.slice('/api/chat/slots/'.length))
      deleted.push(key)
      if (refuse.has(key)) {
        await route.fulfill({
          status: 500, contentType: 'application/json',
          body: JSON.stringify({
            error: 'a history write for this conversation is still running; the tab stays open, close it again in a moment',
            code: 'history_write_running',
          }),
        })
        return true
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
      return true
    }
    // A refused close refetches the slot list; answer with what would still be there.
    if (path === '/api/chat/slots' && refuse.size > 0) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([KEPT_LEAD_ROW, REFUSED_ROW, SOLO_ROW]) })
      return true
    }
    // The dev server serves source modules by their own path; the stub's `**/api/**`
    // pattern also matches `/src/api/client.ts`.
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})
logPageProblems(page)

const PANEL = { x: 0, y: 0, width: 540, height: 780 }
const rowKeys = () => page.$$eval('[data-slot-key]', els => els.map(el => el.getAttribute('data-slot-key')))
const dialog = () => page.locator('[role="dialog"]')

/** Press the ✕ on one row. Hover first: it is a reveal-on-hover control.
 *  The aria-label now depends on whether the row has nested sessions, so we
 *  match by its danger-variant class instead. */
async function pressClose(key) {
  const row = page.locator(`[data-slot-key="${key}"]`).first()
  await row.hover()
  await row.locator('button[aria-label*="lose"]').first().click()
}

/** Load one case of the harness and wait for the lane to paint. */
async function open(query = '') {
  await page.goto(`${BASE}/capture/close-session-tree.html?theme=dark${query}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`[data-slot-key="${LEAD}"]`)
  await page.waitForTimeout(400)
}

await open()

// ── 1. the tree, before anything is pressed ────────────────────────────────
const before = await rowKeys()
check('the lane nests the whole subtree under the lead',
  SUBTREE.every(k => before.includes(k)), before.join(', '))
await page.screenshot({ path: `${OUT}/tree.png`, clip: PANEL })

// ── 1a. a running tree: the ✕ and menu say "Can't close" before the press ──
const BLOCKED_LABEL = "Can't close: 4 sessions are still running"
{
  const row = page.locator(`[data-slot-key="${LEAD}"]`).first()
  await row.hover()
  await page.waitForTimeout(150)
  const bx = row.locator('[data-testid="row-close-blocked"]').first()
  check('a running tree shows a muted "Can\'t close" control, not "Close 5"',
    await bx.count() === 1 && await row.locator('[data-testid="row-close-tree"]').count() === 0)
  check('its visible label counts the running sessions',
    (await bx.locator('[data-testid="row-close-blocked-label"]').innerText()).trim() === "Can't close: 4 running")
  check('its tooltip and accessible name say who is running',
    (await bx.getAttribute('title')) === BLOCKED_LABEL && (await bx.getAttribute('aria-label')) === BLOCKED_LABEL)
  const box = await row.boundingBox()
  await page.screenshot({ path: `${OUT}/x-blocked.png`, clip: { x: 0, y: Math.max(0, box.y - 20), width: 540, height: box.height + 40 } })
  await row.click({ button: 'right' })
  const item = page.getByRole('menuitem', { name: BLOCKED_LABEL })
  await item.waitFor({ state: 'visible', timeout: 5000 })
  check('the row menu says it cannot close, muted', (await item.getAttribute('data-close-muted')) === '')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)
}

// ── 1b. a finished tree: the lead's ✕ and row menu name the subtree they reach ─
await open('&finished=1')
const TREE_LABEL = 'Close all 5: this session and the 4 under it (you can undo or reopen them from Older sessions)'
const leadRow = page.locator(`[data-slot-key="${LEAD}"]`).first()
await leadRow.hover()
const leadX = leadRow.locator(`button[aria-label="${TREE_LABEL}"]`).first()
check('the lead ✕ is labelled with the subtree', await leadX.count() === 1)
check('the lead ✕ tooltip names the subtree', (await leadX.getAttribute('title')) === TREE_LABEL)
check('the lead ✕ shows its reach as a visible count',
  (await leadX.locator('[data-testid="row-close-count"]').innerText()).trim() === '5')
await page.mouse.move(900, 700)
await page.waitForTimeout(150)
await leadRow.hover()
await page.waitForTimeout(150)
check('at rest, with the pointer off the ✕, it reads "Close 5"',
  await leadX.locator('[data-testid="row-close-word"]').isVisible()
    && `${(await leadX.locator('[data-testid="row-close-word"]').innerText()).trim()} ${(await leadX.locator('[data-testid="row-close-count"]').innerText()).trim()}` === 'Close 5')
check('the ✕ word and count sit on one line',
  await leadX.evaluate(el => el.scrollWidth <= el.clientWidth + 1 && el.getBoundingClientRect().height < 30))
check('a leaf ✕ carries no count',
  await page.locator(`[data-slot-key="${SOLO}"] [data-testid="row-close-count"]`).count() === 0)
const leadBox = await leadRow.boundingBox()
await page.screenshot({ path: `${OUT}/x-count.png`, clip: { x: 0, y: Math.max(0, leadBox.y - 20), width: 540, height: leadBox.height + 40 } })
// The native `title` tooltip is drawn by the OS, outside the page, so a
// screenshot cannot catch it. Render the same string as an in-page bubble
// at the ✕ for the frame, read from the button's own `title` attribute.
await leadX.hover()
await page.evaluate(label => {
  const btn = [...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === label)
  const r = btn.getBoundingClientRect()
  const tip = document.createElement('div')
  tip.id = 'capture-tooltip'
  tip.textContent = btn.getAttribute('title')
  Object.assign(tip.style, {
    position: 'fixed', top: `${r.bottom + 6}px`, left: `${Math.max(8, r.right - 420)}px`, maxWidth: '520px',
    font: '12px system-ui, sans-serif', color: '#111', background: '#f5f5f5',
    border: '1px solid #999', padding: '3px 6px', borderRadius: '3px', zIndex: 99999, whiteSpace: 'normal',
  })
  document.body.appendChild(tip)
}, TREE_LABEL)
await page.waitForTimeout(200)
check('hovering the ✕ keeps the "Close" word beside the count',
  await leadX.locator('[data-testid="row-close-word"]').isVisible()
    && `${(await leadX.locator('[data-testid="row-close-word"]').innerText()).trim()} ${(await leadX.locator('[data-testid="row-close-count"]').innerText()).trim()}` === 'Close 5')
check('the count carries a screen-reader hint of what it counts',
  (await leadX.locator('[data-testid="row-close-hint"]').textContent()) === '5 sessions'
    && (await leadX.getAttribute('aria-describedby')) === `row-close-hint-${LEAD}`)
await page.screenshot({ path: `${OUT}/x-hover-label.png`, clip: { x: 0, y: Math.max(0, leadBox.y - 20), width: 540, height: leadBox.height + 40 } })
check('the hover tooltip text is the subtree label',
  (await page.locator('#capture-tooltip').innerText()) === TREE_LABEL)
await page.screenshot({ path: `${OUT}/x-tooltip.png`, clip: { x: 0, y: Math.max(0, leadBox.y - 20), width: 1040, height: leadBox.height + 110 } })
await page.evaluate(() => document.getElementById('capture-tooltip')?.remove())
await leadRow.click({ button: 'right' })
const MENU_HINT = 'This session and the 4 under it. You can undo, or reopen them from Older sessions.'
const menuClose = page.getByRole('menuitem', { name: /^Close all 5/ })
await menuClose.waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(300)
check('the row menu Close item reads "Close all 5"', await menuClose.isVisible())
check('the row menu carries the undo line under it',
  (await menuClose.locator('[data-testid="close-item-hint"]').innerText()).trim() === MENU_HINT)
await page.screenshot({ path: `${OUT}/row-menu.png`, clip: { x: 0, y: 0, width: 1040, height: 600 } })
await page.keyboard.press('Escape')
await page.waitForTimeout(300)

// ── 2. ✕ on a running lead → blocked notice (no "close anyway") ───────────
await open()
await pressClose(LEAD)
await dialog().waitFor({ state: 'visible', timeout: 5000 })
await page.locator('[data-testid="close-tree-levels"]').waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(350)
check('nothing closed while the notice is open', deleted.length === 0, deleted.join(', '))

const text = await dialog().innerText()
const says = needle => text.toLowerCase().includes(needle.toLowerCase())
check('the title counts the 4 running, the lead included, as the X does',
  says("Can't close: 4 sessions are still running, including this one"), text.split('\n')[0])
check('the notice says the lead can be closed once it and the rest finish', says('This session can be closed once it and the sessions under it finish.'))
check('the body names the Stop control and where it is', says("Open a running session and press Stop generation (the square button in the chat's message box), or let it finish."))
check('a running row is a button', await page.locator('button[data-testid="close-tree-session-chat-rerun"]').count() === 1)
check('a finished row is not a button', await page.locator('button[data-testid="close-tree-session-chat-logs"]').count() === 0)
for (const level of ['This session', '1 level under', '2 levels under']) {
  check(`the notice lists ${level}`, says(level))
}
const marks = await page.$$eval('[data-testid^="close-tree-session-"]', els => Object.fromEntries(
  els.map(el => [el.getAttribute('data-testid').replace('close-tree-session-', ''), el.innerText.trim()]),
))
const marked = (key, word) => (marks[key] || '').toLowerCase().endsWith(word)
check('the notice marks mid-turn sessions under the lead',
  ['chat-babysit', 'chat-rerun'].every(k => marked(k, 'running')), JSON.stringify(marks))
check('the running lead row says Running',
  marked('chat-lead', 'running'), marks['chat-lead'])
check('the running lead is counted like the rows under it',
  (await page.getByTestId('close-tree-session-chat-lead').getAttribute('data-status')) === 'running')
check('the RUNNING rows match the title count (lead + 3)',
  await page.locator('[data-testid="close-tree-levels"] [data-status="running"]').count() === 4)
check('a row running only through subagents reads Running in background',
  (marks['chat-locale'] || '').endsWith('Running in background'), marks['chat-locale'])
check('every busy tag in the notice says Running',
  Object.values(marks).every(t => /\n(Running|Running in background|Finished)$/.test(t)), JSON.stringify(marks))
check('the notice marks finished sessions',
  marked('chat-logs', 'finished'), JSON.stringify(marks))
check('the notice names every session', Object.keys(marks).length === 5, Object.keys(marks).join(', '))
// Option B: dismiss only, NO "close anyway" button.
check('the dismiss button is present', await page.getByTestId('close-tree-dismiss').isVisible())
check('there is no close-all button', (await dialog().getByRole('button', { name: /Close all/i }).count()) === 0)
await page.screenshot({ path: `${OUT}/blocked.png` })

// ── 3. dismiss → nothing closes, tree unchanged ────────────────────────────
await page.getByTestId('close-tree-dismiss').click()
await dialog().waitFor({ state: 'hidden', timeout: 5000 })
await page.waitForTimeout(350)
check('dismiss closed nothing', deleted.length === 0, deleted.join(', '))
const afterDismiss = await rowKeys()
check('dismiss left every row on screen',
  [LEAD, ...SUBTREE].every(k => afterDismiss.includes(k)), afterDismiss.join(', '))
await page.mouse.move(500, 1400)
await page.waitForTimeout(150)
await page.screenshot({ path: `${OUT}/dismissed.png`, clip: PANEL })

// ── 4. finished subtree → closes whole tree with NO prompt ─────────────────
deleted.length = 0
await open('&finished=1')
await page.screenshot({ path: `${OUT}/idle-before.png`, clip: PANEL })
await pressClose(LEAD)
await page.waitForFunction(() => !document.querySelector('[data-slot-key="chat-lead"]'), null, { timeout: 8000 })
await page.waitForTimeout(400)
check('a finished subtree raises no prompt', await dialog().count() === 0)
check('a finished subtree closes whole, deepest first',
  deleted.length === 5 && deleted.at(-1) === LEAD, deleted.join(' -> '))
check('a child closes before its own parent',
  deleted.indexOf('chat-rerun') < deleted.indexOf('chat-babysit'), deleted.join(' -> '))
const afterIdle = await rowKeys()
check('only the unrelated session is left',
  afterIdle.includes(SOLO) && !afterIdle.includes(LEAD), afterIdle.join(', '))
const closedNotice = page.getByTestId('close-tree-closed')
await closedNotice.waitFor({ state: 'visible', timeout: 5000 })
check('the lane says what closed', (await closedNotice.innerText()).includes('Closed 5 sessions'), await closedNotice.innerText())
check('the notice offers Undo', await page.getByTestId('close-tree-undo').isVisible())
await page.screenshot({ path: `${OUT}/idle-after.png`, clip: PANEL })
await page.screenshot({ path: `${OUT}/closed-notice.png`, clip: { x: 0, y: 0, width: 540, height: 200 } })
resumed.length = 0
await page.getByTestId('close-tree-undo').click()
await page.waitForFunction(() => !document.querySelector('[data-testid="close-tree-closed"]'), null, { timeout: 5000 })
await page.waitForTimeout(800)
check('Undo reopens exactly the closed sessions, lead first',
  resumed.length === 5 && resumed[0] === LEAD && [...SUBTREE].every(k => resumed.includes(k)), resumed.join(' -> '))
check('Undo reopens a parent before its child',
  resumed.indexOf('chat-babysit') < resumed.indexOf('chat-rerun'), resumed.join(' -> '))
check('Undo sends each session\'s history key, not its slot key',
  resumedHistoryKeys.length === 5 && resumedHistoryKeys.every(k => k.startsWith('dashboard_')), resumedHistoryKeys.join(', '))
check('Undo sends the served history_key verbatim',
  resumedHistoryKeys[0] === `dashboard_${LEAD}`, resumedHistoryKeys.join(', '))
// The slots frame the gateway sends after a resume carries each `parent` again.
await page.evaluate(() => window.__captureRestoreTree())
await page.waitForTimeout(600)
const afterUndo = await rowKeys()
check('after Undo the tree is back under its lead',
  [LEAD, ...SUBTREE].every(k => afterUndo.includes(k)), afterUndo.join(', '))
await page.mouse.move(500, 1400)
await page.waitForTimeout(150)
await page.screenshot({ path: `${OUT}/undone.png`, clip: PANEL })

// ── 4a. an Undo the server refuses for one session → named failure notice ──
deleted.length = 0
refuseResume.add('chat-locale')
await open('&finished=1')
await pressClose(LEAD)
await page.getByTestId('close-tree-undo').waitFor({ state: 'visible', timeout: 8000 })
await page.getByTestId('close-tree-undo').click()
const undoFailed = page.getByTestId('close-tree-undo-failed')
await undoFailed.waitFor({ state: 'visible', timeout: 8000 })
await page.waitForTimeout(600)
const undoFailedText = await undoFailed.innerText()
check('a failed Undo is reported', undoFailedText.includes('Some sessions did not reopen'), undoFailedText)
check('the failed Undo names the session', undoFailedText.includes('worker: locale sweep'), undoFailedText)
check('the failed Undo reads in the singular', undoFailedText.includes('open it from Older sessions'), undoFailedText)
check('the failed Undo offers the agent hand-off', undoFailedText.includes('Ask the agent'), undoFailedText)
await page.evaluate(() => window.__captureRestoreTree(['chat-locale']))
await page.waitForTimeout(500)
await page.screenshot({ path: `${OUT}/undo-failed.png`, clip: { x: 0, y: 0, width: 540, height: 360 } })
refuseResume.clear()

// ── 4b. same tree, preference ON → in-app confirm (useConfirm, not window.confirm) ─
deleted.length = 0
await open('&finished=1&confirmclose=1')
await pressClose(LEAD)
await dialog().waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(350)
check('the preference confirm opens', await dialog().getByRole('button', { name: /Close all 5/i }).isVisible())
check('the confirm says everything has finished',
  (await dialog().innerText()).includes('They have all finished'))
check('the confirm title counts the 4 under it',
  (await dialog().innerText()).includes('Close this session and the 4 sessions under it?'))
check('nothing closed while the pref confirm is open', deleted.length === 0, deleted.join(', '))
await page.screenshot({ path: `${OUT}/pref-confirm.png` })
await dialog().getByRole('button', { name: /Close all/i }).click()
await page.waitForFunction(() => !document.querySelector('[data-slot-key="chat-lead"]'), null, { timeout: 8000 })
check('pref confirm closes the whole tree', deleted.length === 5 && deleted.at(-1) === LEAD, deleted.join(' -> '))
await page.waitForTimeout(300)
check('a confirmed close shows no second receipt', await page.getByTestId('close-tree-closed').count() === 0)

// ── 4c. only the lead still running → the press refuses for the lead itself ─
deleted.length = 0
await open('&leadonly=1&confirmclose=1')
await pressClose(LEAD)
await dialog().waitFor({ state: 'visible', timeout: 5000 })
await page.locator('[data-testid="close-tree-levels"]').waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(350)
const leadText = await dialog().innerText()
check('a running lead refuses even with every worker finished',
  leadText.includes("Can't close: this session is still running"), leadText.split('\n')[0])
check('it says the lead can be closed once it finishes', leadText.includes('This session can be closed once it finishes.'))
check('with only the lead running the Stop hint still shows', leadText.includes('press Stop generation (the square button in the chat\'s message box)'))
check('the running lead is the one running row',
  await page.locator('[data-testid="close-tree-levels"] [data-status="running"]').count() === 1
    && (await page.getByTestId('close-tree-session-chat-lead').getAttribute('data-status')) === 'running')
check('no confirm is offered for a running lead', (await dialog().getByRole('button', { name: /Close all/i }).count()) === 0)
check('nothing closed for a running lead', deleted.length === 0, deleted.join(', '))
await page.screenshot({ path: `${OUT}/lead-running.png` })
await page.getByTestId('close-tree-dismiss').click()
await dialog().waitFor({ state: 'hidden', timeout: 5000 })

// ── 5. leaf card → no prompt, delegates to single-session close ────────────
// Load a fresh page with preference off so the leaf close is not gated by
// window.confirm (which playwright dismisses automatically).
deleted.length = 0
await open('')
await pressClose(SOLO)
await page.waitForTimeout(600)
check('a leaf card raises no prompt', await dialog().count() === 0)
check('a leaf card closed anyway', deleted.length === 1 && deleted[0] === SOLO, deleted.join(', '))

// ── 6. one close in a finished tree REFUSED → partial-failure notice ────────
deleted.length = 0
refuse.add('chat-babysit')
await open('&finished=1')
await pressClose(LEAD)
const errNotice = page.locator('[data-testid="close-tree-refused"]')
await errNotice.waitFor({ state: 'visible', timeout: 8000 })
await page.waitForTimeout(600)
const noticeText = await errNotice.innerText()
check('the refused session is named', noticeText.includes('worker: pull-request babysit'), noticeText)
check('the error names only the failed close', !noticeText.includes('Conductor: dashboard lane'), noticeText)
const keptText = await page.getByTestId('close-tree-kept').innerText()
check('the lead kept open above it says why, by name',
  keptText.includes('Kept open: Conductor: dashboard lane, because worker: pull-request babysit under it did not close.'), keptText)
check('the kept line offers no agent hand-off', !keptText.includes('Ask the agent'), keptText)
check('a session that closed is not named', !noticeText.includes('worker: locale sweep'), noticeText)
check('the lead above a refusal is never closed', !deleted.includes(LEAD), deleted.join(' -> '))
check('the off-path sessions still closed',
  ['chat-rerun', 'chat-logs', 'chat-locale'].every(k => deleted.includes(k)), deleted.join(' -> '))
const partialText = await page.getByTestId('close-tree-closed').innerText()
check('the partial receipt names what closed',
  ['worker: rerun the flaky lane', 'worker: fetch the job logs', 'worker: locale sweep'].every(t => partialText.includes(t)), partialText)
const afterRefuse = await rowKeys()
check('the refused worker stays nested under its lead',
  afterRefuse.includes(LEAD) && afterRefuse.includes('chat-babysit'), afterRefuse.join(', '))
await page.screenshot({ path: `${OUT}/refused.png`, clip: PANEL })
refuse.clear()

// ── 7. phone: row's only close is its menu → same blocked notice ───────────
deleted.length = 0
await page.setViewportSize(PHONE)
await open('&finished=1')
const phoneRow = page.locator(`[data-slot-key="${LEAD}"]`).first()
await phoneRow.locator('[aria-label="More options"]').first().click()
const closeItem = page.getByRole('menuitem', { name: /^Close all 5/ })
await closeItem.waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(300)
check('the phone menu close wraps instead of truncating', await closeItem.evaluate(el => {
  const hint = el.querySelector('[data-testid="close-item-hint"]')
  const menu = el.closest('[role="menu"]')
  return !!hint && hint.scrollWidth <= hint.clientWidth + 1 && hint.getBoundingClientRect().height > 20
    && el.getBoundingClientRect().right <= menu.getBoundingClientRect().right + 1
    && menu.getBoundingClientRect().right <= window.innerWidth
}))
await page.screenshot({ path: `${OUT}/phone-menu.png` })
await page.keyboard.press('Escape')
await page.waitForTimeout(300)
// A running tree: the same menu says it cannot close, and pressing it explains why.
await open()
await page.locator(`[data-slot-key="${LEAD}"]`).first().locator('[aria-label="More options"]').first().click()
const blockedItem = page.getByRole('menuitem', { name: BLOCKED_LABEL })
await blockedItem.waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(300)
check('the phone menu says it cannot close before the press', await blockedItem.isVisible())
await blockedItem.click()
await dialog().waitFor({ state: 'visible', timeout: 5000 })
await page.waitForTimeout(350)
check('the phone menu raises the blocked notice', await page.getByTestId('close-tree-dismiss').isVisible())
check('no close-all on phone either', (await dialog().getByRole('button', { name: /Close all/i }).count()) === 0)
check('nothing closed from the phone menu', deleted.length === 0, deleted.join(', '))
const phoneTitle = page.getByTestId('close-tree-title')
check('the title wraps instead of truncating at phone width',
  await phoneTitle.evaluate(el => el.parentElement.scrollWidth <= el.parentElement.clientWidth + 1 && el.getBoundingClientRect().height > 30))
check('the wrapped title fits inside the header, no line clipped',
  await phoneTitle.evaluate(el => { const t = el.getBoundingClientRect(); const h = el.closest('[role="dialog"]').firstElementChild.getBoundingClientRect(); return t.top >= h.top - 1 && t.bottom <= h.bottom + 1 }))
await page.screenshot({ path: `${OUT}/phone-blocked.png` })
// A running row opens that session: the notice goes and nothing closes.
await page.locator('button[data-testid="close-tree-session-chat-rerun"]').click()
await dialog().waitFor({ state: 'hidden', timeout: 5000 })
check('opening a running row closes the notice and nothing else', deleted.length === 0, deleted.join(', '))

await context.close()
await browser.close()
console.log(failed ? '\nFAILED' : `\nOK — frames in ${OUT}`)
process.exit(failed ? 1 : 0)
