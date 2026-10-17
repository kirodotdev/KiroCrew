/**
 * Real-layout measurement + screenshots for the follow-up option chips that
 * never form two columns (#5397).
 *
 * Drives the ISOLATED capture entry (website/capture/followup-chip-columns.html),
 * which mounts the REAL FollowUpBar inside ChatInput's `input-area` box chain at
 * each content width and exposes window.__measure().
 *
 * This is the only check that exercises REAL layout. The unit suite
 * (src/test/FollowUpBar.test.tsx) pins the CSS text and the class contract, but
 * happy-dom computes no layout — so a cap wider than half the row leaves every
 * class assertion green while stacking one chip per row, which is exactly the
 * defect that shipped.
 *
 * Assertions:
 *  - fix=on: 4 long options occupy at most 2 rows at every content width, and no
 *    chip is narrower than the 18rem floor (a cap that collapsed to a bare 50%
 *    would pass the row count and make the labels unreadable).
 *  - fix=off: at compact width the before state must reproduce 4 rows. A before
 *    frame identical to the after frame is exactly what a toggle that silently
 *    failed to apply would produce, so the reproduction is asserted, not assumed.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-followup-chip-columns.mjs http://127.0.0.1:6812 ../temp-screenshots/followup-chip-columns
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/followup-chip-columns'
mkdirSync(OUT, { recursive: true })

const CONTENT_WIDTHS = ['compact', 'comfortable', 'full']
/**
 * Wide enough that `compact` is capped by its own 816px rather than the window,
 * and short enough that the bar + composer fill the frame — the bar sits at the
 * bottom of the pane, so a tall viewport would spend most of the shot on empty
 * transcript background.
 */
const VIEWPORT = { width: 1280, height: 360 }
/** The 18rem floor, in px at the default 16px root. */
const MIN_READABLE_CHIP = 18 * 16
/** The content width whose composer is narrowest, so the defect reproduces. */
const REPRO_WIDTH = 'compact'
/** Screenshot evidence comes from the reported path only; the rest is asserted. */
const SHOT_WIDTH = 'compact'
/**
 * The chips' staggered entrance (`animate-chip-hop`) is still translating them
 * for FOLLOWUP_CHIP_STAGGER_MS × MAX_STEPS + HOP_DURATION ≈ 750ms after mount.
 * Measuring inside that window reads mid-animation offsets, and a screenshot
 * taken there catches a chip part-way through its hop.
 */
const ENTRANCE_SETTLE_MS = 900

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const fix of ['off', 'on']) {
  for (const width of CONTENT_WIDTHS) {
    const page = await browser.newPage({ viewport: VIEWPORT })
    await page.goto(
      `${BASE}/capture/followup-chip-columns.html?theme=dark&width=${width}&layout=multiline&fix=${fix}`,
      { waitUntil: 'networkidle' },
    )
    await page.waitForSelector('span.followup-chip')
    await page.waitForTimeout(ENTRANCE_SETTLE_MS)
    const m = await page.evaluate(() => window.__measure())
    console.log(
      `fix=${fix} ${width.padEnd(11)}: row=${String(m.rowWidth).padStart(4)} ` +
      `chip=${String(m.chipWidth).padStart(3)} rows=${m.rows} cols=${m.columns} barH=${String(m.barHeight).padStart(3)} ` +
      `→ ${m.columns >= 2 ? `${m.columns} columns` : `1 column, ${m.rows} STACKED ROWS`}`,
    )
    if (fix === 'on') {
      if (m.rows > 2 || m.columns < 2) {
        console.error(`FAIL: ${width} renders 4 options as ${m.columns} column(s) / ${m.rows} row(s) with the relative cap applied`)
        failures++
      }
      if (m.chipWidth < MIN_READABLE_CHIP) {
        console.error(`FAIL: ${width} chip collapsed to ${m.chipWidth}px, below the ${MIN_READABLE_CHIP}px floor — labels unreadable`)
        failures++
      }
      if (m.chipWidth > m.rowWidth / 2) {
        console.error(`FAIL: ${width} chip ${m.chipWidth}px exceeds half the ${m.rowWidth}px row — two columns cannot fit`)
        failures++
      }
    }
    if (fix === 'off' && width === REPRO_WIDTH && (m.rows <= 2 || m.columns >= 2)) {
      console.error(`FAIL: ${width} did not reproduce the pre-fix stacking — before/after evidence would be meaningless`)
      failures++
    }
    if (width === SHOT_WIDTH) {
      await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-compact.png` })
    }
    await page.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
