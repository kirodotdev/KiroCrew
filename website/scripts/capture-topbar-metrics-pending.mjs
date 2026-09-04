/**
 * Pixel + box evidence for the metrics readout's open-but-no-frame state.
 *
 * Drives the shared top-bar capture entry (website/capture/topbar-search-variants.html),
 * which renders the shipped `.topbar` / `.tb-right` / `.tb-capsule` class strings and
 * lets the real stylesheet lay them out. Booting <App/> needs a live gateway
 * session; what is under test here is the segment's presence and its footprint.
 *
 * `?metricsfix=off` is the before state: the open branch pushed a segment only
 * when the query had ERRORED, so the state where it had produced no frame yet
 * rendered nothing. The readout was logically open with its toggle off screen —
 * the reported "the metrics doesn't open on click", because the control the click
 * was aimed at is gone.
 *
 * Two instruments, for two different claims:
 *
 *  - BOXES prove the fix does not recalibrate the ladder: the actions group's
 *    CONTENT box — which is what the @container rungs measure, and what #7851
 *    tuned every rung against — is identical in all three scenes, and the pending
 *    segment is never WIDER than the loaded one, so it cannot trip a rung the
 *    loaded state would not. Width parity is deliberately not claimed: dashes are
 *    narrower than readings, and the loaded readout's own width moves as the
 *    values do. The invariant is that the SEGMENT COUNT is the same pending and
 *    loaded, i.e. nothing mounts when the frame lands.
 *  - PIXELS prove two things. The before render is missing a segment and a
 *    divider, so before and after must DIFFER — a fix that failed to apply would
 *    leave them byte-identical. And the after render must differ from the LOADED
 *    render only inside the CAPSULE: that is the real claim, because it says the
 *    frame landing repaints the readout and moves nothing else in the header —
 *    the feedback pill and the bell to its right stay exactly where they are.
 *    The capsule and not the segment, because the capsule is right-aligned in a
 *    `justify-content:flex-end` group: dashes are narrower than readings, so the
 *    capsule's own left edge sits ~33px further right while no sibling moves.
 *
 *    Confinement is asserted on pending-vs-loaded rather than before-vs-after on
 *    purpose. Restoring the segment legitimately reflows the group — below the
 *    530px metrics rung the group is already in its squeeze band, and the 41px
 *    the segment occupies is taken from the feedback pill, exactly as it is in
 *    the loaded state. A before-vs-after confinement check would be asserting
 *    that the fix does not do the thing it exists to do.
 *
 * Assertions, per width:
 *  - metricsfix=off reproduces the defect: no metrics segment in the capsule
 *  - metricsfix=on renders one, and it is the same width as the loaded segment
 *  - the capsule's own width is the same pending and loaded
 *  - the actions group's CONTENT box width is unchanged across all three, so the
 *    collapse ladder cannot have been recalibrated
 *  - the before/after renders DIFFER, and every differing pixel is in the capsule
 *
 * The glass promotion in the same change (`transform:translateZ(0)` +
 * `backface-visibility:hidden` on `.topbar-glass`) is NOT asserted here, and
 * deliberately: it fixes a stale-raster bug in the compositor, and a screenshot
 * of a correct render cannot distinguish a promoted layer from an unpromoted one.
 * Its contract is pinned from the stylesheet source in
 * `src/test/topbarGlassLayer.test.ts`; what this script can say about it is that
 * the header's geometry is untouched, which the group-width assertion covers.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-topbar-metrics-pending.mjs http://127.0.0.1:6812 ../temp-screenshots/topbar-metrics-pending
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { diffPngs } from './lib/diff-pngs.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/topbar-metrics-pending'
mkdirSync(OUT, { recursive: true })

// Above and below the base metrics rung (`@container (max-width:530px)` in
// index.css). 1900px of viewport gives the actions group ~640px so the readings
// themselves render; 1280px gives it ~475px, which is the icon-collapsed form —
// the same form the reporter's 243px crop was in, and the one where a missing
// toggle is least recoverable because there is no text left to click either.
const WIDTHS = [1900, 1280]
const PAGE = (q) =>
  `${BASE}/capture/topbar-search-variants.html?theme=dark&count=11&form=desktop&${q}`

const SCENES = {
  'pending-before': 'metrics=pending&metricsfix=off',
  'pending-after': 'metrics=pending',
  loaded: 'metrics=loaded',
}

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }
const ok = (msg) => console.log(`ok: ${msg}`)
const near = (a, b, tol = 0.5) => Math.abs(a - b) <= tol

/** Same thresholded diff as capture-topbar-badge-overhang.mjs, and for the same
 *  reason: any change to a box inside the header re-rasterises its
 *  `backdrop-filter` layer and sprays single-channel ±1 noise across the whole
 *  strip. Counting that noise would let the diff's bounding box swallow the
 *  header and make "confined to the capsule" vacuous. `rawN` is reported so the
 *  noise stays visible rather than silently discarded. */
const TOL = 24


/** Boxes of the three things every claim here is about. */
const measure = (page) => page.evaluate(() => {
  const q = (s) => document.querySelector(s)
  const box = (el) => {
    if (!el) return null
    const r = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    return {
      x: r.x, y: r.y, w: r.width, h: r.height,
      // Content box: what the @container rungs actually measure.
      contentW: r.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
    }
  }
  return {
    header: box(q('.topbar')),
    group: box(q('.tb-right')),
    capsule: box(q('.tb-capsule')),
    metrics: box(q('[data-metrics]')),
    // Direct children of the capsule: segments AND the dividers between them.
    // This is the count that used to change when the frame landed.
    segCount: q('.tb-capsule') ? q('.tb-capsule').children.length : 0,
  }
})

const browser = await chromium.launch()
const page = await browser.newPage()
const shots = {}

for (const w of WIDTHS) {
  await page.setViewportSize({ width: w, height: 220 })
  const m = {}
  for (const [name, q] of Object.entries(SCENES)) {
    await page.goto(PAGE(q), { waitUntil: 'networkidle' })
    // The entry initialises i18n asynchronously; the capsule is what we measure,
    // so wait for it rather than a fixed delay.
    await page.waitForSelector('.tb-capsule')
    m[name] = await measure(page)
    console.log(`  ${w}/${name}: capsule=${m[name].capsule.w.toFixed(1)} metrics=${m[name].metrics ? m[name].metrics.w.toFixed(1) : 'absent'} groupContent=${m[name].group.contentW.toFixed(1)}`)
    const file = join(OUT, `${w}-${name}.png`)
    const buf = await page.screenshot({ path: file, clip: { x: 0, y: 0, width: w, height: 60 } })
    shots[`${w}-${name}`] = buf.toString('base64')
    console.log(`wrote ${file}`)
  }

  // --- the defect, reproduced -------------------------------------------------
  if (m['pending-before'].metrics) fail(`${w}: expected NO metrics segment in the before state`)
  else ok(`${w}: before state has no metrics segment (the defect)`)

  // --- the fix renders one ---------------------------------------------------
  if (!m['pending-after'].metrics) fail(`${w}: expected a metrics segment in the pending state`)
  else ok(`${w}: pending state renders a metrics segment`)

  // --- nothing mounts when the frame lands ----------------------------------
  // The segment and divider count is what used to change; the text inside the
  // button is what changes now.
  if (m['pending-after'].segCount !== m.loaded.segCount) {
    fail(`${w}: capsule holds ${m['pending-after'].segCount} children pending vs ${m.loaded.segCount} loaded — a segment still mounts on arrival`)
  } else ok(`${w}: capsule child count identical pending and loaded (${m.loaded.segCount}) — nothing mounts on arrival`)
  if (m['pending-before'].segCount === m.loaded.segCount) {
    fail(`${w}: before state already had the loaded child count — the defect is not reproduced`)
  } else ok(`${w}: before state is short by ${m.loaded.segCount - m['pending-before'].segCount} child/children (segment + divider)`)
  // Never wider than loaded, so the pending form cannot trip a rung the loaded
  // form clears.
  if (m['pending-after'].metrics && m.loaded.metrics && m['pending-after'].metrics.w > m.loaded.metrics.w + 0.5) {
    fail(`${w}: pending segment ${m['pending-after'].metrics.w.toFixed(1)}px is WIDER than loaded ${m.loaded.metrics.w.toFixed(1)}px`)
  } else ok(`${w}: pending segment is not wider than loaded (${m['pending-after'].metrics.w.toFixed(1)} <= ${m.loaded.metrics.w.toFixed(1)}px)`)

  // The ladder's calibration: the group's content box is the container queries'
  // measurement, and #7851 tuned every rung against it.
  for (const name of Object.keys(SCENES)) {
    if (!near(m[name].group.contentW, m.loaded.group.contentW, 0.5)) {
      fail(`${w}/${name}: actions-group content width ${m[name].group.contentW.toFixed(1)}px != ${m.loaded.group.contentW.toFixed(1)}px — the collapse ladder would be off calibration`)
    }
  }
  ok(`${w}: actions-group content width identical across all three scenes`)
  if (!near(m['pending-before'].header.h, m.loaded.header.h, 0.5)) {
    fail(`${w}: header height changed between scenes`)
  } else ok(`${w}: header height unchanged (${m.loaded.header.h.toFixed(1)}px)`)

  // --- pixels: the fix is not inert ----------------------------------------
  const dBefore = await diffPngs(page, shots[`${w}-pending-before`], shots[`${w}-pending-after`], TOL)
  if (dBefore.n === 0) fail(`${w}: before and after renders are identical — the fix did not apply`)
  else ok(`${w}: ${dBefore.n} pixels differ before vs after (${dBefore.rawN} raw incl. backdrop noise)`)

  // --- pixels: pending sits in the loaded layout ----------------------------
  const dLoaded = await diffPngs(page, shots[`${w}-pending-after`], shots[`${w}-loaded`], TOL)
  const segBox = m.loaded.metrics
  if (dLoaded.n === 0) {
    // Legitimate on the icon-collapsed rung: with the readings hidden, the two
    // open states render the same accent glyph and differ only in opacity, which
    // is below the noise threshold. Nothing to confine.
    ok(`${w}: pending and loaded renders are identical (readings hidden on this rung)`)
  } else {
    const a = m['pending-after'].capsule, b = m.loaded.capsule
    const lo = Math.min(a.x, b.x)
    const hi = Math.max(a.x + a.w, b.x + b.w)
    if (dLoaded.minX < Math.floor(lo) - 2 || dLoaded.maxX > Math.ceil(hi) + 2) {
      fail(`${w}: pending differs from loaded at [${dLoaded.minX}..${dLoaded.maxX}], outside the capsule [${lo.toFixed(0)}..${hi.toFixed(0)}] — a sibling in the header moved`)
    } else {
      ok(`${w}: ${dLoaded.n} pixels differ pending vs loaded, all inside the capsule — no sibling moved`)
    }
  }
}

writeFileSync(join(OUT, 'README.txt'),
  'Before/after for the metrics readout with no frame (issue #7967).\n' +
  '  *-pending-before.png  open readout, no frame -> NO segment (the defect)\n' +
  '  *-pending-after.png   open readout, no frame -> dimmed em-dash segment, same width\n' +
  '  *-loaded.png          open readout with a frame\n')

await browser.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed`)
  process.exit(1)
}
console.log('\nall assertions passed')
