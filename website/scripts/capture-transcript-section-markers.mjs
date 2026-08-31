/**
 * Screenshots of the transcript section-marker row in both themes.
 *
 * Asserts the real row actually mounted before shooting: four
 * `[data-testid="section-marker-row"]` nodes must be present, and the labelled
 * one must carry its label, so a frame cannot silently photograph an empty
 * container if the component failed to render.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6874 --strictPort   # in another shell
 *   node scripts/capture-transcript-section-markers.mjs [base] [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6874'
const OUT = process.argv[3] || '../temp-screenshots/transcript-section-markers'
mkdirSync(OUT, { recursive: true })

const run = async () => {
  const browser = await chromium.launch()
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 900, height: 900 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    await page.goto(`${BASE}/capture/transcript-section-markers.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    await page.waitForSelector('[data-capture-root]')
    const rows = page.locator('[data-testid="section-marker-row"]')
    await rows.first().waitFor({ timeout: 10000 })
    const count = await rows.count()
    if (count !== 4) throw new Error(`expected 4 marker rows, saw ${count}`)
    const label = await rows.first().getAttribute('aria-label')
    if (label !== 'End of: item-42') throw new Error(`expected label item-42, saw ${label}`)
    await page.waitForTimeout(400)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/section-markers-${theme}.png` })
    console.log(`ok  section-markers-${theme}.png  (rows=${count})`)
    await ctx.close()
  }
  await browser.close()
}

run().catch((e) => {
  console.error(e)
  process.exit(1)
})
