/**
 * Measures the sketch pad's pointer-mapping error, and screenshots the proof.
 *
 * Three scenes against the SAME build, which is what makes the reading a
 * measurement rather than an assertion of the diagnosis:
 *
 *   cold   — first open, lazy Excalidraw chunk still on the wire. The load
 *            outlasts the 200ms dialog animation, so the offsets are captured
 *            after it settles and the delta is ~0. This is why the bug looks
 *            intermittent.
 *   warm   — chunk pre-resolved, so the pad mounts in the SAME commit that
 *            starts the enter animation: every open after the first one in
 *            production. This is the broken case.
 *   noanim — warm, with animations forced to 0s. Isolates the animation as the
 *            cause: same mount timing, no transform, delta must be ~0.
 *
 * A drag is then performed with the real mouse and the created element's screen
 * position compared with where the pointer actually went, so the offset is
 * confirmed end-to-end and not only in internal state.
 *
 * The run ASSERTS its readings rather than only printing them: every scene must
 * measure a delta under half a pixel, or the process exits non-zero. Pass
 * `--expect-bug` when running against the base branch — the `warm` scene must
 * then measure a REAL offset, so a before/after pair cannot be two runs of the
 * same build.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6931 --strictPort   # in another shell
 *   node scripts/capture-sketch-cursor-offset.mjs http://127.0.0.1:6931 ../temp-screenshots/sketch-offset
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const argv = process.argv.slice(2)
const expectBug = argv.includes('--expect-bug')
const positional = argv.filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6931'
const OUT = positional[1] || '../temp-screenshots/sketch-offset'
mkdirSync(OUT, { recursive: true })

const NO_ANIM = '*, *::before, *::after { animation-duration: 0s !important;'
  + ' animation-delay: 0s !important; transition-duration: 0s !important;'
  + ' transition-delay: 0s !important; }'

const browser = await chromium.launch()

/** One scene: returns the measured probe plus the drag check. */
async function run({ warm, noanim }) {
  const page = await browser.newPage({ viewport: { width: 1512, height: 900 }, deviceScaleFactor: 1 })
  page.on('pageerror', e => console.error('  page error:', e.message))
  await page.goto(`${BASE}/capture/sketch-cursor-offset.html`)
  if (noanim) await page.addStyleTag({ content: NO_ANIM })
  await page.waitForFunction(() => typeof window.__open === 'function')
  if (warm) await page.evaluate(() => window.__warm())
  await page.evaluate(() => window.__open())
  await page.waitForSelector('canvas.interactive', { timeout: 15000 })
  // Well past the 200ms enter animation: whatever the pad believes now is what
  // it will believe for the rest of its life.
  await page.waitForTimeout(1200)
  const probe = await page.evaluate(() => window.__probe())

  // End-to-end: draw a rectangle with the real mouse between two viewport
  // points, then ask where it landed on screen.
  let drag = null
  if (probe) {
    const a = { x: Math.round(probe.actual.left + 300), y: Math.round(probe.actual.top + 260) }
    const b = { x: a.x + 200, y: a.y + 120 }
    // Excalidraw binds keydown on its OWN container (handleKeyboardGlobally is
    // off by default), and the dialog autofocuses the header CTA — so focus the
    // canvas the way a user does before reaching for a tool shortcut.
    await page.mouse.click(a.x, a.y)
    await page.keyboard.press('r')
    await page.mouse.move(a.x, a.y)
    await page.mouse.down()
    await page.mouse.move(b.x, b.y, { steps: 12 })
    await page.mouse.up()
    await page.waitForTimeout(200)
    drag = await page.evaluate(({ a, b }) => {
      const el = (window.h?.elements ?? []).filter(e => !e.isDeleted).at(-1)
      if (!el) return null
      const s = window.h.state
      // scene -> viewport, using the TRUE container origin, i.e. where the shape
      // is actually painted on screen.
      const container = document.querySelector('.excalidraw').getBoundingClientRect()
      const z = s.zoom.value
      const screenX = (el.x + s.scrollX) * z + container.left
      const screenY = (el.y + s.scrollY) * z + container.top
      return {
        pointerFrom: a,
        pointerTo: b,
        paintedAt: { x: Math.round(screenX * 100) / 100, y: Math.round(screenY * 100) / 100 },
        errorX: Math.round((screenX - a.x) * 100) / 100,
        errorY: Math.round((screenY - a.y) * 100) / 100,
      }
    }, { a, b })
  }

  const name = noanim ? 'noanim' : warm ? 'warm' : 'cold'
  await page.screenshot({ path: `${OUT}/${name}.png` })
  // A legible frame: mark the exact viewport point the pointer went down on, and
  // crop tight around it. The drawn rectangle's corner sits ON the mark when the
  // mapping is right and BESIDE it when it is not — the offset is a few pixels,
  // so only a crop this tight shows it.
  if (probe) {
    const a = { x: Math.round(probe.actual.left + 300), y: Math.round(probe.actual.top + 260) }
    await page.evaluate(({ x, y }) => {
      const mark = document.createElement('div')
      mark.style.cssText = `position:fixed;left:${x - 9}px;top:${y}px;width:18px;height:1px;`
        + 'background:#ff2d55;z-index:99999;pointer-events:none'
      const v = document.createElement('div')
      v.style.cssText = `position:fixed;left:${x}px;top:${y - 9}px;width:1px;height:18px;`
        + 'background:#ff2d55;z-index:99999;pointer-events:none'
      document.body.append(mark, v)
    }, a)
    await page.screenshot({
      path: `${OUT}/${name}-corner.png`,
      clip: { x: a.x - 40, y: a.y - 30, width: 120, height: 90 },
    })
  }
  await page.close()
  return { name, probe, drag }
}

/** The load-failure surface, plus what the browser does with a failed chunk.
 *
 * Blocks every Excalidraw chunk request so the lazy import rejects, which is
 * what an offline first open looks like. This scene ASSERTS one thing — that the
 * failed state renders as a real error surface carrying the agent hand-off —
 * which is also the frame a UX reviewer needs.
 *
 * It deliberately asserts NOTHING about the retry, because a browser cannot see
 * it. Measured: after unblocking, a reopen makes ZERO network requests and logs
 * a boundary throw either way, whether the lazy component is minted per open or
 * shared at module scope. A failed module-script fetch is recorded in the
 * browser's module map for the document's lifetime (whatwg/html#6768; Chromium's
 * "don't cache HTTP errors in the module map" issue is still open), so the same
 * URL is never refetched on this page load and both arms look identical from
 * out here. An earlier version of this scene asserted "reopening re-ran the
 * loader" and passed against a deliberately reverted fix — a vacuous guard.
 * The React layer (a rejected `lazy` payload is cached on the component object,
 * so a shared lazy never calls its factory again) is pinned where it IS
 * observable, by the jsdom test `retries a failed load when the dialog is
 * reopened`.
 *
 * Reloading is the way back on this page load, which is what the failure
 * message tells the user to do. Do NOT "fix" that by cache-busting the
 * specifier: `import('@excalidraw/excalidraw')` is a bare specifier Vite
 * resolves at build time, and making it dynamic to append a query would break
 * the chunking this pad depends on.
 */
async function runLoadFailure() {
  const page = await browser.newPage({ viewport: { width: 1512, height: 900 }, deviceScaleFactor: 1 })
  let excalidrawRequestsAfterUnblock = 0
  let blocking = true
  await page.route('**/*', route => {
    const url = route.request().url()
    if (/excalidraw/i.test(url)) {
      if (blocking) return route.abort()
      excalidrawRequestsAfterUnblock += 1
    }
    return route.continue()
  })
  await page.goto(`${BASE}/capture/sketch-cursor-offset.html`)
  await page.waitForFunction(() => typeof window.__open === 'function')
  await page.evaluate(() => window.__open())
  // The failure surface: ErrorNotice's role="alert" plus the hand-off button.
  const alert = page.getByRole('alert')
  let fallbackShown = false
  let message = ''
  try {
    await alert.waitFor({ timeout: 15000 })
    fallbackShown = true
    message = (await alert.innerText()).replace(/\s+/g, ' ').trim()
  } catch { /* reported below */ }
  const handoff = await page.getByRole('button', { name: /ask the agent/i }).count()
  // Read the HEADER before writing the frame. This scene's PNG is the evidence a
  // UX reviewer judges the failed state on, and a frame captured from a build
  // where the hint was still showing is worse than no frame — it depicts a state
  // the shipped code cannot paint. That happened once: a capture taken before
  // the hint suppression landed was carried into the PR body and the UX lane
  // blocked on the contradiction. So the hint's absence is measured here, and
  // `hintLeaked` fails the run rather than quietly shipping the frame.
  const hintLeaked = await page.getByText('Draw something first').count() > 0
  await page.screenshot({ path: `${OUT}/load-failure.png` })

  // Observation only, to keep the module-map behaviour on the record.
  blocking = false
  await page.evaluate(() => window.__close())
  await page.waitForTimeout(400)
  await page.evaluate(() => window.__open())
  await page.waitForTimeout(2500)
  const recovered = await page.locator('canvas.interactive').count() > 0
  await page.close()
  return { name: 'load-failure', fallbackShown, message, handoff, hintLeaked, recovered, excalidrawRequestsAfterUnblock }
}

const results = []
results.push(await run({ warm: false, noanim: false }))
results.push(await run({ warm: true, noanim: false }))
results.push(await run({ warm: true, noanim: true }))
const failure = await runLoadFailure()

await browser.close()

/** Half a pixel: sub-pixel rect arithmetic is exact here (all three scenes read
 *  identical numbers), so anything above this is a real mapping error. */
const TOLERANCE = 0.5
const worst = r => (r.probe
  ? Math.max(...Object.values(r.probe.delta).map(Math.abs))
  : Number.POSITIVE_INFINITY)

let failed = false
for (const r of results) {
  console.log(`\n--- ${r.name} ---`)
  if (!r.probe) {
    console.log('  probe unavailable — Excalidraw\'s dev test hook is missing.'
      + ' This harness needs the DEV build (run it against `npx vite`, not a prod bundle).')
    failed = true
    continue
  }
  console.log('  believed:', JSON.stringify(r.probe.believed))
  console.log('  actual  :', JSON.stringify(r.probe.actual))
  console.log('  DELTA   :', JSON.stringify(r.probe.delta))
  if (r.drag) {
    console.log(`  drag: pointer down at (${r.drag.pointerFrom.x},${r.drag.pointerFrom.y}),`
      + ` shape painted at (${r.drag.paintedAt.x},${r.drag.paintedAt.y})`
      + ` -> off by (${r.drag.errorX},${r.drag.errorY})`)
  } else {
    console.log('  drag: no element created — the scene proves nothing end to end')
    failed = true
  }
}

const warm = results.find(r => r.name === 'warm')
if (expectBug) {
  // Base-branch run: the broken scene must actually be broken, otherwise this is
  // not the base build and a "before" frame would be a lie.
  if (worst(warm) <= TOLERANCE) {
    console.error(`\nFAIL: --expect-bug, but the warm scene measured no offset`
      + ` (worst ${worst(warm)}px). This build is not the unfixed one.`)
    process.exit(1)
  }
  console.log(`\nbase build confirmed: warm scene is off by up to ${worst(warm)}px`)
} else {
  for (const r of results) {
    if (worst(r) > TOLERANCE) {
      console.error(`FAIL: scene "${r.name}" maps pointers ${worst(r)}px off`
        + ` (tolerance ${TOLERANCE}px)`)
      failed = true
    }
    if (r.drag && Math.max(Math.abs(r.drag.errorX), Math.abs(r.drag.errorY)) > TOLERANCE) {
      console.error(`FAIL: scene "${r.name}" painted the shape`
        + ` (${r.drag.errorX},${r.drag.errorY}) from the pointer`)
      failed = true
    }
  }
  if (!failed) console.log('\nall scenes: pointer maps to the cursor within half a pixel')
}

console.log('\n--- load-failure ---')
console.log(`  failure surface rendered: ${failure.fallbackShown}`)
console.log(`  message: ${failure.message}`)
console.log(`  "Ask the agent" hand-off buttons: ${failure.handoff}`)
console.log(`  header hint leaked into the failed state: ${failure.hintLeaked}`)
console.log(`  [observed] chunk requests after unblocking: ${failure.excalidrawRequestsAfterUnblock}`
  + `  (0 = the browser served its cached module-map failure)`)
console.log(`  [observed] pad recovered on this page load: ${failure.recovered}`
  + `  (false in Chromium by design of the module map — reloading is the way back)`)
if (!expectBug) {
  // The one assertion here, and the reason the screenshot is evidence of
  // anything: the failed state must be REACHABLE, must be a real error surface
  // (role="alert"), and must carry the hand-off. Swap ErrorNotice back for a
  // muted line of text and the hand-off count goes to 0 and this fails.
  if (!failure.fallbackShown || failure.handoff < 1) {
    console.error('FAIL: the load-failure surface did not render with its hand-off')
    failed = true
  }
  // The header must not contradict the error beside it. A "Draw something
  // first" hint over "the pad never loaded" is unsatisfiable, and a frame
  // showing both is evidence of a state that cannot ship.
  if (failure.hintLeaked) {
    console.error('FAIL: "Draw something first" is showing in the failed-load state —'
      + ' the captured frame contradicts itself and must not be used as evidence')
    failed = true
  }
  // The message must name the remedy that WORKS on this page load. Reopening
  // cannot recover a cached module-map failure, so copy telling the user to
  // reopen would be a false instruction.
  if (failure.fallbackShown && /reopen/i.test(failure.message)) {
    console.error(`FAIL: the failure message tells the user to reopen, which cannot`
      + ` recover a cached module-map failure: "${failure.message}"`)
    failed = true
  }
}

console.log(`wrote ${results.length} screenshots to ${OUT}`)
if (failed) process.exit(1)
