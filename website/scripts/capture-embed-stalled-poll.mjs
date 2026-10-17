/**
 * Screenshots + assertions for a ChatEmbed whose poll keeps failing after a
 * page was shown, on the isolated entry website/capture/embed-stalled-poll.html
 * (the real ChatEmbed, gateway-free).
 *
 * The first slot read answers with a RUNNING turn; every later read is a 503.
 * After the stall window the header must stop reading "streaming" and the
 * connection-lost notice must show; the failing poll must have backed off.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-embed-stalled-poll.mjs http://127.0.0.1:6813 <out-dir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/embed-stalled-poll'
const LABEL = process.argv[4] || 'after'
const ASSERT = LABEL === 'after'
mkdirSync(OUT, { recursive: true })

const PAGE = {
  messages: [
    { role: 'user', content: 'Summarize the last deploy and list any failed steps.', cls: '', ts: '2026-10-13T17:00:00Z' },
    { role: 'streaming', content: 'The deploy ran four steps. Build and upload finished; the', cls: '', ts: '2026-10-13T17:00:04Z' },
  ],
  running: true,
  title: 'Example app · deploy review',
  has_more: false,
}

const browser = await chromium.launch()
try {
  for (const [theme, width] of [['dark', 720], ['light', 720], ['dark', 360], ['light', 320]]) {
    const page = await browser.newPage({ viewport: { width: width + 40, height: 560 } })
    let reads = 0
    let heldRead
    let holdNext = false
    let recovered = false
    await page.route('**/api/chat/slots/**', route => {
      reads++
      if (holdNext) {
        holdNext = false
        heldRead = route
        return
      }
      return reads === 1 || recovered
        ? route.fulfill({ json: PAGE })
        : route.fulfill({ status: 503, json: { error: 'unavailable' } })
    })
    await page.goto(`${BASE}/capture/embed-stalled-poll.html?theme=${theme}&w=${width}`, { waitUntil: 'load' })
    await page.getByText('Example app', { exact: false }).first().waitFor()
    const notice = page.getByTestId('chat-embed-stalled')
    if (ASSERT) await notice.waitFor({ timeout: 45_000 })
    else await page.waitForFunction(() => document.body.textContent.includes('streaming'))
    const reconnect = page.getByRole('button', { name: 'Reconnect' })
    await page.mouse.move(5, 5)
    await page.locator('[data-capture-box]').screenshot({ path: `${OUT}/${LABEL}-${theme}-${width}.png` })
    if (ASSERT) {
      if (await page.getByText('streaming', { exact: true }).count()) throw new Error('stale header still streaming')
      console.log(`[${LABEL} ${theme} ${width}] reads=${reads}, stalled notice visible`)
      holdNext = true
      await reconnect.click()
      await page.waitForFunction(() => document.querySelector('button[aria-busy="true"]:disabled') !== null)
      if (!heldRead) throw new Error('pending reconnect did not issue a read')
      await page.locator('[data-capture-box]').screenshot({ path: `${OUT}/pending-${theme}-${width}.png` })
      await heldRead.fulfill({ status: 503, json: { error: 'unavailable' } })
      await page.waitForFunction(() => document.querySelector('button[aria-busy="false"]:not(:disabled)') !== null)
      await notice.waitFor()
      await page.locator('[data-capture-box]').screenshot({ path: `${OUT}/failed-${theme}-${width}.png` })
      recovered = true
      await reconnect.click()
      await notice.waitFor({ state: 'detached' })
      await page.getByText('streaming', { exact: true }).waitFor()
      await page.locator('[data-capture-box]').screenshot({ path: `${OUT}/recovered-${theme}-${width}.png` })
      console.log(`[${theme} ${width}] pending, failed, recovered verified`)
    }
    await page.close()
  }
} finally {
  await browser.close()
}
console.log('ALL GREEN')
