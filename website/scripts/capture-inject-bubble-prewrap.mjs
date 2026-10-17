/**
 * Measurement + screenshot runner for capture/inject-bubble-prewrap.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort
 *   node scripts/capture-inject-bubble-prewrap.mjs http://127.0.0.1:6821 \
 *     ../temp-screenshots/inject-bubble-prewrap
 *
 * The frames are evidence, but the MEASUREMENT is the point. The defect is a
 * vertical gap between a note's heading and the table under it, so the runner
 * measures exactly that gap and fails when a scene does not show the state it
 * claims. A run that photographs the wrong state exits nonzero rather than
 * emitting a misleading image.
 *
 * Thresholds are deliberately far apart rather than tight: the claim under test
 * is "tens of line-heights of dead space" vs "normal block spacing", and a
 * threshold tuned to the exact pixel count would break on any font-metric
 * change while proving nothing more.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/inject-bubble-prewrap'

/** Dead space above the table, in px, that counts as the defect reproducing. */
const INFLATED_MIN = 200
/** Dead space that counts as normal block spacing (`my-3` is 12px each side). */
const NORMAL_MAX = 48

mkdirSync(OUT, { recursive: true })

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failed = 0
const measured = {}

for (const theme of ['dark', 'light']) {
  for (const scene of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 1400 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${scene}.png`
    try {
      await page.goto(`${BASE}/capture/inject-bubble-prewrap.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector('[data-bubble] table', { timeout: 10000 })
      await page.waitForTimeout(400)

      const m = await page.evaluate(() => {
        const bubble = document.querySelector('[data-bubble]')
        const heading = bubble.querySelector('h2')
        const lead = bubble.querySelector('p')
        const wrapper = bubble.querySelector('div.overflow-x-auto')
        const table = bubble.querySelector('table')
        // The gap is measured from the LAST block before the table (the lead
        // paragraph) to the table wrapper's top. Measuring from the heading
        // instead would fold the paragraph's own height into the "gap" and
        // overstate it.
        const anchor = lead || heading
        const prose = document.querySelector('[data-bubble-prose]')
        return {
          gap: Math.round(wrapper.getBoundingClientRect().top - anchor.getBoundingClientRect().bottom),
          bubbleH: Math.round(bubble.getBoundingClientRect().height),
          tableH: Math.round(table.getBoundingClientRect().height),
          whiteSpace: getComputedStyle(wrapper).whiteSpace,
          proseH: Math.round(prose.getBoundingClientRect().height),
          proseBr: prose.querySelectorAll('br').length,
        }
      })
      measured[`${theme}-${scene}`] = m

      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })
      // The element-scoped shot above is the whole bubble, which in `before` is
      // ~8000px tall and scales down to an unreadable sliver in a review. This
      // second frame is the viewport — literally what the user sees: the
      // heading, then a screenful of nothing. That is the symptom, so it is the
      // frame worth putting in front of a reviewer.
      await page.screenshot({ path: `${OUT}/${theme}-${scene}-viewport.png` })

      let frameFailed = 0

      // 1. The gap: the property that actually regressed.
      if (scene === 'before' && m.gap < INFLATED_MIN) {
        frameFailed++
        console.error(`FAIL ${name}: expected the defect to reproduce (gap >= ${INFLATED_MIN}px) but gap is ${m.gap}px`)
      }
      if (scene === 'after' && m.gap > NORMAL_MAX) {
        frameFailed++
        console.error(`FAIL ${name}: expected normal block spacing (gap <= ${NORMAL_MAX}px) but gap is ${m.gap}px`)
      }

      // 2. The mechanism: `pre-wrap` must be what the table wrapper INHERITS in
      //    `before`, and must be gone in `after`. Without this a gap change
      //    could come from anything.
      const wantWs = scene === 'before' ? 'pre-wrap' : 'normal'
      if (m.whiteSpace !== wantWs) {
        frameFailed++
        console.error(`FAIL ${name}: table wrapper white-space is "${m.whiteSpace}", expected "${wantWs}"`)
      }

      // 3. The prose case: `after` drops `pre-wrap`, so `softBreaks` has to be
      //    what keeps a 3-line notification on 3 lines. Assert the <br> nodes
      //    exist rather than the height, which font metrics would make brittle.
      if (scene === 'after' && m.proseBr < 2) {
        frameFailed++
        console.error(`FAIL ${name}: plain prose lost its line breaks — ${m.proseBr} <br>, expected >= 2 (softBreaks not applied?)`)
      }

      if (errors.length) {
        frameFailed++
        console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
      }

      failed += frameFailed
      if (!frameFailed) {
        console.log(`ok   ${name}\n       gap=${m.gap}px  bubble=${m.bubbleH}px  table=${m.tableH}px  ws=${m.whiteSpace}  prose=${m.proseH}px/${m.proseBr}br`)
      }
    } catch (err) {
      failed++
      console.error(`FAIL ${name}: ${err.message}`)
    }
    await ctx.close()
  }
}

await browser.close()

// The headline number the change is about, printed once so a reader does not
// have to diff two ok lines to find it.
for (const theme of ['dark', 'light']) {
  const b = measured[`${theme}-before`]
  const a = measured[`${theme}-after`]
  if (b && a) {
    console.log(`\n${theme}: dead space above table ${b.gap}px -> ${a.gap}px (bubble ${b.bubbleH}px -> ${a.bubbleH}px)`)
  }
}

if (failed) {
  console.error(`\n${failed} assertion(s) failed — the frames do not show the state they claim.`)
  process.exit(1)
}
console.log('\nall scenes match their expected geometry')
