/** Real-browser evidence: Quote on a long reply in a Bot chat, on the real
 * Members page (gateway-free). Opens radar's DM, then either selects the
 * whole reply and clicks the selection Quote (`selection`), or clicks the
 * reply's own Quote (`message`). It checks the composer box shows its draft:
 * the box is not scrolled, and the editor and the button row sit inside it.
 * Exits non-zero when a check fails.
 *
 * Usage (in website/):
 *   node scripts/capture-bot-chat-long-quote.mjs <outDir> <label> [selection|message] [dark|light]
 * It starts its own Vite dev server on a free loopback port and stops it.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/bot-chat-long-quote')
const LABEL = process.argv[3] || 'after'
const KIND = process.argv[4] || 'selection'
const THEME = process.argv[5] || 'dark'
mkdirSync(OUT, { recursive: true })

const PARAGRAPH = 'The rollout plan for the Acme billing service has four stages, and each one waits on the one before it. First the schema change ships behind a flag in the staging region, where the nightly suite runs against a copy of last week\'s invoices. Then the backfill writes the invoice table in batches of five thousand rows, pausing whenever replication lag passes ten seconds. After that the flag turns on for five percent of accounts, and the team watches error rates and support tickets for a full day.'
const LONG = ['**Rollout plan**', '', PARAGRAPH, '', '- Staging first.', '- Backfill in batches.', '', PARAGRAPH, '', PARAGRAPH, '', PARAGRAPH, '', PARAGRAPH].join('\n')
const THREAD = [
  { role: 'user', content: 'What is the rollout plan?', ts: '2026-11-25T13:00:00Z' },
  { role: 'assistant', content: LONG, ts: '2026-11-25T13:00:05Z' },
]

const server = await createServer({ server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' })
await server.listen()
const BASE = server.resolvedUrls.local[0].replace(/\/$/, '')
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 })
const errors = []
page.on('pageerror', e => errors.push(String(e)))
await routeMembersApi(page, { key: 'member-radar', title: 'radar', running: false, messages: THREAD })
await page.goto(`${BASE}/capture/members-page.html?theme=${THEME}`)
await page.waitForSelector('[data-capture-root]')
await page.getByText('What is the rollout plan?').waitFor()
const reply = page.locator('[data-chat-pane] .message-bubble', { hasText: 'Rollout plan' }).last()
if (KIND === 'selection') {
  // The reply's top sits above the viewport, so select it in script (first
  // paragraph to last, as a drag would) and release the mouse inside it,
  // which opens the selection popover.
  const selectReply = () => reply.evaluate(el => {
    const ps = el.querySelectorAll('p')
    const range = document.createRange()
    range.setStartBefore(ps[0]); range.setEndAfter(ps[ps.length - 1])
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range)
    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    document.dispatchEvent(new Event('selectionchange'))
  })
  await selectReply()
  const b = await reply.boundingBox()
  await page.mouse.move(b.x + b.width / 2, b.y + b.height - 30)
  await page.mouse.down(); await page.mouse.up()
  await selectReply()
  await page.getByRole('button', { name: 'Quote', exact: true }).first().click()
} else {
  await reply.hover()
  await page.locator('[data-chat-pane] [data-testid="quote-message"]').last().click()
}
await page.getByTestId('composer-bottom-row').first().waitFor()
await page.waitForTimeout(1200)
const m = await page.evaluate(() => {
  const pane = document.querySelector('[data-chat-pane]')
  const box = pane.querySelector('[data-testid="input-wrapper"]')
  const input = pane.querySelector('[data-composer-input]')
  const row = pane.querySelector('[data-testid="composer-bottom-row"]')
  const r = el => { const b = el.getBoundingClientRect(); return { top: Math.round(b.top), bottom: Math.round(b.bottom) } }
  return { boxScrollTop: box.scrollTop, box: r(box), input: r(input), row: r(row), draftChars: (input.value ?? input.textContent).length }
})
let failed = false
const check = (name, ok) => { console.log(`${LABEL} ${KIND}: ${name} => ${ok ? 'OK' : 'MISMATCH'}`); if (!ok) failed = true }
console.log(LABEL, KIND, JSON.stringify(m))
check('the box is not scrolled', m.boxScrollTop === 0)
check('the editor starts inside the box', m.input.top >= m.box.top)
check('the button row sits inside the box', m.row.top >= m.box.top && m.row.bottom <= m.box.bottom)
check('no page errors', errors.length === 0)
await page.screenshot({ path: resolve(OUT, `${LABEL}-${KIND}-${THEME}.png`) })
await browser.close()
await server.close()
process.exit(failed ? 1 : 0)
