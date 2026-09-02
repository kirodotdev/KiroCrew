/**
 * Narrow-width measurement + screenshots for the three flex-1 input rows
 * (#3789): the shared Input, the SessionArchive filter row, and the ChatEmbed
 * composer.
 *
 * Drives the ISOLATED capture entry (website/capture/flex-input-min-w-0.html),
 * which renders each row with its production class strings inside an
 * overflow-hidden frame and exposes window.__measure(). This is the only check
 * that exercises REAL layout: the unit suite (src/test/flexInputMinWidth.test.tsx)
 * pins the class contract but happy-dom computes no layout, so a clip that
 * returns through a path leaving the classes intact (a consumer min-w-[…]
 * override, a new non-shrinking wrapper) is only caught here.
 *
 * Assertions:
 *  - fix=on: at every width, no scene's row overflows (scrollWidth <=
 *    clientWidth) and no trailing control's right edge escapes the clip box.
 *  - fix=off (the before state, min-w-0 stripped): at 320px every scene must
 *    reproduce the defect — a before frame identical to the after frame is
 *    exactly what a toggle that silently failed to apply would produce, so the
 *    reproduction is asserted, not assumed.
 *
 * The defect only reproduces at 320px: at 360px+ the input's ~162px automatic
 * minimum (size=20) already fits, so wider frames render identically in both
 * states and are captured for coverage of the fixed state only.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6809 --strictPort   # in another shell
 *   node scripts/capture-flex-input-min-w-0.mjs http://127.0.0.1:6809 ../temp-screenshots/flex-input-min-w-0
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6809'
const OUT = process.argv[3] || '../temp-screenshots/flex-input-min-w-0'
mkdirSync(OUT, { recursive: true })

/** The one width at which the pre-fix clip reproduces (see header comment). */
const REPRO_WIDTH = 320
const WIDTHS = [320, 360, 420]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 520, height: 640 } })
let failures = 0

for (const fix of ['off', 'on']) {
  for (const w of WIDTHS) {
    // Screenshots of the identical-render widths add nothing for fix=off.
    if (fix === 'off' && w !== REPRO_WIDTH) continue
    await page.goto(`${BASE}/capture/flex-input-min-w-0.html?theme=dark&w=${w}&fix=${fix}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-scene]')
    const measures = await page.evaluate(() => window.__measure())
    for (const m of measures) {
      const clips = m.overflows || m.trailingClipped
      console.log(
        `fix=${fix} w=${w} ${m.scene}: scrollW=${m.rowScrollWidth} clientW=${m.rowClientWidth} ` +
        `trailingRight=${m.trailingRight} clipRight=${m.clipRight} → ${clips ? 'CLIPS' : 'fits'}`,
      )
      if (fix === 'on' && clips) {
        console.error(`FAIL: ${m.scene} still overflows/clips at ${w}px with min-w-0 applied`)
        failures++
      }
      if (fix === 'off' && w === REPRO_WIDTH && !clips) {
        console.error(`FAIL: ${m.scene} did not reproduce the pre-fix clip at ${w}px — before/after evidence would be meaningless`)
        failures++
      }
    }
    await page.screenshot({ path: `${OUT}/${fix === 'off' ? 'before' : 'after'}-${w}px.png`, fullPage: true })
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
