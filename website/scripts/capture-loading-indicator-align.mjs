/**
 * Screenshot + measurement runner for capture/loading-indicator-align.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-loading-indicator-align.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/loading-indicator-align
 *
 * The frames are evidence, but the ASSERTIONS are the point: `after` must put the
 * running indicator on the same left edge as the message and tool rows above it,
 * and `before` must show it 14px right of them. A run that photographs the wrong
 * state exits nonzero instead of emitting a misleading image.
 *
 * 900x360 at deviceScaleFactor 2 keeps each frame under 2000px per edge.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/loading-indicator-align'

/** The pre-fix inner `px-3.5`, i.e. how far the indicator used to be pushed. */
const STACKED_INSET = 14

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const scene of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 360 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${scene}.png`
    try {
      await page.goto(`${BASE}/capture/loading-indicator-align.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector('[data-testid="chat-footer"]', { timeout: 10000 })
      // The carousel cross-fades on a 2.8s beat; hold for one slot cascade so the
      // frame catches artwork rather than a mid-fade blank.
      await page.waitForTimeout(900)

      // One axis only: where each row's CONTENT starts, relative to the column's
      // text edge. 0 means flush with every sibling; anything else is the
      // misalignment the reader sees.
      const measured = await page.evaluate(() => {
        const root = document.querySelector('[data-capture-root]')
        const rb = root.getBoundingClientRect()
        const textEdge = rb.x + (rb.width - 800) / 2 + 16
        const at = el => Math.round(el.getBoundingClientRect().x - textEdge)
        const rows = [...document.querySelectorAll('[data-row]')].map(r => ({
          row: r.getAttribute('data-row'),
          dx: at(r.firstElementChild),
        }))
        // The carousel itself, not the footer wrapper: the wrapper's box spans
        // the whole column in both scenes, so only the artwork reveals the inset.
        const carousel = document.querySelector('.csb4')
        rows.push({ row: 'running indicator', dx: carousel ? at(carousel) : NaN })
        return rows
      })

      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

      let frameFailed = 0
      for (const r of measured) {
        const want = scene === 'before' && r.row === 'running indicator' ? STACKED_INSET : 0
        if (r.dx !== want) {
          frameFailed++
          console.error(`FAIL ${name}: "${r.row}" starts at ${r.dx}px, expected ${want}px`)
        }
      }
      if (errors.length) {
        frameFailed++
        console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
      }
      failed += frameFailed
      // Only claim a frame is good when nothing about it failed — an `ok` line
      // beside a FAIL line is how a misleading screenshot gets published.
      if (!frameFailed) {
        console.log(`ok   ${name}\n       ${measured.map(r => `${r.row}=${r.dx}px`).join('  ')}`)
      }
    } catch (err) {
      failed++
      console.error(`FAIL ${name}: ${err.message}`)
    }
    await ctx.close()
  }
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed — the frames do not show the state they claim.`)
  process.exit(1)
}
console.log('\nall scenes match their expected geometry')
