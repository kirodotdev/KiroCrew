/**
 * Screenshot runner for capture/steer-requeue-superseded.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort
 *   node scripts/capture-steer-requeue-superseded.mjs http://127.0.0.1:6841 <outdir>
 *
 * Asserts the user-bubble count per episode (1, 2, 1) and that the fix episode
 * draws its one bubble AFTER the Stop card, then shoots each theme.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/steer-requeue-superseded'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0
for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({ viewport: { width: 940, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/steer-requeue-superseded.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.locator('[data-episode="fix"] [data-testid="stop-event-card"]').waitFor({ timeout: 10000 })
    const want = { stopped: 1, main: 2, fix: 1 }
    for (const [ep, n] of Object.entries(want)) {
      const got = await page.locator(`[data-episode="${ep}"] [data-role="user"]`).count()
      if (got !== n) throw new Error(`${ep}: expected ${n} user bubbles, found ${got}`)
    }
    const after = await page.evaluate(() => {
      const ep = document.querySelector('[data-episode="fix"]')
      const stop = ep.querySelector('[data-testid="stop-event-card"]')
      const user = ep.querySelector('[data-role="user"]')
      return !!(stop.compareDocumentPosition(user) & Node.DOCUMENT_POSITION_FOLLOWING)
    })
    if (!after) throw new Error('fix episode: the bubble is not after the Stop card')
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/steer-requeue-superseded-${theme}.png` })
    console.log(`${theme}: OK`)
  } catch (e) {
    console.error(`${theme}: FAILED ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}
await browser.close()
process.exit(failed ? 1 : 0)
