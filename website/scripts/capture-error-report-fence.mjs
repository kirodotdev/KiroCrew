/**
 * Screenshots of a ```error-report fence in a user chat bubble — before vs after.
 *
 * Drives the ISOLATED capture entry (website/capture/error-report-fence.html),
 * which mounts the real UserMessage over the prompt the real error->agent
 * builder produces, so the frame is the production render with no gateway.
 *
 * The run is SELF-CHECKING: it reads every `.code-block` header label in the
 * bubble and FAILS unless they match the `expect` argument —
 *   broken -> ['error', 'code']   (misparsed fence + phantom empty block)
 *   fixed  -> ['error-report']    (one block, full tag)
 * so a "before" shot cannot come from a fixed checkout and vice versa; a
 * mislabelled pair exits non-zero instead of emitting a misleading image.
 *
 * Usage (one shell for the server, one per checkout for the shot):
 *   npx vite --host 127.0.0.1 --port 5611 --strictPort
 *   node scripts/capture-error-report-fence.mjs http://127.0.0.1:5611 \
 *     ../temp-screenshots/error-report-fence after fixed
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:5611'
const OUT = process.argv[3] || '../temp-screenshots/error-report-fence'
const LABEL = process.argv[4] || 'after'
const EXPECT = process.argv[5] || 'fixed'
mkdirSync(OUT, { recursive: true })

const WANT = { broken: ['error', 'code'], fixed: ['error-report'] }[EXPECT]
if (!WANT) {
  console.error(`expect must be 'broken' or 'fixed', got ${EXPECT}`)
  process.exit(2)
}

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 760, height: 620 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(`${BASE}/capture/error-report-fence.html?theme=${theme}`, { waitUntil: 'networkidle' })
    try {
      await page.waitForFunction(
        () => document.querySelectorAll('[data-capture-root] .message-bubble .code-block').length >= 1,
        { timeout: 15000 },
      )
    } catch {
      console.error(`[${theme}] no code block rendered in the bubble; page errors: ${errors.join(' | ') || 'none'}`)
      failed++
      await ctx.close()
      continue
    }
    // Let the staged highlighter settle so the frame shows the final surface.
    await page.waitForTimeout(600)
    const labels = await page.evaluate(() =>
      Array.from(document.querySelectorAll('[data-capture-root] .message-bubble .code-block')).map(
        (b) => b.querySelector('span')?.textContent ?? '',
      ),
    )
    const ok = JSON.stringify(labels) === JSON.stringify(WANT)
    if (!ok) {
      console.error(`[${theme}] expected labels ${JSON.stringify(WANT)} (${EXPECT}), got ${JSON.stringify(labels)}`)
      failed++
    }
    if (errors.length) {
      console.error(`[${theme}] page errors: ${errors.join(' | ')}`)
      failed++
    }
    const path = `${OUT}/${LABEL}-${theme}.png`
    await page.locator('[data-capture-root]').screenshot({ path })
    console.log(`captured ${path} labels=${JSON.stringify(labels)}`)
    await ctx.close()
  }
  await browser.close()
  if (failed) {
    console.error(`FAILED: ${failed} check(s) did not hold — do not use these frames as evidence`)
    process.exit(1)
  }
  console.log(`done -> ${OUT}`)
}

run().catch((e) => {
  console.error(e)
  process.exit(1)
})
