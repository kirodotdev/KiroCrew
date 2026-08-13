/**
 * Screenshots of the trust menu's exact-command label.
 *
 * Drives the isolated capture entry (website/capture/trust-command-label.html),
 * which mounts the REAL ApprovalCard + TrustDropdown against the real
 * stylesheet, theme tokens and live i18n catalog. Radix renders the open menu
 * into a portal outside the capture root, so each frame is a viewport shot
 * rather than an element shot.
 *
 * Each scene ASSERTS ITS RENDERED LABEL before writing the file, and the run
 * additionally asserts that the two `api` scenes — two DIFFERENT commands
 * sharing a long prefix — do not render the SAME label. That cross-scene check
 * is the property the change exists to restore, and it fails on the old budget,
 * so a before-frame cannot be mistaken for a passing one.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-trust-command-label.mjs http://127.0.0.1:6821 ../temp-screenshots/trust-command-label
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/trust-command-label'
const PREFIX = process.argv[4] || ''
const EXPECT_DISTINCT = !process.argv.includes('--no-expect-distinct')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'trust-menu-api-config-dark', cmd: 'api_config', theme: 'dark', pair: 'api', width: 760 },
  { name: 'trust-menu-api-secrets-dark', cmd: 'api_secrets', theme: 'dark', pair: 'api', width: 760 },
  { name: 'trust-menu-api-config-light', cmd: 'api_config', theme: 'light', pair: null, width: 760 },
  { name: 'trust-menu-pipeline-dark', cmd: 'pipeline', theme: 'dark', pair: null, width: 760 },
  // Narrow viewport: pins the viewport-aware width cap. A flat max-w-[450px] ran
  // the menu off the right edge here (measured 440px against a 320px viewport),
  // hiding the label this menu exists to make readable.
  { name: 'trust-menu-api-secrets-narrow', cmd: 'api_secrets', theme: 'dark', pair: null, width: 320 },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 260 }, deviceScaleFactor: 2 })

let failed = false
const pairLabels = []
for (const s of SCENES) {
  await page.setViewportSize({ width: s.width, height: s.width === 320 ? 620 : 260 })
  await page.goto(`${BASE}/capture/trust-command-label.html?cmd=${s.cmd}&theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  // Open the menu: the labels under test only exist once it is open.
  await page.getByRole('button', { name: /Trust/ }).click()
  await page.waitForSelector('[role="menuitem"]')
  const items = await page.locator('[role="menuitem"]').allInnerTexts()
  const exact = (items[0] ?? '').trim()
  const hasBase = items.some(t => /commands/.test(t))
  const hasAll = items.some(t => /Trust all tools/.test(t))
  // GEOMETRY, not text. `innerText` returns the full label even when CSS has
  // ellipsis-clipped it, so a text assertion cannot see the render-vs-computed
  // gap: a `truncate` class silently cut off the very tail the middle-ellipsis
  // preserves, re-colliding two commands on screen while every unit test passed.
  // jsdom computes no layout, so this check only exists here.
  const clipped = await page.locator('[role="menuitem"]').first().evaluate(el => {
    const span = el.querySelector('span')
    if (!span) return { ok: false, why: 'no label span' }
    const over = span.scrollWidth - span.clientWidth
    return { ok: over <= 1, why: `scrollWidth-clientWidth=${over}` }
  })
  const box = await page.locator('[role="menu"]').first().boundingBox()
  const vw = await page.evaluate(() => document.documentElement.clientWidth)
  const fitsViewport = Math.round(box.x + box.width) <= vw && Math.round(box.x) >= 0
  const ok = items.length === 3 && exact.startsWith('Trust') && hasBase && hasAll
    && clipped.ok && fitsViewport
  console.log(
    `${s.name}: items=${items.length} unclipped=${clipped.ok} (${clipped.why}) ` +
    `fitsViewport=${fitsViewport} label=${JSON.stringify(exact)} ${ok ? 'OK' : 'MISMATCH'}`,
  )
  if (!ok) { failed = true; continue }
  if (s.pair === 'api') pairLabels.push(exact)
  await page.screenshot({ path: `${OUT}/${PREFIX}${s.name}.png` })
}

if (pairLabels.length === 2) {
  const distinct = pairLabels[0] !== pairLabels[1]
  console.log(`api pair distinguishable: ${distinct}`)
  if (EXPECT_DISTINCT && !distinct) {
    console.error('the two api commands render the SAME label — the reader cannot tell them apart')
    failed = true
  }
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected label — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
