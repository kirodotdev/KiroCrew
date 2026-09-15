/**
 * Verifies trackpad / ctrl+wheel magnification in a REAL engine (PR #6262).
 *
 * The unit suite fabricates its events, because jsdom has no `GestureEvent` and
 * its `WheelEvent` constructor drops every inherited MouseEvent field. So it pins
 * the arithmetic and none of the browser contract. This script exercises the
 * parts only Blink can answer:
 *
 *   A. Blink's own ctrl+wheel (real key state + real wheel) reaches the hook and
 *      zooms, against REAL layout rather than a stubbed box.
 *   B. `preventDefault()` is HONOURED — asserted by the absence of Blink's
 *      "Unable to preventDefault inside passive event listener invocation".
 *      That console error is exactly what a React `onWheel` prop would produce,
 *      since React registers `wheel` passively at the root. Its absence is the
 *      measurement behind using a manual non-passive `addEventListener`.
 *   C. A PLAIN wheel is NOT claimed, so the scroller a no-viewBox diagram
 *      depends on still scrolls.
 *   D. On a no-viewBox diagram the gesture is not claimed at all, leaving the
 *      browser page zoom that DOES magnify non-fit-scaled content intact.
 *
 * NOT covered, and deliberately not claimed: WebKit's `gesturestart` /
 * `gesturechange` path. Playwright's WebKit is not Safari and does not implement
 * those events, so no automated run here can speak for them. That half stays a
 * hardware check.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-trackpad-zoom.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/trackpad-zoom'
mkdirSync(OUT, { recursive: true })

/** Declared, not discovered: OUT is caller-supplied, so the cleanup must be able
 *  to name every file it deletes rather than globbing a pattern wide enough to
 *  catch something the caller owns. */
const FRAMES = ['01-fit', '02-ctrl-wheel-zoomed', '03-natural-untouched']
for (const frame of FRAMES) rmSync(join(OUT, `${frame}.png`), { force: true })

const VIEWPORT = { width: 1280, height: 800 }

let failures = 0
function check(ok, label, detail = '') {
  if (ok) { console.log(`  ok   ${label}`); return }
  failures++
  console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`)
}

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const page = await browser.newPage({ viewport: VIEWPORT })

/** Blink's passive-listener complaint, if our registration were passive. */
const passiveErrors = []
page.on('console', m => {
  if (/passive event listener/i.test(m.text())) passiveErrors.push(m.text())
})

async function shot(name) {
  if (!FRAMES.includes(name)) throw new Error(`frame '${name}' is not in FRAMES`)
  await page.screenshot({ path: join(OUT, `${name}.png`) })
}

/** The transform the viewer applies to its SVG host. */
const hostStyle = () => page.evaluate(() => {
  const el = document.querySelector('[role="dialog"] .overflow-auto > div')
  return el ? (el.getAttribute('style') ?? '') : null
})

const CAPTURE_URL = `${BASE}/capture/trackpad-zoom.html?theme=dark`

// ---------------------------------------------------------------- fit-scaled
console.log('\nfit-scaled diagram (viewBox present)')
await page.goto(CAPTURE_URL, { waitUntil: 'networkidle' })
await page.waitForSelector('[role="dialog"] .overflow-auto > div')
await shot('01-fit')

const atFit = await hostStyle()
check(/scale\(1\)/.test(atFit ?? ''), 'opens at fit', `style=${atFit}`)

// (C) A PLAIN wheel must not be claimed — assert before the ctrl case so a
// wrongly-claimed plain wheel cannot be mistaken for the ctrl one working.
await page.mouse.move(640, 400)
await page.mouse.wheel(0, -120)
await page.waitForTimeout(120)
check(/scale\(1\)/.test((await hostStyle()) ?? ''), 'a plain wheel does not zoom')

// (A) Blink's real ctrl+wheel: real modifier key state, real wheel event.
await page.keyboard.down('Control')
await page.mouse.wheel(0, -120)
await page.keyboard.up('Control')
await page.waitForTimeout(160)

const zoomed = await hostStyle()
await shot('02-ctrl-wheel-zoomed')
const scale = Number((zoomed ?? '').match(/scale\(([\d.]+)\)/)?.[1] ?? '1')
check(scale > 1, 'ctrl+wheel magnifies through Blink', `style=${zoomed}`)
check(scale <= 1.25 + 1e-9, 'one event stays inside the per-event clamp', `scale=${scale}`)

// (B) The claim was honoured rather than dropped as passive.
check(passiveErrors.length === 0, 'preventDefault honoured (listener is non-passive)',
  passiveErrors.join(' | '))

// Zooming out returns to fit and stops there.
await page.keyboard.down('Control')
await page.mouse.wheel(0, 240)
await page.mouse.wheel(0, 240)
await page.keyboard.up('Control')
await page.waitForTimeout(160)
check(/scale\(1\)/.test((await hostStyle()) ?? ''), 'returns to fit and clamps there')

// ------------------------------------------------------------ natural size
console.log('\nno-viewBox diagram (not fit-scaled)')
await page.goto(`${CAPTURE_URL}&variant=natural`, { waitUntil: 'networkidle' })
await page.waitForSelector('[role="dialog"] .overflow-auto > div')

const naturalBefore = await hostStyle()
check(naturalBefore === '' || naturalBefore === null, 'carries no transform', `style=${naturalBefore}`)

// (D) The gesture must not be claimed here at all.
await page.mouse.move(640, 400)
await page.keyboard.down('Control')
await page.mouse.wheel(0, -120)
await page.keyboard.up('Control')
await page.waitForTimeout(160)
await shot('03-natural-untouched')
const naturalAfter = await hostStyle()
check(naturalAfter === naturalBefore, 'ctrl+wheel leaves a no-viewBox diagram alone',
  `before=${naturalBefore} after=${naturalAfter}`)
check(passiveErrors.length === 0, 'still no passive-listener complaint',
  passiveErrors.join(' | '))

await browser.close()
console.log(`\n${failures === 0 ? 'PASS' : `FAIL (${failures})`} — frames in ${OUT}`)
console.log('NOT covered here: WebKit gesturestart/gesturechange (Playwright WebKit is not Safari).')
process.exit(failures === 0 ? 0 : 1)
