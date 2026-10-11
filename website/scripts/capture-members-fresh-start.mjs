/**
 * Screenshots for Fresh start in a crewmate DM on the Members page.
 *
 * Drives the REAL MembersPage capture entry (capture/members-page.html) with
 * the shared gateway-free API stub. radar's thread holds an older
 * conversation and reads idle (the `idle` capture frame); the fresh-start
 * route answers a reset time after all of it. Every frame asserts its state
 * before it is written, at 1280px and 320px: the header has no button and
 * the quiet link sits under the crewmate's name in the Profile card's head.
 * A last frame reloads the page from a roster whose row already carries
 * `reset_at` (what the member event log folds a cleared reset into) and
 * asserts the fold is drawn from that alone, with no fresh-start call in
 * the session.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6835 --strictPort
 *   node scripts/capture-members-fresh-start.mjs http://127.0.0.1:6835 <outdir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { MEMBERS, routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6835'
const OUT = positional[1] || '../temp-screenshots/members-fresh-start'
mkdirSync(OUT, { recursive: true })

const ts = (i) => new Date(Date.UTC(2026, 9, 10, 5, 0, 0) + i * 60_000).toISOString()
const RESET_AT = new Date(Date.UTC(2026, 9, 10, 6, 0, 0)).toISOString()
const MESSAGES = [
  { role: 'user', content: 'Take the scroll bug.', cls: '', ts: ts(0) },
  { role: 'assistant', content: 'Taken. Reading the pane code first.', cls: '', ts: ts(1) },
  { role: 'user', content: 'Any ETA?', cls: '', ts: ts(2) },
  { role: 'assistant', content: 'A first fix within the hour.', cls: '', ts: ts(3) },
]
const OLD_TEXT = 'A first fix within the hour.'
// An idle crewmate: the stop step has nothing to stop in these frames.
const IDLE = MEMBERS.map(m => ({ ...m, running: false }))

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

async function openDm(theme, width, lang = 'en') {
  const page = await browser.newPage({ viewport: { width, height: 760 }, deviceScaleFactor: 1 })
  await page.addInitScript((lg) => localStorage.setItem('mc-lang', lg), lang)
  await routeMembersApi(page, { messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length }, { members: IDLE })
  await page.route(u => new URL(u).pathname === '/api/teams', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"teams":[]}' }))
  await page.route(u => new URL(u).pathname === '/api/members/radar/fresh-start', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ slot: 'member-radar', reset_at: RESET_AT, outcome: 'cleared' }),
  }))
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}&route=${encodeURIComponent('/members?member=radar')}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText(OLD_TEXT).waitFor()
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('capture:frame', { detail: { kind: 'idle', slot: 'member-radar' } })))
  await page.waitForTimeout(600)
  return page
}

/** Open the Profile card; return the Fresh start link under the crewmate's
 *  name. It lives in the card's head, so it is there on every tab -- no need
 *  to switch to Profile first. */
async function openCardRow(page) {
  await page.getByTestId('member-identity-pill').click()
  const card = page.getByTestId('crew-profile-panel')
  await card.waitFor()
  const row = page.getByTestId('crew-profile-fresh-start')
  await row.waitFor()
  await row.scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  return row
}

/** One full press on a viewport: header, card row, confirm, fold, open. */
async function run(theme, width, tag) {
  const page = await openDm(theme, width)
  check(`${tag}: header has no Fresh start`, (await page.getByTestId('member-fresh-start').count()) === 0
    && !(await page.getByTestId('member-thread-header').innerText()).includes('Fresh start'))
  check(`${tag}: crewmate idle`, !(await page.getByTestId('member-pill-activity').innerText()).includes('Working'))
  await page.screenshot({ path: `${OUT}/${tag}-01-header.png` })
  const row = await openCardRow(page)
  const text = (await row.innerText()).replace(/\s+/g, ' ')
  const underName = await page.evaluate(() => {
    const name = document.querySelector('[data-testid="crew-profile-name"]')
    const link = document.querySelector('[data-testid="crew-profile-fresh-start"]')
    return !!(name && link && name.parentElement.contains(link))
  })
  check(`${tag}: quiet link under the crewmate's name`, text.includes('Fresh start…') && underName, JSON.stringify(text))
  await page.screenshot({ path: `${OUT}/${tag}-02-card.png` })
  await row.click()
  const dialog = page.getByRole('dialog').filter({ hasText: 'Give this crewmate a fresh start?' })
  await dialog.waitFor()
  await page.waitForTimeout(400)
  check(`${tag}: themed confirm names what stays`, (await dialog.innerText()).includes('messages stay here'))
  await page.screenshot({ path: `${OUT}/${tag}-03-confirm.png` })
  await dialog.getByRole('button', { name: 'Start fresh' }).click()
  await page.getByTestId('earlier-conversation').waitFor()
  await page.waitForTimeout(500)
  const toggle = page.getByTestId('earlier-conversation-toggle')
  const closed = await toggle.innerText()
  check(`${tag}: card closed, rows folded with time and count`, (await page.getByTestId('crew-profile-panel').count()) === 0
    && (await page.getByText(OLD_TEXT).count()) === 0 && closed.includes('Show earlier messages (4)'), JSON.stringify(closed))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: `${OUT}/${tag}-04-folded.png` })
  await toggle.click()
  await page.getByText(OLD_TEXT).waitFor()
  await page.waitForTimeout(300)
  check(`${tag}: opened, says hide`, (await toggle.innerText()).includes('Hide earlier messages'))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: `${OUT}/${tag}-05-opened.png` })
  await page.close()
}

await run('dark', 1280, 'desktop-dark')
await run('light', 1280, 'desktop-light')
await run('dark', 320, 'narrow-dark')

// A refusal: queued messages. The notice says to wait, and nothing folds.
{
  const page = await openDm('dark', 1280)
  await page.route(u => new URL(u).pathname === '/api/members/radar/fresh-start', route => route.fulfill({
    status: 409, contentType: 'application/json', body: JSON.stringify({ error: 'queued messages pending', code: 'slot_queue_pending' }),
  }))
  await (await openCardRow(page)).click()
  const dialog = page.getByRole('dialog').filter({ hasText: 'Give this crewmate a fresh start?' })
  await dialog.waitFor()
  await dialog.getByRole('button', { name: 'Start fresh' }).click()
  const notice = page.getByTestId('member-fresh-start-error')
  await notice.waitFor()
  await page.waitForTimeout(400)
  check('refused: notice says to wait, nothing folded', /still queued/i.test(await notice.innerText())
    && (await page.getByTestId('earlier-conversation').count()) === 0)
  await page.screenshot({ path: `${OUT}/desktop-dark-06-refused.png` })
  await page.close()
}

// A fresh LOAD, no press this session: the roster row already carries
// `reset_at` (what a prior Fresh start folded into the member event log), and
// the pane must fold from that alone -- proving the boundary survives a
// reload instead of living only in the page's React state.
{
  const RESET_ROSTER = MEMBERS.map(m => (m.slug === 'radar' ? { ...m, reset_at: RESET_AT } : m))
  const page = await browser.newPage({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 })
  await routeMembersApi(page, { messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length }, { members: RESET_ROSTER })
  await page.route(u => new URL(u).pathname === '/api/teams', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"teams":[]}' }))
  await page.goto(`${BASE}/capture/members-page.html?theme=dark&route=${encodeURIComponent('/members?member=radar')}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByTestId('earlier-conversation').waitFor()
  await page.waitForTimeout(500)
  const toggle = page.getByTestId('earlier-conversation-toggle')
  const closed = await toggle.innerText()
  check('reload: the roster\'s reset_at alone folds the pane', (await page.getByText(OLD_TEXT).count()) === 0
    && closed.includes('Show earlier messages (4)'), JSON.stringify(closed))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: `${OUT}/desktop-dark-07-reload-persists.png` })
  await page.close()
}
await browser.close()
if (failed) process.exit(1)
console.log('all frames verified')
