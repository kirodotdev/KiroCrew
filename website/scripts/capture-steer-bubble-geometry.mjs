/**
 * Screenshot + measurement runner for capture/steer-bubble-geometry.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-steer-bubble-geometry.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/steer-bubble-geometry
 *
 * The frames are evidence, but the ASSERTIONS are the point:
 *  - every user bubble (steered long / steered short / plain long) shares ONE
 *    end edge: identical endGap to the content column's right edge;
 *  - the entrance ring, sampled MID-ANIMATION, never extends past the row
 *    wrapper's overflow-hidden clip edge (clippedRight <= 0).
 * A run that photographs a broken state exits nonzero instead of emitting a
 * misleading image. 800px viewport at deviceScaleFactor 2 keeps each frame
 * under 2000px per edge; the 390px pass guards the phone-viewport regression
 * (#4116) without a screenshot.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/steer-bubble-geometry'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const width of [800, 390]) {
    const ctx = await browser.newContext({
      viewport: { width, height: 700 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))
    const name = `${theme}-${width}.png`
    try {
      await page.goto(`${BASE}/capture/steer-bubble-geometry.html?theme=${theme}`, {
        waitUntil: 'domcontentloaded',
      })
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      // Sample at 500ms: the 320ms slide-in has settled (during the slide the
      // whole bubble legitimately overflows the clip — that is the entrance),
      // while the ~0.9s ring is still alive. This is the window where the
      // ring's REST geometry is measurable.
      await page.waitForTimeout(500)
      const geo = await page.evaluate('window.__measure()')
      if (geo.length !== 3) throw new Error(`expected 3 user bubbles, saw ${geo.length}`)
      const gaps = geo.map(g => g.endGap)
      if (new Set(gaps).size !== 1) {
        throw new Error(`end edges diverge: endGaps=${JSON.stringify(gaps)} — a steered bubble is not end-aligned`)
      }
      for (const g of geo) {
        if (g.ring && g.ring.clippedRight > 0) {
          throw new Error(`entrance ring extends ${g.ring.clippedRight}px past the clip edge on bubble ${g.i}`)
        }
      }
      // Screenshot only the wide pass — the narrow pass is a pure assertion.
      if (width === 800) await page.screenshot({ path: `${OUT}/${name}`, fullPage: false })
      if (errors.length) throw new Error(`pageerror: ${errors[0]}`)
      console.log(`OK   ${name} endGaps=${JSON.stringify(gaps)}`)
    } catch (e) {
      console.error(`FAIL ${name}: ${e.message}`)
      failed++
    } finally {
      await ctx.close()
    }
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
