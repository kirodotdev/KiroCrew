/**
 * Deterministic layout-settle for the render gate (issue #1555).
 *
 * ## Why this exists
 *
 * The gate used to wait a FIXED time (`waitForTimeout(surface.settle || 250)` plus a bare
 * double `requestAnimationFrame`) and then read `scrollWidth`/`clientWidth`. framer-motion
 * drives width and layout animations in JavaScript -- a `requestAnimationFrame` loop writing
 * inline `style.width` -- NOT as CSS transitions, so neither the browser context's
 * `reducedMotion: 'reduce'` nor a `* { transition: none }` injection stops them for a
 * component that does not itself call `useReducedMotion()`. Most do not, and there is no
 * global `<MotionConfig>`. A fixed wall-clock wait therefore RACED those springs (and the
 * webfont reflow): whichever frame it landed on was the width that got bucketed as `layout`,
 * so the count flaked run to run and failed unrelated PRs at random.
 *
 * This waits for the GEOMETRY ITSELF to go quiescent instead of guessing a duration. It is
 * animation-engine-agnostic (springs, tweens, layout/layoutId morphs, webfont reflow, late
 * async panels) and measures the settled layout a user actually sees.
 *
 * `waitForLayoutStable` is pure and its `raf`/`now` are injectable, so it is unit-tested
 * with a fake clock (src/test/renderGateSettle.test.ts). `geometrySignature` reads only
 * width/height BOX metrics, so transform/opacity-only motion (spinners, caret blink, pulses)
 * cannot keep it from settling, while the width/height changes the scanner measures do.
 */

/**
 * Resolve once `sample()` returns a value byte-identical for `framesStable` consecutive
 * animation frames AND at least `minMs` has elapsed, or after `maxFrames` frames.
 *
 * `minMs` is the async-mount FLOOR: without it, a page that is trivially static BEFORE its
 * animation starts reads as already-quiescent and gets sampled in its not-yet-animated
 * state (observed empirically). `maxFrames` is a hard ceiling so an unending animation
 * degrades to a measurement rather than hanging CI.
 *
 * @param {() => (string|number)} sample geometry signature to watch
 * @param {{framesStable?:number, maxFrames?:number, minMs?:number, raf?:Function, now?:Function}} [opts]
 * @returns {Promise<{reason:'stable'|'cap', frames:number}>}
 */
export function waitForLayoutStable(sample, opts = {}) {
  const framesStable = opts.framesStable ?? 4
  const maxFrames = opts.maxFrames ?? 300
  const minMs = opts.minMs ?? 0
  const raf = opts.raf || (cb => requestAnimationFrame(cb))
  const now = opts.now || (() => performance.now())
  return new Promise((resolve) => {
    const t0 = now()
    let prev = null
    let stable = 0
    let frames = 0
    const tick = () => {
      const cur = sample()
      if (cur === prev) stable++
      else { stable = 0; prev = cur }
      frames++
      if (stable >= framesStable && (now() - t0) >= minMs) return resolve({ reason: 'stable', frames })
      if (frames >= maxFrames) return resolve({ reason: 'cap', frames })
      raf(tick)
    }
    raf(tick)
  })
}

/**
 * A signature of the document's layout geometry: width/height box metrics only, as
 * integers, one triple per element. Deliberately NOT transform/opacity (those do not change
 * these box metrics), so infinite spinners/pulses cannot prevent quiescence, while the
 * width/height animations the scanner actually measures do move it.
 *
 * The element set MIRRORS the scanner's own exclusions (render-scan.mjs ~315-338, 538-539):
 * geometry the scanner never reads must not be able to hold the settle open. Virtuoso
 * virtual lists re-measure an oversized inner container continuously; hidden and opaque
 * (code/terminal/mono) subtrees are skipped by the scan. We use only the cheap
 * closest()/checkVisibility() tests here (not the scanner's per-element computed-font read)
 * so this stays affordable to run every animation frame, and we exclude strictly LESS than
 * the scanner in every ambiguous case, so we never stop watching something it measures.
 */
export function geometrySignature(root = document) {
  const body = root.body || root
  const els = body.querySelectorAll('*')
  let s = ''
  for (let i = 0; i < els.length; i++) {
    const e = els[i]
    if (e.closest('[data-testid="virtuoso"], code, pre, kbd, samp, [data-i18n-opaque]')) continue
    if (typeof e.checkVisibility === 'function' && !e.checkVisibility()) continue
    s += (e.clientWidth | 0) + ',' + (e.scrollWidth | 0) + ',' + (e.offsetHeight | 0) + ';'
  }
  return s
}

// __BROWSER_BUNDLE_CUT__

/**
 * Rewrite this module into a plain script for `page.addScriptTag`, exposing the two
 * browser-safe helpers on `window.__I18N_SETTLE`. Mirrors `browserBundle` in
 * lib/render-scan.mjs exactly: one implementation, two runtimes, no drift.
 */
export function browserBundleSettle(source) {
  const body = source.split('// __BROWSER_BUNDLE_CUT__')[0]
    .replace(/^export (function|const|class)/gm, '$1')
  return `(() => {\n${body}\nwindow.__I18N_SETTLE = { waitForLayoutStable, geometrySignature };\n})()`
}
