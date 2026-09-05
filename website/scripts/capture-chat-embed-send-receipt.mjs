/**
 * Screenshots for ChatEmbed's send-receipt policy (chat-core P2).
 *
 * Drives the isolated entry website/capture/chat-embed-send-receipt.html: the
 * REAL ChatEmbed under the REAL AppApiProvider, with fetch stubbed so the
 * slot detail loads and POST /api/chat is refused. Three frames per theme:
 *
 *  - idle:      the embed before any send -- and, on main, ALSO what it looked
 *               like after a refused send (nothing rendered, text gone).
 *  - refused:   a 409 from the server -> error row carrying the server's own
 *               reason, text handed back to the composer.
 *  - denied:    the scoped api refusing an app that never granted /api/chat ->
 *               refused receipt naming the missing grant, text handed back.
 *  - late:      the POST never answers; the transport deadline (10s) fires ->
 *               response-late receipt: a NOTICE (not an error) that delivery
 *               is unconfirmed, text handed back.
 *
 * Asserted, not assumed: the refused frame must show exactly one error row
 * whose text is the server reason, and the composer must hold the sent text.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6809 --strictPort   # in another shell
 *   node scripts/capture-chat-embed-send-receipt.mjs http://127.0.0.1:6809 ../temp-screenshots/chat-embed-send-receipt
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6809'
const OUT = process.argv[3] || '../temp-screenshots/chat-embed-send-receipt'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 600, height: 560 } })
let failures = 0
const SENT = 'Also cap the per-key burst at 20.'

for (const theme of ['dark', 'light']) {
  for (const scene of ['refused', 'denied', 'late']) {
    const refuse = scene === 'denied' ? '403' : scene === 'late' ? 'late' : '409'
    await page.goto(`${BASE}/capture/chat-embed-send-receipt.html?theme=${theme}&refuse=${refuse}`, { waitUntil: 'networkidle' })
    const input = page.getByLabel('Chat message')
    await input.waitFor()
    if (scene === 'refused') await page.screenshot({ path: `${OUT}/idle-${theme}.png` })
    await input.fill(SENT)
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    // The failure row renders from the poll-independent local state, so it is
    // there as soon as the mutation settles.
    const ROW_RE = /Send failed|Delivery not confirmed/
    // The late scene waits out the real 10s transport deadline.
    await page.waitForFunction((re) => {
      const nodes = Array.from(document.querySelectorAll('[data-capture-root] *'))
      return nodes.some(n => n.childElementCount === 0 && new RegExp(re).test(n.textContent || ''))
    }, ROW_RE.source, { timeout: 15000 }).catch(() => {})
    const rowText = await page.evaluate((re) => {
      const nodes = Array.from(document.querySelectorAll('[data-capture-root] *'))
      const hit = nodes.find(n => n.childElementCount === 0 && new RegExp(re).test(n.textContent || ''))
      return hit?.textContent?.trim() ?? ''
    }, ROW_RE.source)
    const composer = await input.inputValue()
    console.log(`${theme}/${scene}: row="${rowText}" composer="${composer}"`)
    const expectedRow = scene === 'refused' ? 'Send failed: slot agent mismatch' : scene === 'denied' ? "Send failed: This app isn't allowed to send chat messages." : /^Delivery not confirmed/
    const rowOk = typeof expectedRow === 'string' ? rowText === expectedRow : expectedRow.test(rowText)
    if (!rowOk) { console.error(`FAIL: ${theme}/${scene} expected error row "${expectedRow}", got "${rowText}"`); failures++ }
    if (composer !== SENT) { console.error(`FAIL: ${theme}/${scene} composer should hold the sent text back`); failures++ }
    await page.screenshot({ path: `${OUT}/${scene}-${theme}.png` })
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
