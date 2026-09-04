/**
 * Proves the reveal highlight HOLDS, then FADES, rather than vanishing in one frame.
 *
 * A screenshot cannot show motion, so this samples the live decoration's computed
 * `background-color` across its lifetime. Samples are judged against the
 * element's OWN animation clock (`Animation.currentTime`, which counts the delay
 * phase) rather than wall-clock time: `waitForSelector` resolves some unknown
 * lag after the decoration is inserted, so wall-clock samples drift against the
 * real animation and a tail sample can land past the end. Reading the animation's
 * own time removes that dependence instead of merely budgeting for it, and is
 * what makes the hold assertable at all.
 *
 * Guards three separable regressions:
 *   1. no hold      — fading immediately, so the reader never sees a lit band
 *   2. no fade      — full alpha right up to the frame it disappears (the
 *                     original bug: a `transition` cannot fade a decoration,
 *                     because clearing it removes Monaco's node outright)
 *   3. no relight   — a repeat reveal painting nothing, because Monaco reuses the
 *                     rendered line node and the finished animation stays applied
 *
 * Also writes frames so the change has visual evidence for review.
 *
 * Needs a dev server already running, so this is a MANUAL verification harness,
 * not a CI gate — its assertions read real browser animation state and would be
 * timing-fragile on shared CI runners. Exposed as `npm run verify:reveal-fade` so
 * it stays discoverable:
 *
 *   npx vite --host 127.0.0.1 --port 6807 &
 *   npm run verify:reveal-fade -- http://127.0.0.1:6807 ../temp-screenshots/path-chips
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6807'
const OUT = process.argv[3] || '../temp-screenshots/path-chips'
mkdirSync(OUT, { recursive: true })

/** Must match useLineReveal's constants. */
const HOLD_MS = 1800
const FADE_MS = 1000

/**
 * Wall-clock sample cadence. These only decide WHEN to look; every reading is
 * bucketed by the animation's own reported time, so the exact spacing is not
 * load-bearing. Dense through the fade so there are ample partial readings.
 */
const SAMPLES = [0, 600, 1200, 1850, 2000, 2150, 2300, 2450, 2600, 2750]

/** Frames worth keeping: lit, mid-fade, nearly gone. */
const SHOOT_AT = new Set([0, 2150, 2450])

const alphaOf = (rgb) => {
  const m = /rgba?\(([^)]+)\)/.exec(rgb || '')
  if (!m) return null
  const parts = m[1].split(',').map(s => parseFloat(s.trim()))
  return parts.length === 4 ? parts[3] : 1
}

/** Reads the decoration's colour AND its animation clock in one round trip. */
const READ = () => {
  const el = document.querySelector('.mc-line-reveal')
  if (!el) return null
  const anims = (el.getAnimations ? el.getAnimations() : []).map(a => ({
    name: a.animationName ?? null,
    time: typeof a.currentTime === 'number' ? a.currentTime : null,
  }))
  return { bg: getComputedStyle(el).backgroundColor, anims }
}

const run = async (page) => {
  await page.goto(`${BASE}/capture/path-chips.html?scene=range&theme=dark`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.mc-line-reveal', { timeout: 15000 })

  const t0 = Date.now()
  const readings = []
  for (const at of SAMPLES) {
    const wait = at - (Date.now() - t0)
    if (wait > 0) await page.waitForTimeout(wait)
    const got = await page.evaluate(READ)
    readings.push({ at, ...(got ?? { bg: null, anims: [] }), alpha: alphaOf(got?.bg) })
    if (SHOOT_AT.has(at)) {
      const target = await page.$('[data-capture-root]')
      if (target) await target.screenshot({ path: `${OUT}/dark-fade-t${at}.png` })
    }
  }

  console.log('  wall   anim   alpha  background-color')
  for (const r of readings) {
    const t = r.anims[0]?.time
    console.log(`  ${String(r.at).padStart(5)}  ${t == null ? '   --' : String(Math.round(t)).padStart(5)}` +
      `  ${r.alpha == null ? ' gone' : r.alpha.toFixed(3)}  ${r.bg ?? '(element removed)'}`)
  }

  const failures = []
  const fail = (msg) => { failures.push(msg); console.error(`FAIL: ${msg}`) }

  const live = readings.filter(r => r.alpha != null && r.anims[0]?.time != null)
  if (!live.length) return fail('never observed the decoration with a running animation'), failures

  const lit = live[0].alpha
  if (!(lit > 0)) fail(`decoration was not lit on the first reading (alpha ${lit})`)

  // 1. THE HOLD. Every reading taken before the animation's delay elapsed must be
  //    at full strength. Without this, a regression that dropped the delay (or
  //    mis-ordered it in the `animation` shorthand) would still fade gradually and
  //    pass every other assertion here — the band would just never be seen lit.
  const held = live.filter(r => r.anims[0].time < HOLD_MS)
  if (held.length < 2) fail(`only ${held.length} reading(s) landed inside the ${HOLD_MS}ms hold; cannot judge it`)
  for (const r of held) {
    if (Math.abs(r.alpha - lit) > 1e-6) {
      fail(`alpha changed during the hold: ${r.alpha} at animation time ${Math.round(r.anims[0].time)}ms (expected ${lit})`)
    }
  }

  // 2. THE FADE, and that it is GRADUAL. An abrupt switch-off also ends up dimmer,
  //    so require monotonic non-increase plus at least two distinct partial values
  //    strictly between lit and zero. (A stepped fade would pass — sampling
  //    computed style cannot distinguish smooth from stepped. The keyframe is a
  //    plain two-stop interpolation, so stepping is not a reachable regression.)
  const alphas = live.map(r => r.alpha)
  for (let i = 1; i < alphas.length; i++) {
    if (alphas[i] > alphas[i - 1] + 1e-6) fail(`alpha rose (${alphas[i - 1]} -> ${alphas[i]})`)
  }
  const partials = new Set(alphas.filter(a => a > 0 && a < lit - 1e-6).map(a => a.toFixed(3)))
  if (partials.size < 2) {
    fail(`expected >=2 distinct partial alphas between ${lit} and 0, saw ${[...partials].join(', ') || 'none'}`)
  }
  const faded = live.filter(r => r.anims[0].time >= HOLD_MS)
  if (!faded.length) fail(`no reading landed inside the ${FADE_MS}ms fade window`)

  // 3. It must actually be GONE at the end, not merely dim.
  await page.waitForFunction(() => !document.querySelector('.mc-line-reveal'), { timeout: 4000 })
    .catch(() => fail('decoration still present well after the flash window'))

  // 4. A REPEAT reveal must relight, with a DIFFERENT animation name. Monaco reuses
  //    the rendered line node when the overlay HTML is unchanged, so re-adding an
  //    identical decoration leaves the finished animation applied and `forwards`
  //    pinning it transparent. Alternating the name is what restarts it, so assert
  //    the name really changed rather than only that colour came back.
  //    Dispatched rather than clicked: the control is parked offscreen so it cannot
  //    appear in the captured frames, and Playwright's click would wait for it to
  //    scroll into view.
  const firstName = live[0].anims[0]?.name ?? null
  await page.evaluate(() => {
    const btn = document.querySelector('[data-testid="reveal-again"]')
    if (btn) btn.click()
  })
  await page.waitForSelector('.mc-line-reveal', { timeout: 4000 })
  const again = await page.evaluate(READ)
  const relit = alphaOf(again?.bg)
  const secondName = again?.anims?.[0]?.name ?? null
  console.log(`  re-click: alpha ${relit == null ? 'gone' : relit.toFixed(3)}, animation ${firstName} -> ${secondName}`)
  if (relit == null || !(relit > 0)) fail('repeat reveal did not relight the highlight')
  if (secondName && firstName && secondName === firstName) {
    fail(`repeat reveal reused animation name ${secondName}; a reused node would not restart`)
  }

  if (!failures.length) {
    console.log(`OK: holds ${HOLD_MS}ms at alpha ${lit.toFixed(3)} (${held.length} readings), ` +
      `fades through ${partials.size} partial steps, clears, and relights under a new animation name`)
  }
  return failures
}

const main = async () => {
  const browser = await chromium.launch()
  let failures = ['probe did not complete']
  try {
    const ctx = await browser.newContext({ viewport: { width: 720, height: 420 }, deviceScaleFactor: 2 })
    failures = await run(await ctx.newPage())
  } finally {
    // Always, so a thrown assertion cannot leak the Chromium process.
    await browser.close()
  }
  if (failures.length) process.exit(1)
}

main().catch(err => { console.error(err); process.exit(1) })
