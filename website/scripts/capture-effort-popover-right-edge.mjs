/**
 * Right-edge measurement + before/after screenshots for the reasoning-effort
 * popover (the composer chip's 240px portaled menu).
 *
 * Drives the isolated capture entry (website/capture/effort-popover-right-edge.html),
 * which places the REAL popover with the host's own positioning expression inside
 * an overflow-hidden frame and exposes window.__measure(). The unit suite
 * (src/test/effortPopoverClamp.test.ts) pins the clamp arithmetic; only this
 * check renders the popover at real layout, so a right edge that escapes for a
 * reason the arithmetic cannot see is caught here.
 *
 * Assertions:
 *  - fix=on: at every frame width the popover's right edge stays inside the
 *    frame's 8px gutter, including the width below the popover's own 240px
 *    where it shrinks to `max-w-[calc(100vw-16px)]`.
 *  - fix=off: the pre-fix expression must OVERFLOW at every width — a before
 *    frame identical to the after frame is what a toggle that silently failed
 *    to apply would produce, so the reproduction is asserted, not assumed.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6809 --strictPort   # in another shell
 *   node scripts/capture-effort-popover-right-edge.mjs http://127.0.0.1:6809 ../temp-screenshots/effort-popover-right-edge
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6809'
const OUT = process.argv[3] || '../temp-screenshots/effort-popover-right-edge'
mkdirSync(OUT, { recursive: true })

/** Frame widths to photograph. 240 is below the popover's own width, so it is
 *  the one that exercises the `max-w-[calc(100vw-16px)]` shrink branch — and
 *  that only holds when the WINDOW is the frame, since `100vw` resolves against
 *  the window, never the frame. So the page viewport follows the frame width. */
const WIDTHS = [240, 900, 1280]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: WIDTHS[0], height: 480 } })
let failures = 0

for (const fix of ['off', 'on']) {
  for (const w of WIDTHS) {
    for (const theme of ['dark', 'light']) {
      await page.setViewportSize({ width: w, height: 480 })
      await page.goto(`${BASE}/capture/effort-popover-right-edge.html?theme=${theme}&w=${w}&fix=${fix}`, { waitUntil: 'networkidle' })
      await page.waitForSelector('[data-scene="effort-popover"]')
      const m = await page.evaluate(() => window.__measure())
      console.log(
        `fix=${fix} w=${w} theme=${theme}: left=${m.left} right=${m.right} frameRight=${m.frameRight} → ${m.overflows ? 'OVERFLOWS' : 'fits'}`,
      )
      if (fix === 'on' && m.overflows) {
        console.error(`FAIL: popover right edge ${m.right} escapes a ${w}px frame with the clamp applied`)
        failures++
      }
      if (fix === 'off' && !m.overflows) {
        console.error(`FAIL: the pre-fix expression did not overflow at ${w}px — before/after evidence would be meaningless`)
        failures++
      }
      await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-${w}px-${theme}.png` })
    }
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
