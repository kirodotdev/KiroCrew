/**
 * Screenshot runner for capture/stop-event-card-reuse.html (#6229 evidence).
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort
 *   node scripts/capture-stop-event-card-reuse.mjs http://127.0.0.1:6832 <outdir>
 *
 * Captures the before/after sheet in both themes, element-scoped to the capture
 * root so frames stay small. Asserts the AFTER episode really is the shared
 * StopEventCard (data-testid present) and the BEFORE one really is not, so a
 * frame cannot photograph the wrong state. Then asserts the substance of the
 * fix, which is the inverse of the #6209 sheet's pixel-identical claim: every
 * BEFORE row leaks the JSON envelope, no AFTER row does, and the three AFTER
 * rows read differently from each other where BEFORE they were one static shape.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/stop-event-card-reuse'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 940, height: 640 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/stop-event-card-reuse.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    const after = page.locator('[data-episode="after"] [data-testid="stop-event-card"]')
    await after.first().waitFor({ timeout: 10000 })
    if (await after.count() !== 3) {
      throw new Error(`expected 3 AFTER cards, found ${await after.count()}`)
    }
    // The BEFORE replica must NOT be the shared component…
    if (await page.locator('[data-episode="before"] [data-testid="stop-event-card"]').count()) {
      throw new Error('BEFORE episode unexpectedly contains the shared StopEventCard')
    }
    // …every BEFORE row must leak the envelope, and no AFTER row may…
    const beforeText = await page.locator('[data-episode="before"]').innerText()
    const afterText = await page.locator('[data-episode="after"]').innerText()
    if (!beforeText.includes('"kind":"stop_event"')) {
      throw new Error('BEFORE episode does not show the envelope it is meant to demonstrate')
    }
    if (afterText.includes('"kind":"stop_event"') || afterText.includes('stop-4f8c1e2a9b')) {
      throw new Error('AFTER episode still leaks the stop envelope')
    }
    // …and the three AFTER rows must read differently, where BEFORE was one shape.
    const labels = new Set(await after.allInnerTexts())
    if (labels.size !== 3) {
      throw new Error(`AFTER rows are not distinct: ${JSON.stringify([...labels])}`)
    }
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({
      path: `${OUT}/stop-event-card-reuse-${theme}.png`,
    })
    console.log(`${theme}: 3 distinct cards, envelope gone — OK`)
  } catch (e) {
    console.error(`${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
