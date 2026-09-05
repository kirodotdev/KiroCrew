/**
 * Screenshots for SideChat's refused-send strip (chat-core P2).
 *
 * Drives website/capture/side-chat-send-receipt.html: the REAL SideChat with
 * fetch stubbed so `/side/open` succeeds and `/side/turn` answers 409. Types a
 * question, sends, and captures the failure strip. Asserts the strip renders
 * through the shared ErrorNotice (role=alert) with the framed reason and that
 * the question was handed back to the composer.
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
  if (!/^Send failed: side turn already in flight/.test(alert?.trim() ?? '')) { console.error(`FAIL: ${theme} alert text`); failures++ }
  if (composer !== SENT) { console.error(`FAIL: ${theme} composer should hold the sent text back`); failures++ }
  await page.screenshot({ path: `${OUT}/refused-${theme}.png` })
}

await browser.close()
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
