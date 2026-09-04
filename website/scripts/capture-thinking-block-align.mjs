/**
 * Before/after evidence shots for the thinking-block alignment change.
 * Clicks the REAL ThinkingBlock open before shooting, and asserts the
 * expanded state so a frame cannot silently photograph a collapsed block.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-thinking-block-align.mjs [base] [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/thinking-block-align'
const PREFIX = process.argv[4] || ''
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1040, height: 1200 }, deviceScaleFactor: 1 })
  await page.goto(`${BASE}/capture/thinking-block-align.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root]')
  // Expand the real ThinkingBlock and assert it opened.
  const toggle = page.getByRole('button', { expanded: false }).last()
  await toggle.click()
  await page.waitForSelector('button[aria-expanded="true"]', { timeout: 10000 })
  // Reasoning text must be on screen before the shot.
  await page.getByText('Writing Playwright script...').waitFor({ timeout: 10000 })
  await page.waitForTimeout(700) // let the spring animation settle
  const root = page.locator('[data-capture-root]')
  await root.screenshot({ path: `${OUT}/${PREFIX}${theme}.png` })
  console.log(`wrote ${OUT}/${PREFIX}${theme}.png`)
  await page.close()
}
await browser.close()
