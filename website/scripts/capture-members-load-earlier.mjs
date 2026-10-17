/**
 * Screenshots for the earlier-messages bar in a crewmate DM on the Members page.
 *
 * Drives the REAL MembersPage capture entry (capture/members-page.html). The
 * thread's newest page answers has_more=true; a `before=` request (the bar's
 * click) answers per scene: hang (loading), 500 (failed), or the older page.
 * Every frame asserts its state before it is written.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6834 --strictPort
 *   node scripts/capture-members-load-earlier.mjs http://127.0.0.1:6834 <outdir> [--expect=after|before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6834'
const OUT = positional[1] || '../temp-screenshots/members-load-earlier'
const EXPECT = (process.argv.find(a => a.startsWith('--expect=')) || '--expect=after').slice('--expect='.length)
mkdirSync(OUT, { recursive: true })

const ts = (i) => new Date(Date.UTC(2026, 9, 3, 8, 0, 0) + i * 60_000).toISOString()
const turn = (i, q, a) => [
  { role: 'user', content: q, ts: ts(2 * i) },
  { role: 'assistant', content: a, ts: ts(2 * i + 1) },
]
const OLDER = [
  ...turn(0, 'Morning. What is on the board?', 'Nine open PRs. Two are red on the same flaky test.'),
  ...turn(1, 'Which test?', 'test_slot_switch_race. It times out on the slow runner only.'),
  ...turn(2, 'Can you rerun just that lane?', 'Rerun started on both PRs.'),
  ...turn(3, 'Anything new from triage?', 'Three new issues. One is a duplicate of last week\'s report.'),
  ...turn(4, 'Close the duplicate.', 'Closed with a link to the original.'),
]
const NEWEST = [
  ...turn(5, 'Status on the two reruns?', 'Both green now. The flake did not come back.'),
  ...turn(6, 'Good. Who owns the scroll bug?', 'Nobody yet. I can take it after the patrol.'),
  ...turn(7, 'Take it.', 'Taken. Reading the pane code first.'),
  ...turn(8, 'Any ETA?', 'A first fix within the hour.'),
  ...turn(9, 'Ping me when the PR is up.', 'Will do.'),
]
const OLDEST_TEXT = 'Morning. What is on the board?'
const NEWEST_FIRST = 'Status on the two reruns?'

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

/** Members page with radar's DM open on its newest page. `older` decides what
 *  a `before=` page request answers: 'hang' | 'fail' | 'ok'. */
async function openDm(theme, older) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
  await routeMembersApi(page, { messages: NEWEST, running: false, has_more: true, total: 20, next_before: 10 })
  await page.route(u => new URL(u).pathname === '/api/teams', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"teams":[]}' }))
  // Newest-first: this one wins for the thread's page requests.
  await page.route(u => /^\/api\/chat\/slots\/member-radar$/.test(new URL(u).pathname), route => {
    const before = new URL(route.request().url()).searchParams.get('before')
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (before === null) return json({ messages: NEWEST, running: false, has_more: true, total: 20, next_before: 10 })
    if (older === 'hang') return
    if (older === 'fail') return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' })
    return json({ messages: OLDER, running: false, has_more: false, total: 20, next_before: 0 })
  })
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText(NEWEST_FIRST).waitFor()
  await page.waitForTimeout(300)
  return page
}

const bar = (page) => page.getByTestId('load-earlier-messages')
/** Scroll the DM transcript to its top, where the bar lives. */
async function toTop(page) {
  await page.evaluate(() => {
    for (const el of document.querySelectorAll('[data-chat-pane] *')) {
      if (el.scrollHeight > el.clientHeight + 4 && getComputedStyle(el).overflowY.match(/auto|scroll/)) el.scrollTop = 0
    }
  })
  await page.waitForTimeout(250)
}
const inView = (page, text) => page.getByText(text, { exact: true }).first().evaluate((el) => {
  const r = el.getBoundingClientRect()
  return r.bottom > 0 && r.top < window.innerHeight
})

if (EXPECT === 'before') {
  const page = await openDm('dark', 'ok')
  await toTop(page)
  const n = await bar(page).count()
  check('00-before: no bar on main', n === 0, `bars=${n}`)
  await page.screenshot({ path: `${OUT}/00-before-no-bar-dark.png` })
  await page.close()
} else {
  for (const theme of ['dark', 'light']) {
    // 01 idle: bar at the top of the DM, older history exists.
    {
      const page = await openDm(theme, 'ok')
      await toTop(page)
      const n = await bar(page).count()
      check(`01-${theme} idle bar`, n === 1, `bars=${n}`)
      await page.screenshot({ path: `${OUT}/01-idle-${theme}.png` })

      // 04 after: click, older turns load above, reader keeps place.
      const anchorBefore = await inView(page, NEWEST_FIRST)
      await bar(page).click()
      await page.getByText(OLDEST_TEXT).waitFor({ state: 'attached' })
      await page.waitForTimeout(400)
      const anchorAfter = await inView(page, NEWEST_FIRST)
      const left = await bar(page).count()
      check(`04-${theme} after: older loaded, bar gone (no more history)`, left === 0, `bars=${left}`)
      check(`04-${theme} after: reader keeps place`, anchorBefore && anchorAfter, `anchor in view before=${anchorBefore} after=${anchorAfter}`)
      await page.screenshot({ path: `${OUT}/04-after-load-${theme}.png` })
      await toTop(page)
      const oldestVisible = await inView(page, OLDEST_TEXT)
      check(`05-${theme} scrolled up: oldest turn reachable`, oldestVisible, `oldest in view=${oldestVisible}`)
      await page.screenshot({ path: `${OUT}/05-scrolled-to-oldest-${theme}.png` })
      await page.close()
    }
    // 02 loading: the page request is in flight.
    {
      const page = await openDm(theme, 'hang')
      await toTop(page)
      await bar(page).click()
      await page.waitForTimeout(250)
      const busy = await bar(page).getAttribute('aria-busy')
      check(`02-${theme} loading`, busy === 'true', `aria-busy=${busy}`)
      await page.screenshot({ path: `${OUT}/02-loading-${theme}.png` })
      await page.close()
    }
    // 03 failed: the page request returned 500.
    {
      const page = await openDm(theme, 'fail')
      await toTop(page)
      await bar(page).click()
      await page.waitForTimeout(600)
      const busy = await bar(page).getAttribute('aria-busy')
      const notice = await page.locator('[data-chat-pane]').getByText(/older|earlier/i).count()
      check(`03-${theme} failed`, busy === 'false' && (await bar(page).count()) === 1, `aria-busy=${busy} textHits=${notice}`)
      await page.screenshot({ path: `${OUT}/03-failed-${theme}.png` })
      await page.close()
    }
  }
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
