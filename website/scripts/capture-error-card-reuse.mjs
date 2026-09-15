/**
 * Screenshot runner for capture/error-card-reuse.html (#6209 evidence).
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6829 --strictPort
 *   node scripts/capture-error-card-reuse.mjs http://127.0.0.1:6829 <outdir>
 *
 * Captures the before/after sheet in both themes, element-scoped to the
 * capture root so frames stay small. Asserts the AFTER episode really is the
 * shared ErrorCard (data-testid present) and the BEFORE one really is not,
 * so a frame cannot photograph the wrong state. Also asserts the two rows'
 * bounding boxes match — the whole point is a pixel-identical no-op.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6829'
const OUT = process.argv[3] || '../temp-screenshots/error-card-reuse'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 780, height: 520 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/error-card-reuse.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    const before = page.locator('[data-episode="before"] > div')
    const after = page.locator('[data-episode="after"] [data-testid="error-card"]')
    await before.waitFor({ timeout: 10000 })
    await after.waitFor({ timeout: 10000 })
    // The BEFORE replica must NOT be the shared component…
    if (await page.locator('[data-episode="before"] [data-testid="error-card"]').count()) {
      throw new Error('BEFORE episode unexpectedly contains the shared ErrorCard')
    }
    // …and the two rows must occupy the same box: pixel-identical by design.
    const [bb, ab] = [await before.boundingBox(), await after.boundingBox()]
    if (!bb || !ab || bb.width !== ab.width || bb.height !== ab.height) {
      throw new Error(`row boxes differ: before=${JSON.stringify(bb)} after=${JSON.stringify(ab)}`)
    }
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({
      path: `${OUT}/error-card-reuse-${theme}.png`,
    })
    console.log(`${theme}: rows ${bb.width}x${bb.height} identical — OK`)
  } catch (e) {
    console.error(`${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
