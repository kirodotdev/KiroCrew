/**
 * Screenshots of the pinned-session section's ordering (capture/pinned-session-order.html).
 *
 * Self-checking: asserts the RENDERED row sequence in each scene before it
 * screenshots, so a frame can never show the wrong state. A screenshot of the
 * wrong state is worse evidence than none -- it argues for the change while
 * showing something else.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort    # in another shell
 *   node scripts/capture-pinned-session-order.mjs http://127.0.0.1:6817 ../temp-screenshots/pinned-order-intent
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6817'
const OUT = process.argv[3] || '../temp-screenshots/pinned-order-intent'
mkdirSync(OUT, { recursive: true })

/** Rendered list-scope pinned row keys, top to bottom. */
const ROW_ORDER = () => [...document.querySelectorAll('[data-session-row][data-session-scope="list"]')]
  .map(el => el.getAttribute('data-session-row'))
  .filter(Boolean)

const SCENES = [
  // Stored order exists from membership bookkeeping; the user never reordered, so
  // the sort key governs and the newest session is on top.
  { scene: 'stored-not-reordered', expect: ['charlie', 'bravo', 'alpha'] },
  // Same stored order, marker set: the user's own arrangement wins.
  { scene: 'reordered', expect: ['alpha', 'bravo', 'charlie'] },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 360, height: 580 }, deviceScaleFactor: 2 })

for (const { scene, expect } of SCENES) {
  await page.goto(`${BASE}/capture/pinned-session-order.html?scene=${scene}`)
  await page.waitForFunction(
    n => document.querySelectorAll('[data-session-row][data-session-scope="list"]').length === n,
    expect.length,
  )
  const got = await page.evaluate(ROW_ORDER)
  if (got.join(',') !== expect.join(',')) {
    throw new Error(`scene ${scene}: rendered ${got.join(',')}, expected ${expect.join(',')}`)
  }
  await page.screenshot({ path: join(OUT, `${scene}.png`) })
  console.log(`captured ${scene}.png  (${got.join(' > ')})`)
}

await browser.close()
console.log(`done -> ${OUT}`)
