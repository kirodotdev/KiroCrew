/**
 * Real-layout probe for issue #9684. Drives the isolated capture entry, clicks
 * the unselected compact segment, and samples the active-pill indicator's
 * rendered box against the settled button box across the reveal animation.
 *
 *  - fix=off (pre-fix shape via the component seam: button `layout`, indicator
 *    size spring): the indicator MUST visibly disagree with the settled button
 *    box during the reveal (peak delta above a sub-pixel threshold). A before
 *    that matched the after would mean the defect never reproduced.
 *  - fix=on (shipped: button no `layout`, indicator `layout="position"`): the
 *    indicator MUST stay glued to the button box every frame (peak delta ~0)
 *    AND the pill MUST still travel between segments, so the fix is proven not
 *    to have frozen the animation.
 *
 * happy-dom computes no layout, so this browser probe is the only place the
 * divergence is observable; the unit suite pins the prop contract instead.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port <p> --strictPort
 *   node scripts/probe-segmented-indicator-9684.mjs http://127.0.0.1:<p> [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6837'
const OUT = process.argv[3] || '../temp-screenshots/segmented-indicator-9684'
mkdirSync(OUT, { recursive: true })

/** Below this the disagreement is sub-pixel jitter, not the tracking defect. */
const DEFECT_THRESHOLD = 4
/** fix=on must settle to within this of the button box at every frame. */
const FIXED_TOLERANCE = 1

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
let failures = 0
const results = {}

for (const fix of ['off', 'on']) {
  const page = await browser.newPage({ viewport: { width: 480, height: 320 } })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  await page.goto(`${BASE}/capture/segmented-indicator-track-9684.html?theme=dark&fix=${fix}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-host] button', { timeout: 15000 })
  const beforeLeft = await page.evaluate(() => {
    const ind = document.querySelector('[data-host] .absolute.inset-0')
    return ind ? Math.round(ind.getBoundingClientRect().left) : null
  })
  const { frames, peakDelta } = await page.evaluate(() => window.__sampleReveal())
  const afterLeft = await page.evaluate(() => {
    const ind = document.querySelector('[data-host] .absolute.inset-0')
    return ind ? Math.round(ind.getBoundingClientRect().left) : null
  })
  const travelled = beforeLeft != null && afterLeft != null ? Math.abs(afterLeft - beforeLeft) : 0
  results[fix] = { peakDelta, travelled, frames: frames.length }
  console.log(`fix=${fix}: peakDelta=${peakDelta}px  indicatorTravel=${travelled}px  frames=${frames.length}`)
  for (const f of frames.filter((_, i) => i % Math.max(1, Math.ceil(frames.length / 5)) === 0)) {
    console.log(`   t=${f.t}ms indicatorW=${f.indicatorW} buttonW=${f.buttonW} delta=${f.delta}`)
  }
  await page.screenshot({ path: `${OUT}/reveal-fix-${fix}.png`, fullPage: true })

  if (fix === 'off' && peakDelta < DEFECT_THRESHOLD) {
    console.error(`FAIL: pre-fix peakDelta ${peakDelta}px < ${DEFECT_THRESHOLD}px — defect did not reproduce, before/after would be meaningless`)
    failures++
  }
  if (fix === 'on' && peakDelta > FIXED_TOLERANCE) {
    console.error(`FAIL: fixed peakDelta ${peakDelta}px > ${FIXED_TOLERANCE}px — indicator still tracks the mid-animation box`)
    failures++
  }
  if (fix === 'on' && travelled < 4) {
    console.error(`FAIL: fixed indicatorTravel ${travelled}px — the pill no longer moves between segments; the fix froze the animation`)
    failures++
  }
  await page.close()
}

await browser.close()
console.log('\nsummary:', JSON.stringify(results))
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
