/**
 * Frame of the chat tool group's refused-approval notice (#16389): a group
 * parked on one pending tool approval, Approve pressed, and the server answers
 * that the approval is gone (404). The group then shows the shared sentence
 * every approval surface uses for that refusal, through ErrorNotice.
 *
 * Drives the isolated capture entry (website/capture/tool-group-approval-refused.html),
 * which mounts the REAL CollapsibleToolGroup. The frame asserts before it is
 * written that the notice is on screen and reads that sentence.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-tool-group-approval-refused.mjs http://127.0.0.1:6822 ../temp-screenshots/notification-approval-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const GONE = 'This approval has expired or was already decided'
const browser = await chromium.launch()
const name = '25-chat-tool-group-404'
const page = await browser.newPage({ viewport: { width: 760, height: 320 }, deviceScaleFactor: 2 })
await page.route(url => url.pathname.startsWith('/api/'), route =>
  route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))
await page.goto(`${BASE}/capture/tool-group-approval-refused.html?theme=dark`)
await page.waitForSelector('[data-capture-root]')
await page.getByRole('button', { name: /^Approve/ }).first().click()
const notice = page.getByRole('alert')
await notice.waitFor()
await page.waitForTimeout(300)
const text = await notice.innerText()
const ok = text.includes(GONE)
console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify({ text })}`)
if (ok) await page.screenshot({ path: `${OUT}/${name}.png` })
await browser.close()
process.exit(ok ? 0 : 1)
