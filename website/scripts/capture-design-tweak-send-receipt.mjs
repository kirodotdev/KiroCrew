/**
 * Screenshots for Design Tweak's refused dispatch (chat-core P2).
 *
 * Drives website/capture/design-tweak-send-receipt.html: the REAL page over the
 * real `api.ts` -> `sendChatMessage` -> chat-core `sendTurn` path, with fetch
 * stubbed to serve one project and one draft request, seal it on `/send`, adopt
 * the app's slot, and then refuse the turn (409 "slot agent mismatch"). One
 * frame per theme: the rail's Send pressed, the refusal rendered through the
 * page's `bridgeError` ErrorNotice as `Send failed: <server reason>`.
 *
 * Asserted, not assumed: the alert text, and that the request is still
 * actionable in the rail afterwards (a refused dispatch must not be marked
 * delivered -- that was the bug).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-design-tweak-send-receipt.mjs http://127.0.0.1:6831 ../temp-screenshots/design-tweak-send-receipt
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/design-tweak-send-receipt'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1100, height: 640 } })
let failures = 0

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/design-tweak-send-receipt.html?theme=${theme}`, { waitUntil: 'load' })
  const send = page.getByRole('button', { name: 'Send as Request 7' })
  await send.waitFor({ timeout: 10000 })
  await send.click()
  const alert = page.getByRole('alert').filter({ hasText: /Send failed/ })
  await alert.waitFor({ timeout: 10000 }).catch(() => {})
  const text = ((await alert.textContent().catch(() => '')) ?? '').trim()
  // Still actionable: a refused dispatch is not delivered, so the rail keeps a
  // Send control for the (now sealed) request.
  const resend = await page.getByRole('button', { name: /Send (as )?Request 7/ }).count()
  console.log(`${theme}: alert="${text}" sendControls=${resend}`)
  if (!/^Send failed: slot agent mismatch/.test(text)) { console.error(`FAIL: ${theme} alert text`); failures++ }
  if (resend < 1) { console.error(`FAIL: ${theme} the request lost its Send control after a refusal`); failures++ }
  await page.evaluate(() => Promise.allSettled(document.getAnimations().map(a => a.finished)))
  await page.screenshot({ path: `${OUT}/refused-${theme}.png` })
}

await browser.close()
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
