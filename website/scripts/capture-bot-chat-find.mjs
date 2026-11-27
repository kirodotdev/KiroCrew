/**
 * Screenshots for Find in chat in a Bot's chat (#39585): ⌘F / Ctrl+F on the
 * Bots page opens the find bar over the Bot's transcript, counts matches in
 * every loaded message, and scrolls to one that is far above the window.
 *
 * Drives the REAL MembersPage capture entry (capture/members-page.html) over
 * the worktree's own Vite server, with every /api call answered from fixtures.
 * Every frame asserts its state before it is written.
 *
 * Frames (dark + light):
 *   01-open      Ctrl+F at the bottom of a 40-turn chat: the bar is open
 *   02-found     a word from the first prompt: "1 of 1", that prompt in view
 *   03-no-match  a word no message has: "No results"
 *
 * Usage (from website/):
 *   node scripts/capture-bot-chat-find.mjs [<outdir>]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/bot-chat-find'
mkdirSync(OUT, { recursive: true })

const vite = await createServer({ root: process.cwd(), server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' })
await vite.listen()
const BASE = vite.resolvedUrls.local[0].replace(/\/$/, '')

const ts = (i) => new Date(Date.UTC(2026, 10, 20, 9, 0, 0) + i * 3 * 60_000).toISOString()
const FIRST = 'Please watch the nightly for the quarantined shard and tell me when it flips.'
const MESSAGES = Array.from({ length: 80 }, (_, i) => (i % 2 === 0
  ? { role: 'user', content: i === 0 ? FIRST : `Status of item ${i / 2}?`, ts: ts(i) }
  : { role: 'assistant', content: `Item ${(i - 1) / 2} is on track; nothing needs you yet.`, ts: ts(i) }))

// The Bot's chat column only: the side panel beside it is not part of this change.
const CHAT_COLUMN = { x: 0, y: 0, width: 640, height: 820 }
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}
const inView = (page, text) => page.getByText(text, { exact: false }).first().evaluate((el) => {
  const r = el.getBoundingClientRect()
  return r.bottom > 0 && r.top < window.innerHeight
}).catch(() => false)

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  await routeMembersApi(page, { messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length, next_before: 0 })
  // The Dashboard tab beside the thread: a small live page, so the frame does
  // not carry an unrelated load error.
  const DASH = '<!doctype html><html><body style="font:14px system-ui;padding:16px;color:var(--text)"><h1 style="font-size:18px">radar · today</h1><p>2 open items</p></body></html>'
  await page.route(u => /^\/api\/members\/[^/]+\/dashboard$/.test(new URL(u).pathname), route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ instance_version: 1, template: { id: 'project-report', version: 2 }, html: DASH, rendered_html: DASH, manifest: { id: 'project-report', version: 2, fields: {} }, state: 'live' }),
  }))
  await page.route(u => new URL(u).pathname === '/api/teams', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"teams":[]}' }))
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText(MESSAGES[79].content).waitFor()
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
  check(`00-${theme} first prompt not in the page before the find`, (await page.getByText(FIRST).count()) === 0)

  await page.keyboard.press('Control+f')
  const input = page.getByRole('combobox', { name: 'Find in chat' })
  await input.waitFor()
  check(`01-${theme} find bar open`, await input.isVisible())
  await page.screenshot({ path: `${OUT}/01-open-${theme}.png`, clip: CHAT_COLUMN })

  await input.fill('quarantined')
  await page.getByText('1 of 1 result').waitFor({ timeout: 5000 }).catch(() => {})
  check(`02-${theme} count`, (await page.getByText('1 of 1 result').count()) === 1)
  await page.waitForFunction((t) => [...document.querySelectorAll('.chat-container *')].some(el => el.textContent === t), FIRST, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(1200)
  check(`02-${theme} first prompt scrolled into view`, await inView(page, FIRST))
  await page.screenshot({ path: `${OUT}/02-found-${theme}.png`, clip: CHAT_COLUMN })

  await input.fill('zebra')
  await page.getByText('No results').waitFor({ timeout: 5000 }).catch(() => {})
  check(`03-${theme} no-match count`, (await page.getByText('No results').count()) >= 1)
  await page.screenshot({ path: `${OUT}/03-no-match-${theme}.png`, clip: CHAT_COLUMN })
  await page.close()
}

await browser.close()
await vite.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
