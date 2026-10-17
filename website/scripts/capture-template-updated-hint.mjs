import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/template-updated-hint'
mkdirSync(OUT, { recursive: true })

const NOTE = '[data-testid="schedule-template-updated-notice"]'
const ROOT = '[data-capture-root]'

const browser = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: { width: 720, height: 420 }, deviceScaleFactor: 2 })
    await page.goto(`${BASE}/capture/template-updated-hint.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector(NOTE, { timeout: 10000 })
    const note = await page.textContent(NOTE)
    if (!note || note.trim().length < 20) throw new Error(`empty hint on ${theme}`)
    await page.locator(ROOT).screenshot({ path: `${OUT}/${theme}.png` })
    console.log(`wrote ${OUT}/${theme}.png`)
    await page.close()
  }
} finally {
  await browser.close()
}
