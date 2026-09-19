/**
 * Screenshots for Mochi's send-receipt strip (chat-core P2).
 *
 * Drives website/capture/mochi-send-receipt.html: the REAL ChatPanel over the
 * real mochiApi -> panelBridge -> chat-core sendTurn path, with fetch stubbed so
 * the slot bind succeeds and POST /api/chat either answers 409 or never
 * answers. Two frames per theme:
 *
 *  - refused: the server said no -> ErrorNotice (role=alert) above the composer
 *             framed as a failed send ("Send failed: <server reason>"); the
 *             typed text is back in the composer; no user bubble was painted.
 *  - late:    no receipt inside the transport deadline (10s) -> a role=status
 *             "delivery not confirmed" line (NOT an error), text handed back.
 *
 * Asserted, not assumed: the strip text, the restored composer, and that the
 * transcript holds no bubble for the failed send.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-mochi-send-receipt.mjs http://127.0.0.1:6831 ../temp-screenshots/mochi-send-receipt
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/mochi-send-receipt'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 360, height: 460 }, deviceScaleFactor: 2 })
let failures = 0
const SENT = 'Rename the release branch to 2.4.x'

for (const theme of ['dark', 'light']) {
  for (const scene of ['refused', 'late']) {
    await page.goto(`${BASE}/capture/mochi-send-receipt.html?theme=${theme}&refuse=${scene === 'late' ? 'late' : '409'}`, { waitUntil: 'load' })
    const box = page.locator('[data-capture-root] textarea')
    await box.waitFor()
    await box.fill(SENT)
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    const role = scene === 'refused' ? 'alert' : 'status'
    const strip = page.getByRole(role).filter({ hasText: scene === 'refused' ? /Send failed/ : /Delivery not confirmed/ })
    // The late scene waits out the real 10s transport deadline.
    await strip.waitFor({ timeout: 15000 }).catch(() => {})
    const text = (await strip.textContent().catch(() => '')) ?? ''
    const composer = await box.inputValue()
    const bubbles = await page.evaluate((sent) => Array.from(document.querySelectorAll('[data-capture-root] *')).filter(n => n.childElementCount === 0 && (n.textContent || '').trim() === sent && n.tagName !== 'TEXTAREA').length, SENT)
    console.log(`${theme}/${scene}: ${role}="${text.trim()}" composer="${composer}" bubbles=${bubbles}`)
    const expected = scene === 'refused'
      ? /^Send failed: slot agent mismatch/
      : /^Delivery not confirmed/
    if (!expected.test(text.trim())) { console.error(`FAIL: ${theme}/${scene} strip text`); failures++ }
    if (composer !== SENT) { console.error(`FAIL: ${theme}/${scene} composer should hold the sent text back`); failures++ }
    if (bubbles !== 0) { console.error(`FAIL: ${theme}/${scene} a bubble was painted for a send the server never took`); failures++ }
    // allSettled: a cancelled animation rejects its `finished`, which must not abort the frame.
    await page.evaluate(() => Promise.allSettled(document.getAnimations().map(a => a.finished)))
    await page.screenshot({ path: `${OUT}/${scene}-${theme}.png` })
  }
}

// The two themes must produce two different frames: a harness that applies a
// fixed palette photographs the same picture twice and labels one "light".
for (const scene of ['refused', 'late']) {
  if (readFileSync(`${OUT}/${scene}-dark.png`).equals(readFileSync(`${OUT}/${scene}-light.png`))) {
    console.error(`FAIL: ${scene} light frame is byte-identical to the dark frame -- the theme was not applied`); failures++
  }
}

await browser.close()
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
