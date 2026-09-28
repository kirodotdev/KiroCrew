/**
 * Screenshot + measurement runner for capture/tool-status-row-align.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-tool-status-row-align.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/tool-status-row-align
 *
 * The frames are evidence, but the ASSERTIONS are the point: `after` must put
 * the shell-activity line and the wait countdown on the pill label's own left
 * edge, and `before` must show them 8px left of it. A run that photographs the
 * wrong state exits nonzero instead of emitting a misleading image.
 *
 * 900x300 at deviceScaleFactor 2 keeps each frame under 2000px per edge.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/tool-status-row-align'

/** The pre-fix `ml-3` (12px) against the label edge at 20px: 8px short. */
const PRE_FIX_DELTA = -8

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const scene of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 300 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${scene}.png`
    try {
      await page.goto(`${BASE}/capture/tool-status-row-align.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector('[data-testid="shell-activity"]', { timeout: 10000 })
      await page.waitForSelector('[data-testid="wait-countdown"]', { timeout: 10000 })

      // One axis only: where each status line's CONTENT starts, relative to
      // the pill label's MEASURED text edge. The reference is read off the
      // rendered label itself, never hand-computed from padding arithmetic —
      // a constant derived from the same arithmetic as the fix would
      // self-confirm a mis-measured edge. 0 means flush with the label above;
      // anything else is the misalignment the reader sees.
      const measured = await page.evaluate(() => {
        const label = document.querySelector('[data-row="shell tool"] button span:not(.sr-only)')
        const labelEdge = label.getBoundingClientRect().x
        const at = el => Math.round(el.getBoundingClientRect().x - labelEdge)
        return [
          { row: 'shell activity line', dx: at(document.querySelector('[data-testid="shell-activity"]')) },
          { row: 'wait countdown line', dx: at(document.querySelector('[data-testid="wait-countdown"]')) },
        ]
      })

      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

      let frameFailed = 0
      for (const r of measured) {
        const want = scene === 'before' ? PRE_FIX_DELTA : 0
        if (r.dx !== want) {
          frameFailed++
          console.error(`FAIL ${name}: "${r.row}" starts at ${r.dx}px from the label edge, expected ${want}px`)
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
