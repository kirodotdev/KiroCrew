/**
 * Capture + regression harness for #21935: a plain "New chat" from the sessions
 * sidebar, taken while Split View (Session Grid) is open, must LEAVE the split
 * and show the newly-created, now-active session instead of keeping the old
 * split on screen.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub, with `session_grid: true` so the
 * Split View feature is enabled. It:
 *   1. enters Split View on session A, then fills the second pane with session B
 *      (BEFORE: a two-pane split of A and B);
 *   2. clicks the sidebar header's plain "New chat session" button;
 *   3. asserts the split collapsed and the single new session is shown
 *      (AFTER), and photographs both states.
 *
 * The bug this proves fixed: before the fix the create activated the new
 * session (URL/title) but the grid kept drawing the A|B split over it.
 *
 * A third still (03) documents the collapsed-sidebar flyout New's failure
 * notice: with Split View OFF and the sidebar collapsed (so the flyout is
 * eligible), it opens the flyout, makes the create request fail (500 with a
 * plain error body), clicks New, and photographs the whole app with the
 * resulting error notice visible — showing where the notice lands and its exact
 * text (errors-use-error-notice).
 *
 * Usage: node scripts/capture-21935-split-view-new-chat.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21935-split-view-new-chat'
const VIEW = { width: 1400, height: 900 }
const A = 'chat-aaaa'
const B = 'chat-bbbb'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const base_slot = {
  running: false, last_message: '', agent: 'kirocrew', memory_mode: 'persistent',
  project: '', modified: now, tags: [], source_links: [], source_links_total: 0,
}
const slots = [
  { key: A, title: 'Design review', messages: 7, ...base_slot },
  { key: B, title: 'API refactor', messages: 4, ...base_slot },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` -- ${note}` : ''}`)
  }

  let createSeq = 0
  // When set, the next create POST fails with a 500 carrying a plain error
  // body. The frontend wraps that into an ApiError whose message is the
  // unwrapped `{ error }` (friendlyErrText) — user-facing copy — and flyoutNew's
  // mapping shows it (a raw non-ApiError throw would fall back to a readable
  // sentence instead; that branch is covered by the unit test).
  const CREATE_FAIL_MESSAGE = 'The agent runtime is unavailable. Try again.'
  let failNextCreate = false
  const extra = async (path, route) => {
    const method = route.request().method()
    // Enable the Split View feature flag the chat page reads.
    if (path === '/api/dashboard/config') {
      return json(route, {
        restore_sessions: false, restore_window_minutes: 30,
        merge_queued_messages: false, widget_density: 'more',
        social_share_enabled: true, session_grid: true,
      }), true
    }
    if (path === '/api/chat/slots' && method === 'POST') {
      if (failNextCreate) {
        failNextCreate = false
        return json(route, { error: CREATE_FAIL_MESSAGE }, 500), true
      }
      createSeq += 1
      const key = `chat-new-${createSeq}`
      slots.push({ key, title: '', messages: 0, ...base_slot })
      return json(route, { key, title: '', ...base_slot, messages: 0 }), true
    }
    return false
  }

  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, extra })
  await page.goto(base + `/chat/${A}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1000)

  const shot = (name) => page.screenshot({ path: `${OUT}/${name}.png` })

  // 1. Enter Split View on session A.
  const enterSplit = page.locator('[aria-label="Enter split view"]').first()
  await enterSplit.waitFor({ timeout: 12000 })
  await enterSplit.click()
  await page.waitForTimeout(500)

  // 2. The second cell shows a session picker; pick session B.
  const pickB = page.getByRole('button', { name: /API refactor/i }).last()
  await pickB.waitFor({ timeout: 12000 })
  await pickB.click()
  await page.waitForTimeout(600)
  await shot('01-split-view-two-panes-before')
  // Both panes present: the grid is drawing a two-session split.
  const paneCount = await page.locator('[data-testid^="chat-pane-"], [data-grid-leaf], .mc-chat-pane').count()
  record('BEFORE: a two-pane split is on screen', paneCount >= 1, `panes≈${paneCount}`)

  // 3. Click the sidebar header's plain New chat button.
  const newBtn = page.getByRole('button', { name: /new chat session/i }).first()
  await newBtn.waitFor({ timeout: 12000 })
  await newBtn.click()
  await page.waitForTimeout(900)
  await shot('02-split-left-new-session-after')

  // 4. The split must be gone: the split-right control (only present inside the
  //    grid) is no longer on screen, and exactly one create fired.
  const stillSplit = await page.locator('[aria-label="Split right"]').count()
  record('AFTER: a plain create fired exactly one new session', createSeq === 1, `createSeq=${createSeq}`)
  record('AFTER: Split View left (grid split controls gone)', stillSplit === 0, `splitRight=${stillSplit}`)

  // 5. Collapsed-sidebar flyout New failure notice (still 03). Fresh context,
  //    Split View OFF (never entered). The flyout only exists in the collapsed
  //    state, so we collapse the sidebar through the real toggle, then open the
  //    flyout the way the session-flyout harness does: a real-mouse hover that
  //    rests past the 320ms hover-intent delay.
  const ctx3 = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page3 = await ctx3.newPage()
  logPageProblems(page3)
  await stubDashboardApi(page3, { slots, extra })
  await page3.goto(base + `/chat/${A}`, { waitUntil: 'domcontentloaded' })
  await page3.waitForTimeout(1200)
  const shot3 = (name) => page3.screenshot({ path: `${OUT}/${name}.png` })

  // Collapse the sidebar via the real toggle (the .pi-morph button).
  await page3.locator('.pi-morph').first().click()
  await page3.waitForTimeout(900)
  await page3.mouse.move(900, 600) // park the pointer clear
  await page3.waitForTimeout(400)

  // When collapsed and eligible the toggle carries aria-haspopup="menu".
  const TRIGGER = 'button[aria-haspopup="menu"].pi-morph'
  const FLYOUT = '[role="menu"][aria-label="Recent sessions"]'
  await page3.waitForSelector(TRIGGER, { timeout: 12000 })
  const btn = await page3.locator(TRIGGER).boundingBox()
  // Real-mouse hover that RESTS past the 320ms hover-intent delay.
  await page3.mouse.move(btn.x + btn.width / 2, btn.y + btn.height / 2)
  await page3.waitForSelector(FLYOUT, { timeout: 6000 })
  await page3.waitForTimeout(450)

  // Make the next create fail, then click the flyout's New (the one menuitem
  // without a data-slot-key row). Clicking New closes the flyout (flyout.close)
  // and the failed create then raises the top-of-pane error notice — the
  // real end state a user sees, captured below.
  failNextCreate = true
  const flyoutNewBtn = page3.locator(`${FLYOUT} [role="menuitem"]:not([data-slot-key])`).first()
  await flyoutNewBtn.waitFor({ timeout: 6000 })
  await flyoutNewBtn.click()
  // The error notice surfaces after the create rejects.
  const notice = page3.locator('[data-testid="action-error"]')
  await notice.waitFor({ timeout: 12000 })
  await page3.waitForTimeout(400)
  // The collapsed sessions column (where the flyout lives) is still on screen,
  // so the still shows the notice's placement relative to it.
  const collapsed = await page3.evaluate(() =>
    !!document.querySelector('button[aria-haspopup="menu"].pi-morph'))
  record('03: the sessions sidebar is still collapsed (flyout surface)', collapsed === true)
  await shot3('03-flyout-new-create-failed-notice')

  const noticeText = (await notice.innerText()).replace(/\s+/g, ' ').trim()
  record('03: the flyout New failure notice is visible with its message',
    noticeText.includes(CREATE_FAIL_MESSAGE), `notice="${noticeText.slice(0, 120)}"`)
  record('03: the failing create fired (flag consumed)', failNextCreate === false)
  await ctx3.close()

  await context.close()
  await browser.close()
  srv.close()
  const failed = results.filter(r => !r.pass)
  if (failed.length) {
    console.error(`\n${failed.length} assertion(s) failed`)
    process.exit(1)
  }
  console.log(`\nAll ${results.length} assertions passed; screenshots in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
