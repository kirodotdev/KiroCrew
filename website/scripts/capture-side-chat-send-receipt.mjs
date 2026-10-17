/**
 * Screenshots for SideChat's refused-send strip (chat-core P2).
 *
 * Drives website/capture/side-chat-send-receipt.html: the REAL SideChat with
 * fetch stubbed so `/side/open` succeeds and `/side/turn` answers 409. Types a
 * question, sends, and captures the failure strip. Asserts the strip renders
 * through the shared ErrorNotice (role=alert) with the framed reason and that
 * the question was handed back to the composer.
 *
 * A second pass (`?refuse=late`) lets `/side/turn` hang past the transport
 * deadline (10s): the bubble rolls back, the question comes back, and a
 * standing role=status notice says delivery is unconfirmed. Asserted the same
 * way: notice text, and the composer holding the question again.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-side-chat-send-receipt.mjs http://127.0.0.1:6813 ../temp-screenshots/side-chat-send-receipt
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/side-chat-send-receipt'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 560 } })
let failures = 0
const SENT = 'Why did the last deploy roll back?'

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/side-chat-send-receipt.html?theme=${theme}&lang=en`, { waitUntil: 'networkidle' })
  const box = page.getByLabel('Ask a side question')
  await box.waitFor()
  await box.fill(SENT)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await page.getByRole('alert').waitFor({ timeout: 5000 }).catch(() => {})
  const alert = await page.getByRole('alert').textContent().catch(() => '')
  const composer = await box.inputValue()
  console.log(`${theme}: alert="${alert?.trim()}" composer="${composer}"`)
  if (!/^Couldn't send this message: side turn already in flight\. Your text is back in the composer\./.test(alert?.trim() ?? '')) { console.error(`FAIL: ${theme} alert text`); failures++ }
  if (composer !== SENT) { console.error(`FAIL: ${theme} composer should hold the sent text back`); failures++ }
  await page.screenshot({ path: `${OUT}/refused-${theme}.png` })
}

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/side-chat-send-receipt.html?theme=${theme}&lang=en&refuse=late`, { waitUntil: 'networkidle' })
  const box = page.getByLabel('Ask a side question')
  await box.waitFor()
  await box.fill(SENT)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  // The deadline is the real 10s transport abort; the notice lands right after.
  await page.getByRole('status').filter({ hasText: 'Delivery not confirmed' }).waitFor({ timeout: 15000 }).catch(() => {})
  const notice = await page.getByRole('status').filter({ hasText: 'Delivery not confirmed' }).textContent().catch(() => '')
  const composer = await box.inputValue()
  console.log(`${theme}/late: notice="${notice?.trim()}" composer="${composer}"`)
  if (!/^Delivery not confirmed/.test(notice?.trim() ?? '')) { console.error(`FAIL: ${theme}/late notice text`); failures++ }
  if (composer !== SENT) { console.error(`FAIL: ${theme}/late composer should hold the question back`); failures++ }
  await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished)))
  await page.screenshot({ path: `${OUT}/late-${theme}.png` })
}

// The over-limit hint: a question past MAX_QUESTION_BYTES is not sent and the
// panel says how far to cut. Validation, not a failed operation, so it is a
// `role=status` line at body weight (errors-use-error-notice), never a red
// alert. Client-side only -- nothing is fetched.
for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/side-chat-send-receipt.html?theme=${theme}&lang=en`, { waitUntil: 'networkidle' })
  const box = page.getByLabel('Ask a side question')
  await box.waitFor()
  await box.fill('x'.repeat(33_000))
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  const hint = page.getByRole('status').filter({ hasText: /Question too long/ })
  await hint.waitFor({ timeout: 5000 }).catch(() => {})
  const text = ((await hint.textContent().catch(() => '')) ?? '').trim()
  const alerts = await page.getByRole('alert').count()
  console.log(`${theme}/overlimit: status="${text}" alerts=${alerts}`)
  if (!/^Question too long/.test(text)) { console.error(`FAIL: ${theme}/overlimit hint text`); failures++ }
  if (alerts !== 0) { console.error(`FAIL: ${theme}/overlimit rendered as an alert`); failures++ }
  await page.evaluate(() => Promise.allSettled(document.getAnimations().map(a => a.finished)))
  await page.screenshot({ path: `${OUT}/overlimit-${theme}.png` })
}

await browser.close()
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
