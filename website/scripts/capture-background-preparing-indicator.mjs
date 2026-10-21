/**
 * Screenshots: a crewmate chat shows its normal working indicator while the
 * gateway prepares a message outside any turn (the warm task brief's opener is
 * posted, then a background model call runs for up to a minute).
 *
 * Scene: radar's thread holds the user's request and the brief's fixed opener.
 * No turn runs (`running: false`); the slots frame says `preparing: true`.
 *
 *   01-preparing-<expect>.png  while preparing
 *   02-landed-<expect>.png     after the reply lands and the frame clears it
 *
 * `--expect=before` asserts the old behaviour (no indicator while preparing),
 * `after` the fix (the typing bubble with its "Working" caption).
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6834 --strictPort
 *   node scripts/capture-background-preparing-indicator.mjs http://127.0.0.1:6834 ../temp-screenshots/bg-preparing --expect=after
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6834'
const OUT = positional[1] || '../temp-screenshots/bg-preparing'
const EXPECT = (process.argv.find(a => a.startsWith('--expect=')) || '--expect=after').slice('--expect='.length)
mkdirSync(OUT, { recursive: true })

const SLOT = 'member-radar'
const THREAD = [
  { role: 'user', content: '帮我把三个安全修复的 PR 推到全绿。', ts: '2026-10-21T08:00:00Z' },
  { role: 'assistant', content: '欢迎回来，我跟你说说现在进展到哪了。', ts: '2026-10-21T10:00:00Z', meta: { kind: 'warm_task_brief_preparing' } },
]
const REPLY = '你让我把三个安全修复推到全绿。现在两个已经全绿，第三个还在等 Coverage Gate。你可以先看一下已经全绿的两个，或者让我继续盯第三个。'

let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

const fire = (page, detail) => page.evaluate((d) => window.dispatchEvent(new CustomEvent('capture:frame', { detail: d })), detail)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 1 })
await routeMembersApi(page, { key: SLOT, title: 'radar', running: false, messages: THREAD })
await page.goto(`${BASE}/capture/members-page.html?theme=dark`)
await page.waitForSelector('[data-capture-root]')
await page.getByText('欢迎回来，我跟你说说现在进展到哪了。').first().waitFor()

await fire(page, { kind: 'preparing', slot: SLOT })
await page.waitForTimeout(900)
const shown = await page.getByTestId('crewmate-live-activity').count()
check(`01-preparing (${EXPECT})`, EXPECT === 'before' ? shown === 0 : shown === 1, `indicator=${shown}`)
await page.screenshot({ path: `${OUT}/01-preparing-${EXPECT}.png` })

await fire(page, { kind: 'landed', slot: SLOT, text: REPLY })
await page.getByText(REPLY).first().waitFor()
await page.waitForTimeout(600)
const after = await page.getByTestId('crewmate-live-activity').count()
check(`02-landed (${EXPECT})`, after === 0, `indicator=${after}`)
await page.screenshot({ path: `${OUT}/02-landed-${EXPECT}.png` })

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
