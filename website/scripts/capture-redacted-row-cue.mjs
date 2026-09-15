import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/redacted-row-cue'
mkdirSync(OUT, { recursive: true })

const CUE = '[data-testid="user-message-redacted"]'
const ROOT = '[data-capture-root]'

const browser = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: { width: 980, height: 440 }, deviceScaleFactor: 2 })
    await page.goto(`${BASE}/capture/redacted-row-cue.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector(CUE, { timeout: 10000 })
    // Runs the page's own gate: an unresolved i18n bundle or a missing cue throws
    // here rather than being screenshotted as if it were the shipped surface.
    const gate = await page.evaluate(() => window.__captureGate())
    console.log(`${theme}: cue=${JSON.stringify(gate.cueText)} count=${gate.cueCount}`)
    await page.locator(ROOT).screenshot({ path: `${OUT}/${theme}.png` })
    console.log(`wrote ${OUT}/${theme}.png`)
    await page.close()
  }
} finally {
  await browser.close()
}
