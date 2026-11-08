/**
 * Screenshots for the crewmate chat's unread landing: re-entering a crewmate's
 * thread lands on the last message the reader saw, with the "N new messages"
 * divider above the first unseen one and the jump pill carrying the count.
 *
 * Drives the REAL MembersPage capture entry (capture/members-page.html) over
 * the worktree's own Vite server (started in-process, so no second shell), with
 * every /api call answered from fixtures. The previous visit is simulated by
 * seeding the caught-up marker (`vc_seen_<slot>`) in localStorage before load,
 * exactly as the pane writes it. Every frame asserts its state before it is
 * written.
 *
 * Frames (dark + light):
 *   01-landing       marker set, unseen run below: divider at the top, pill with count
 *   02-caught-up     marker set, no unseen run: opens at the bottom, no divider, no pill
 *   03-read-through  after scrolling to the bottom: pill gone, divider stays
 *   04-back-up       scrolled back up after reading through: divider still there,
 *                    the pill is the plain arrow again (the run is read)
 *
 * Usage (from website/):
 *   node scripts/capture-crewmate-unread-landing.mjs [<outdir>]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crewmate-unread-landing'
mkdirSync(OUT, { recursive: true })

const vite = await createServer({ root: process.cwd(), server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' })
await vite.listen()
const BASE = vite.resolvedUrls.local[0].replace(/\/$/, '')

const ts = (i) => new Date(Date.UTC(2026, 9, 23, 9, 0, 0) + i * 7 * 60_000).toISOString()
const SEEN = [
  { role: 'user', content: 'How did the nightly go?', ts: ts(0) },
  { role: 'assistant', content: 'All green. The two flaky lanes passed this time too.', ts: ts(1) },
  { role: 'user', content: 'Anyone on #22953 yet?', ts: ts(2) },
  { role: 'assistant', content: 'Not yet, the captain has not triaged it.', ts: ts(3) },
  { role: 'user', content: 'Leave it for now.', ts: ts(4) },
  { role: 'assistant', content: 'OK. I merged #22744 meanwhile.', ts: ts(5) },
  { role: 'user', content: 'Where is PR #22975?', ts: ts(6) },
  { role: 'assistant', content: 'Both lanes are green, waiting on review.', ts: ts(7) },
  { role: 'user', content: 'Thanks, ping me if anything comes up.', ts: ts(8) },
  { role: 'assistant', content: 'Will do.', ts: ts(9) },
]
const LAST_SEEN_TS = ts(9)
const NEW = [
  { role: 'assistant', content: 'One nightly failure: `crewmate-chat-clean`. The log says flaky, not a regression.', ts: ts(20) },
  { role: 'assistant', content: 'Re-ran it once, green.', ts: ts(21) },
  { role: 'assistant', content: 'Fixed a dead link in the README while at it, PR #23201 is open.', ts: ts(22) },
  { role: 'assistant', content: 'Issue #22953 sat untriaged past 24h, so I flagged it as triage overdue, per the rule.', ts: ts(23) },
  { role: 'assistant', content: 'Resolved the merge conflict on #19905, CI is re-running.', ts: ts(25) },
  { role: 'assistant', content: 'CI is green there; one CHANGES_REQUESTED review is still standing.', ts: ts(26) },
  { role: 'assistant', content: '`apps-discover-enable-notes` failed in the nightly too, and this one is real: #23188 changed the app card testid.', ts: ts(28) },
  { role: 'assistant', content: 'Left a review comment on #23188 and opened #23210 to track it.', ts: ts(29) },
  { role: 'assistant', content: 'The captain triaged #23210 as T2. I took it.', ts: ts(30) },
  { role: 'assistant', content: 'Fix PR #23212 is open, both lanes running.', ts: ts(31) },
  { role: 'assistant', content: '#23212 is all green, waiting on review.', ts: ts(32) },
  { role: 'assistant', content: 'Review landed on #23212; merged.', ts: ts(34) },
  { role: 'assistant', content: 'Re-ran the nightly on main: all green.', ts: ts(35) },
  { role: 'assistant', content: 'Closed #23210 with the fix linked.', ts: ts(36) },
  { role: 'assistant', content: 'Nothing else today.', ts: ts(37) },
]
const FIRST_NEW = 'One nightly failure'
const NEWEST = 'Nothing else today.'

const DASH = `<!doctype html><html><head><style>
  body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;color:var(--text);background:var(--bg)}
  h1{font-size:18px;margin:0 0 4px} .sub{color:var(--muted);margin:0 0 14px}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
  .card{border:1px solid var(--border);border-radius:8px;padding:10px}
  .n{font-size:22px;font-weight:600} .l{color:var(--muted);font-size:12px}
</style></head><body>
  <h1>radar · today</h1><p class="sub">Refreshed hourly</p>
  <div class="grid">
    <div class="card"><div class="n">9</div><div class="l">Open PRs</div></div>
    <div class="card"><div class="n">1</div><div class="l">Needs you</div></div>
    <div class="card"><div class="n">2</div><div class="l">Nightly failures</div></div>
  </div>
</body></html>`

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

async function openDm(theme, { messages, seen }) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  await routeMembersApi(page, { messages, running: false, has_more: false, total: messages.length, next_before: 0 })
  // The Dashboard tab beside the thread: a small live page, so the frame does
  // not carry an unrelated load error.
  await page.route(u => /^\/api\/members\/[^/]+\/dashboard$/.test(new URL(u).pathname), route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ instance_version: 1, template: { id: 'project-report', version: 2 }, html: DASH, rendered_html: DASH, manifest: { id: 'project-report', version: 2, fields: {} }, state: 'live' }),
  }))
  await page.route(u => new URL(u).pathname === '/api/teams', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"teams":[]}' }))
  if (seen) await page.addInitScript((v) => { localStorage.setItem('vc_seen_member-radar', v) }, seen)
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText(messages[messages.length - 1].content.slice(0, 20)).waitFor()
  // Two frames: the virtualizer's post-entry pin and the landing's settle frame.
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
  return page
}
const divider = (page) => page.locator('[data-unread-divider]')
const pill = (page) => page.getByTestId('jump-to-bottom')
const scroller = (page) => page.locator('.chat-container')
const topOf = (loc) => loc.evaluate(el => el.getBoundingClientRect().top)
const inView = (page, text) => page.getByText(text, { exact: false }).first().evaluate((el) => {
  const r = el.getBoundingClientRect()
  return r.bottom > 0 && r.top < window.innerHeight
})

for (const theme of ['dark', 'light']) {
  // 01 landing: the previous visit saw up to "Will do."; twelve messages followed.
  {
    const page = await openDm(theme, { messages: [...SEEN, ...NEW], seen: LAST_SEEN_TS })
    const n = await divider(page).count()
    check(`01-${theme} divider drawn`, n === 1, `dividers=${n}`)
    const label = n ? await divider(page).textContent() : ''
    check(`01-${theme} divider count`, label.includes(`${NEW.length} new messages`), `label="${label}"`)
    const pillText = await pill(page).textContent().catch(() => null)
    check(`01-${theme} pill carries the count`, pillText != null && pillText.includes(`${NEW.length} new`), `pill="${pillText}"`)
    const dTop = n ? await topOf(divider(page)) : NaN
    const sTop = await topOf(scroller(page))
    check(`01-${theme} divider landed near the top`, dTop - sTop > 40 && dTop - sTop < 220, `divider ${Math.round(dTop - sTop)}px below the scroller top`)
    check(`01-${theme} first new message in view, newest not`, (await inView(page, FIRST_NEW)) && !(await inView(page, NEWEST)), '')
    await page.screenshot({ path: `${OUT}/01-landing-${theme}.png` })

    // 03 read through: jumping to the bottom hides the pill and keeps the divider.
    await pill(page).click()
    await page.waitForFunction(() => document.querySelector('[data-testid="jump-to-bottom"]') === null)
    await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))
    check(`03-${theme} pill gone at the bottom`, (await pill(page).count()) === 0, '')
    check(`03-${theme} divider stays for the visit`, (await divider(page).count()) === 1, '')
    check(`03-${theme} marker moved to the newest message`, (await page.evaluate(() => localStorage.getItem('vc_seen_member-radar'))) === ts(37), '')
    await page.screenshot({ path: `${OUT}/03-read-through-${theme}.png` })

    // 04 back up: the run is read, so the pill is the plain arrow; the divider
    // stays as the landmark of where this visit started.
    await scroller(page).hover()
    await page.mouse.wheel(0, -600)
    await page.waitForFunction(() => document.querySelector('[data-testid="jump-to-bottom"]') !== null)
    await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))
    const backText = await pill(page).textContent()
    check(`04-${theme} pill is the plain arrow after reading through`, backText === '', `pill="${backText}"`)
    check(`04-${theme} divider still marks the run`, (await divider(page).count()) === 1 && (await inView(page, 'new messages')), '')
    await page.screenshot({ path: `${OUT}/04-back-up-${theme}.png` })
    await page.close()
  }
  // 02 caught up: the marker IS the newest row, so nothing changes.
  {
    const page = await openDm(theme, { messages: [...SEEN, ...NEW], seen: ts(37) })
    check(`02-${theme} no divider`, (await divider(page).count()) === 0, '')
    check(`02-${theme} no pill (at the bottom)`, (await pill(page).count()) === 0, '')
    check(`02-${theme} newest in view`, await inView(page, NEWEST), '')
    await page.screenshot({ path: `${OUT}/02-caught-up-${theme}.png` })
    await page.close()
  }
}

await browser.close()
await vite.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
