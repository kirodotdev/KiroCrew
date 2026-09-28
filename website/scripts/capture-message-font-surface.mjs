/**
 * Screenshots + assertions for the message font size setting scaling the whole
 * conversation surface (website/capture/message-font-surface.html).
 *
 * The assertions are the point: a frame in which the code block, table cell,
 * chip or composer stayed at its old size would photograph plausibly and fail
 * here. Each element's computed font-size in the large column must equal the
 * default column's times size/14 (within a pixel), and the compact width must
 * scale the same way.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6847 --strictPort   # in another shell
 *   node scripts/capture-message-font-surface.mjs http://127.0.0.1:6847 ../temp-screenshots/message-font-surface
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6847'
const OUT = process.argv[3] || '../temp-screenshots/message-font-surface'
mkdirSync(OUT, { recursive: true })

const SIZE = 20
const RATIO = SIZE / 14

/** selector -> label; each is measured in both columns (selectors must be comma-free: they are scoped per column). */
const PROBES = {
  '.msg-content p': 'prose',
  '.msg-content :not(pre) > code': 'inline code',
  '.msg-content .pierre-surface diffs-container': 'code block',
  '.msg-content td': 'table cell',
  '.msg-content th': 'table header',
  '.msg-content span.mc-md-ref-chip': 'reference chip',
  'button.mc-message-font-chip': 'follow-up chip',
  '[data-composer-typo][data-lexical-composer]': 'composer',
}

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

async function px(page, size, selector, prop = 'font-size') {
  return page.evaluate(
    ([size, selector, prop]) => {
      const el = document.querySelector(`[data-size="${size}"] ${selector}`)
      return el ? parseFloat(getComputedStyle(el)[prop]) : NaN
    },
    [size, selector, prop],
  )
}

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1260, height: 1500 } })
  await page.goto(`${BASE}/capture/message-font-surface.html?theme=${theme}&size=${SIZE}`, { waitUntil: 'networkidle' })
  await page.waitForSelector(`[data-size="${SIZE}"] .msg-content td`)
  await page.waitForSelector(`[data-size="${SIZE}"] [data-composer-typo]`)
  await page.waitForSelector(`[data-size="${SIZE}"] diffs-container`)

  for (const [selector, label] of Object.entries(PROBES)) {
    const base = await px(page, 14, selector)
    const large = await px(page, SIZE, selector)
    const want = base * RATIO
    const ok = Number.isFinite(base) && Number.isFinite(large) && Math.abs(large - want) < 1
    console.log(`${theme} ${label.padEnd(13)}: ${base.toFixed(2)}px -> ${large.toFixed(2)}px (want ${want.toFixed(2)}) ${ok ? 'ok' : 'FAIL'}`)
    if (!ok) failures++
  }
  // Row leading follows too: td line-height at 20px must be 20/14 of the default's.
  const lhBase = await px(page, 14, '.msg-content td', 'line-height')
  const lhLarge = await px(page, SIZE, '.msg-content td', 'line-height')
  const lhOk = Math.abs(lhLarge - lhBase * RATIO) < 1
  console.log(`${theme} ${'td leading'.padEnd(13)}: ${lhBase.toFixed(2)}px -> ${lhLarge.toFixed(2)}px ${lhOk ? 'ok' : 'FAIL'}`)
  if (!lhOk) failures++

  // The column itself must widen: the rendered section is the Compact width
  // plus its padding, so the two panels differ by exactly the width delta.
  const w14 = await page.evaluate(() => document.querySelector('[data-size="14"]').getBoundingClientRect().width)
  const wL = await page.evaluate((s) => document.querySelector(`[data-size="${s}"]`).getBoundingClientRect().width, SIZE)
  const wantW = 800 * RATIO - 800
  const wOk = Math.abs((wL - w14) - wantW) < 2
  console.log(`${theme} ${'compact width'.padEnd(13)}: ${w14.toFixed(0)}px -> ${wL.toFixed(0)}px (delta ${(wL - w14).toFixed(0)}, want ${wantW.toFixed(0)}) ${wOk ? 'ok' : 'FAIL'}`)
  if (!wOk) failures++

  const shot = `${OUT}/message-font-surface-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path: shot })
  console.log(`  -> ${shot}`)
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed.`)
  process.exit(1)
}
console.log('\nevery surface scales at its default ratio.')
