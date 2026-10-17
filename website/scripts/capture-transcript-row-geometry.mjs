/**
 * Screenshot + measurement runner for capture/transcript-row-geometry.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort
 *   node scripts/capture-transcript-row-geometry.mjs http://127.0.0.1:6814 \
 *     ../temp-screenshots/transcript-row-geometry
 *
 * The frames are evidence, but the ASSERTIONS are the point: `after` must put
 * every row's content on the column text edge, and `before` must show the four
 * affected cards off it. A run that photographs the wrong state exits nonzero
 * instead of emitting a misleading image.
 *
 * 900px viewport at deviceScaleFactor 2 keeps each frame under 2000px per edge.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/transcript-row-geometry'

/** Rows the fix moves; the two reference rows must never move. */
const AFFECTED = ['workflow_run', 'spawn_run', 'workflow completion', 'subagent completion']

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const scene of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 900 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${scene}.png`
    try {
      await page.goto(`${BASE}/capture/transcript-row-geometry.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector('[data-row="workflow_run"]', { timeout: 10000 })
      // Let the scale-in / reveal transitions settle so the frame is not a blur.
      await page.waitForTimeout(900)

      // Two DIFFERENT axes, and conflating them is what makes a geometry claim
      // meaningless:
      //   dx  = where the card's own BOX starts, relative to the column's text
      //         edge. This is the alignment axis: 0 means flush with every
      //         sibling row, and it is what the fix changes.
      //   pad = the card's own internal padding, i.e. where its text starts
      //         inside its border. A design axis, reported but not asserted.
      const rows = await page.evaluate(() => {
        const root = document.querySelector('[data-capture-root]')
        const rb = root.getBoundingClientRect()
        const textEdge = rb.x + (rb.width - 800) / 2 + 20
        return [...document.querySelectorAll('[data-row]')].map(r => {
          // The card's REAL root, found through the marker rather than by
          // walking first children: in the `before` scene the simulated nesting
          // wrapper is the first child, and measuring it would report the
          // wrapper's offset instead of the card's.
          const el = r.querySelector('[data-card]')?.firstElementChild
          const b = el.getBoundingClientRect()
          return {
            row: r.getAttribute('data-row'),
            dx: Math.round(b.x - textEdge),
            pad: Math.round(parseFloat(getComputedStyle(el).paddingLeft || '0')),
            width: Math.round(b.width),
          }
        })
      })

      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

      let frameFailed = 0
      for (const r of rows) {
        const affected = AFFECTED.includes(r.row)
        const want = scene === 'before' && affected ? 20 : 0
        if (r.dx !== want) {
          frameFailed++
          console.error(`FAIL ${name}: row "${r.row}" box starts at ${r.dx}px, expected ${want}px`)
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
        const summary = rows.map(r => `${r.row}=${r.dx}px(pad ${r.pad})/${r.width}w`).join('  ')
        console.log(`ok   ${name}\n       ${summary}`)
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
