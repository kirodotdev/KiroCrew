/**
 * Screenshots of the collapsed reasoning row: live preview vs settled label.
 *
 * Drives the ISOLATED capture entry (website/capture/thinking-live-line.html),
 * which mounts the real ThinkingBlock against the real stylesheet and grows its
 * content on a timer, so the row's own liveness rule decides the frame.
 *
 * The run is SELF-CHECKING: it asserts the preview is present (and non-empty) on
 * the streaming scenes and absent on the settled one, so it can never quietly
 * emit a screenshot of the state it is meant to prove.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-thinking-live-line.mjs http://127.0.0.1:6812 ../temp-screenshots/thinking-live-line
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/thinking-live-line'
mkdirSync(OUT, { recursive: true })

/**
 * Per scene: whether a preview must be on screen, and whether it must be
 * clipped. `short` pins the state every burst OPENS in -- a preview that still
 * fits the row -- which is where a fade applied unconditionally would show as
 * missing first letters, so the scene caps its chunk count and the run asserts
 * both no overflow and no fade.
 */
const SCENES = [
  { scene: 'short', chunks: 2, live: true, clipped: false, note: 'preview fits the row: no clipping, no fade' },
  { scene: 'long', chunks: 1000, live: true, clipped: true, note: 'preview overflows: newest words at the right, left edge faded' },
  { scene: 'settled', chunks: 0, live: false, clipped: false, note: 'chunks stopped: back to the bare label' },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, chunks, live, clipped, note } of SCENES) {
      const ctx = await browser.newContext({ viewport: { width: 720, height: 140 }, deviceScaleFactor: 2 })
      const page = await ctx.newPage()
      await page.goto(`${BASE}/capture/thinking-live-line.html?scene=${scene}&theme=${theme}&chunks=${chunks}`)
      await page.waitForSelector('button[aria-expanded]')
      const line = page.getByTestId('thinking-live-line')
      if (live) {
        await line.waitFor({ state: 'visible', timeout: 5000 })
        if (clipped) {
          // Wait for the row to actually overflow rather than for a fixed delay,
          // so the frame is the clipped state by construction.
          await page.waitForFunction(() => {
            const el = document.querySelector('[data-testid="thinking-live-line"]')
            return !!el && el.scrollWidth > el.clientWidth
          }, null, { timeout: 5000 })
        }
      } else {
        await page.waitForTimeout(1600) // past the idle window
      }
      const shown = await line.count()
      const seen = shown
        ? await line.first().evaluate((el) => ({
          text: el.textContent || '',
          overflowing: el.scrollWidth > el.clientWidth,
          faded: (getComputedStyle(el).maskImage || 'none') !== 'none',
        }))
        : { text: '', overflowing: false, faded: false }
      const ok = live
        ? shown === 1 && seen.text.length > 0 && seen.overflowing === clipped && seen.faded === clipped
        : shown === 0
      if (!ok) {
        failed += 1
        console.error(`FAIL ${theme}/${scene}: shown=${shown} overflowing=${seen.overflowing} faded=${seen.faded} want clipped=${clipped}`)
      }
      await page.screenshot({ path: `${OUT}/${scene}-${theme}.png` })
      console.log(`${ok ? 'ok  ' : 'FAIL'} ${scene}-${theme}.png — ${note}`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) process.exit(1)
}

run()
