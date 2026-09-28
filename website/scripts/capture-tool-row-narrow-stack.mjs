/**
 * Screenshot + assertion runner for capture/tool-row-narrow-stack.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort
 *   node scripts/capture-tool-row-narrow-stack.mjs http://127.0.0.1:6823 \
 *     ../temp-screenshots/tool-row-narrow-stack
 *
 * What it proves, and why the image alone would not: the pill's label is
 * `min-w-0` shrinkable while the file chip is `shrink-0` with its own label
 * cap, so on a no-wrap row the chip takes its width first and the label lives
 * on what is left. In a 358px column that starved the label into a ten-line
 * ribbon beside a chip carrying the same (truncated) filename. The fix lets the
 * pair wrap, so the assertions are geometric on both arrangements:
 *
 *   wrapped   chip sits BELOW the pill, its left edge on the column's text edge
 *             (the wrapper's -ml-2 must be cancelled, or the chip hangs 8px into
 *             the gutter), and the pill gets essentially the whole column
 *   unwrapped chip sits BESIDE the pill, separated by the chip's own 8px margin
 *
 * The three cases separate the VIEWPORT from the COLUMN on purpose. The defect
 * follows the column — `ChatPane` sets `--mc-content-width: 100%`, so a
 * quarter-width pane in the session grid is a ~350px column at a 1440px viewport
 * — so `pane-400-at-1200` is the case that fails if the wrap is ever re-gated on
 * a viewport breakpoint, which is a thing a phone-width case alone cannot catch.
 * The width assertion is what fails if a stack is achieved by some mechanism
 * that leaves the label starved anyway.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/tool-row-narrow-stack'

mkdirSync(OUT, { recursive: true })

/** The two rows under test: one whose label + chip cannot share a narrow line,
 *  and one whose can. Both matter — the first is the defect, the second is the
 *  regression the naive fix (force every narrow row into a column) introduces. */
const TARGETS = {
  long: 'membership_actions_show_star_skeleton',
  short: 'Reading the turn grouping',
}

const CASES = [
  // A phone.
  { name: 'narrow-390', width: 390, height: 720, colw: '800px', wraps: true },
  // A session-grid pane: narrow COLUMN, wide VIEWPORT. Must wrap exactly like
  // the phone — this is the case a `md:` breakpoint would have missed.
  { name: 'pane-400-at-1200', width: 1200, height: 720, colw: '400px', wraps: true },
  // The main chat column on a desktop: the pair fits, so it must NOT wrap.
  { name: 'wide-900', width: 900, height: 720, colw: '800px', wraps: false },
]

const browser = await chromium.launch()
let failed = 0

for (const c of CASES) {
  const ctx = await browser.newContext({
    viewport: { width: c.width, height: c.height },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  try {
    await page.goto(`${BASE}/capture/tool-row-narrow-stack.html?theme=dark&colw=${encodeURIComponent(c.colw)}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    // The chip only appears after its HEAD probe resolves; wait for the chip
    // itself rather than a fixed sleep, so a fixture that never shows one fails
    // loudly here instead of silently measuring a one-item row.
    await page.waitForSelector('button.font-mono', { timeout: 15000 })
    await page.waitForTimeout(700)

    const m = await page.evaluate((targets) => {
      const root = document.querySelector('[data-capture-root]')
      // The row wrapper owns the gutter; read it from the DOM rather than
      // hardcoding a px-N class, which is how a sibling probe silently went to
      // zero matches after the gutter was renamed.
      const wrappers = [...root.querySelectorAll('[style*="max-width"]')]
      const read = (target) => {
        const wrap = wrappers.find(w => (w.textContent || '').includes(target))
        if (!wrap) throw new Error(`no row wrapper containing "${target}"`)
        const wr = wrap.getBoundingClientRect()
        const cs = getComputedStyle(wrap)
        const textEdge = wr.x + parseFloat(cs.paddingLeft)
        const columnWidth = wr.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)

        const buttons = [...wrap.querySelectorAll('button')]
        const chip = buttons.find(b => b.classList.contains('font-mono'))
        const pill = buttons.find(b => b !== chip && b.hasAttribute('aria-expanded'))
        if (!chip) throw new Error(`no file chip in the "${target}" row`)
        if (!pill) throw new Error(`no pill button in the "${target}" row`)
        const pr = pill.getBoundingClientRect()
        const cr = chip.getBoundingClientRect()
        return {
          textEdge: Math.round(textEdge),
          columnWidth: Math.round(columnWidth),
          pill: { x: Math.round(pr.x), y: Math.round(pr.y), w: Math.round(pr.width), bottom: Math.round(pr.bottom) },
          chip: { x: Math.round(cr.x), y: Math.round(cr.y), w: Math.round(cr.width), top: Math.round(cr.top) },
          labelLines: Math.round(pr.height / 20),
        }
      }
      return { long: read(targets.long), short: read(targets.short) }
    }, TARGETS)

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${c.name}.png` })

    const below = (r) => r.chip.top >= r.pill.bottom - 2
    const dx = (r) => r.chip.x - r.textEdge
    const fails = []
    if (errors.length) fails.push(`${errors.length} page error(s): ${errors.join(' | ')}`)
    if (c.wraps) {
      if (!below(m.long)) fails.push(`long row: chip is not below the pill (chip.top=${m.long.chip.top}, pill.bottom=${m.long.pill.bottom})`)
      // 0 = the column's text edge. A non-zero value means the wrapper's -ml-2
      // leaked onto the wrapped line (negative) or an indent was over-applied.
      if (Math.abs(dx(m.long)) > 1) fails.push(`long row: wrapped chip is ${dx(m.long)}px off the text edge, expected 0`)
      // The whole point of wrapping: the label gets the column, not a sliver.
      if (m.long.pill.w < m.long.columnWidth * 0.9) fails.push(`long row: pill only claims ${m.long.pill.w}px of a ${m.long.columnWidth}px column — the label is still starved`)
      // ...and only a pair that cannot fit pays a line. A row whose pill and
      // chip fit together must NOT be forced apart, or every tool row in a
      // narrow column grows a second line.
      if (below(m.short)) fails.push('short row: chip wrapped even though the pair fits — narrow rows are being force-stacked')
    } else {
      if (below(m.long)) fails.push(`long row: chip wrapped below the pill in a ${m.long.columnWidth}px column where the pair fits`)
      // Compare against the pill's RIGHT edge, not its width: the wrapper's
      // -ml-2 starts the pill 8px left of the text edge, so `pill.w` alone is
      // not a position.
      if (m.long.chip.x < m.long.pill.x + m.long.pill.w) fails.push(`long row: chip is not to the RIGHT of the pill (chip.x=${m.long.chip.x}, pill right=${m.long.pill.x + m.long.pill.w})`)
      // The unwrapped separation is the chip's own 8px margin and nothing more:
      // the wrapper's row gap is 0, so a larger value means a gap was stacked on
      // top of the margin.
      const gap = m.long.chip.x - (m.long.pill.x + m.long.pill.w)
      if (gap > 9) fails.push(`long row: pill-to-chip gap is ${gap}px, expected the chip's 8px margin alone`)
    }

    if (fails.length) {
      failed += fails.length
      console.error(`FAIL ${c.name}:`)
      for (const f of fails) console.error(`     ${f}`)
    } else {
      console.log(`ok   ${c.name}.png — viewport ${c.width}px, column ${m.long.columnWidth}px, long pill ${m.long.pill.w}px over ~${m.long.labelLines} line(s) with chip dx=${dx(m.long)} ${c.wraps ? 'wrapped below' : 'beside'}; short row ${below(m.short) ? 'WRAPPED' : 'on one line'}`)
    }
  } catch (err) {
    failed++
    console.error(`FAIL ${c.name}: ${err.message}`)
  }
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed — the tool row does not lay out as claimed.`)
  process.exit(1)
}
