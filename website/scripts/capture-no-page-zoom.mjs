/**
 * Evidence for "the shell does not page-zoom, and the image viewer pinches itself".
 *
 * Two claims, two isolated entries, one script — because they are one design
 * decision seen from both ends: page zoom is withheld app-wide, so the surface
 * that must magnify does it on its own transform.
 *
 *   shell    capture/no-page-zoom.html, which installs the SHIPPED viewport meta
 *            read out of index.html. A genuine two-finger spread must leave
 *            `visualViewport.scale` at 1, while `?zoomable=1` — the same page with
 *            a permissive meta — must zoom. The control is not optional: without a
 *            run that DOES zoom, a scale of 1 could just mean the harness never
 *            managed to pinch anything.
 *
 *   viewer   capture/lightbox-swipe.html, the real `Lightbox` opened through the
 *            production `lightbox` event. A spread must scale the <img> up, and a
 *            pinch back in must return it to fit — proving the viewer's own
 *            gesture, not the browser's, is what magnifies.
 *
 * Gestures are injected as real touch frames via CDP Input.dispatchTouchEvent with
 * TWO touch points. A mouse or a single point would prove nothing: the shell rule
 * is about a multi-touch browser gesture, and the viewer's pinch arms only on a
 * second contact.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-no-page-zoom.mjs http://127.0.0.1:6841 ../temp-screenshots/no-page-zoom
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/no-page-zoom'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 390, height: 844 }
const CENTRE = { x: 195, y: 422 }

/** Dispatch a two-finger frame `gap`px apart, centred on `at` (default CENTRE). */
async function pinchFrame(cdp, type, gap, at = CENTRE) {
  await cdp.send('Input.dispatchTouchEvent', {
    type,
    touchPoints: type === 'touchEnd' ? [] : [
      { x: at.x, y: at.y - gap / 2, id: 1 },
      { x: at.x, y: at.y + gap / 2, id: 2 },
    ],
  })
}

/** Walk the finger gap from `from` to `to` over `steps` frames, ~16ms apart so the
 *  injected gesture arrives at roughly a real touch frame rate. */
async function pinch(page, cdp, from, to, steps = 16, onFrame, at = CENTRE) {
  await pinchFrame(cdp, 'touchStart', from, at)
  for (let i = 1; i <= steps; i++) {
    await pinchFrame(cdp, 'touchMove', from + ((to - from) * i) / steps, at)
    await page.waitForTimeout(16)
    if (onFrame) await onFrame(i)
  }
}

async function context(video = false) {
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    ...(video ? { recordVideo: { dir: OUT, size: VIEWPORT } } : {}),
  })
  const page = await ctx.newPage()
  return { ctx, page, cdp: await ctx.newCDPSession(page) }
}

const browser = await chromium.launch()

// ── the shell ─────────────────────────────────────────────────────────────────

/** Spread two fingers on the shell page and report the scale the ENGINE ends at. */
async function shellRun({ name, zoomable }) {
  const { ctx, page, cdp } = await context()
  await page.goto(`${BASE}/capture/no-page-zoom.html${zoomable ? '?zoomable=1' : ''}`)
  await page.waitForFunction(() => typeof window.__zoom === 'function', { timeout: 20000 })
  await page.waitForTimeout(300)

  const before = await page.evaluate(() => window.__zoom())
  if (before.scale !== 1) throw new Error(`${name}: page already zoomed at ${before.scale}`)
  await page.screenshot({ path: `${OUT}/shell-${name}-01-before.png` })

  await pinch(page, cdp, 80, 620, 24)
  await pinchFrame(cdp, 'touchEnd', 620)
  await page.waitForTimeout(400)

  const after = await page.evaluate(() => window.__zoom())
  await page.screenshot({ path: `${OUT}/shell-${name}-02-after-spread.png` })
  console.log(`shell/${name.padEnd(8)} scale ${before.scale} → ${after.scale} · touch-action ${after.touchAction} · meta "${after.viewport}"`)
  await ctx.close()
  return after
}

// The CONTROL first: if a permissive meta does not zoom, the harness cannot see
// zoom and the subject run below would be vacuously green.
const control = await shellRun({ name: 'zoomable', zoomable: true })
if (control.scale <= 1) throw new Error(`control: a permissive viewport did not zoom (scale ${control.scale}) — the harness cannot detect page zoom, so nothing here is evidence`)

const subject = await shellRun({ name: 'shipped', zoomable: false })
if (subject.scale !== 1) throw new Error(`shipped: the shell zoomed to ${subject.scale}`)
if (subject.touchAction !== 'pan-x pan-y') throw new Error(`shipped: root touch-action resolved to "${subject.touchAction}", expected "pan-x pan-y"`)

// Scrolling must survive: `touch-action: none` would also pass the zoom assertion
// above while making the whole app undraggable.
{
  const { ctx, page, cdp } = await context()
  await page.goto(`${BASE}/capture/no-page-zoom.html`)
  await page.waitForFunction(() => typeof window.__zoom === 'function', { timeout: 20000 })
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 195, y: 700, id: 1 }] })
  for (let y = 700; y >= 300; y -= 50) {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: 195, y, id: 1 }] })
    await page.waitForTimeout(16)
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  await page.waitForTimeout(400)
  const scrolled = await page.evaluate(() => window.scrollY)
  await page.screenshot({ path: `${OUT}/shell-scroll-after.png` })
  console.log(`shell/scroll   scrollY ${scrolled}`)
  if (scrolled <= 0) throw new Error('the page did not scroll — pan-x pan-y must keep both scroll axes')
  await ctx.close()
}

// ── the image viewer ──────────────────────────────────────────────────────────

{
  const { ctx, page, cdp } = await context(true)
  await page.goto(`${BASE}/capture/lightbox-swipe.html?theme=light`)
  await page.waitForSelector('[role="button"].fixed.inset-0', { timeout: 20000 })
  await page.waitForTimeout(500)

  const atRest = await page.evaluate(() => window.__swipe())
  if (!atRest.open) throw new Error('viewer: did not open')
  await page.screenshot({ path: `${OUT}/viewer-01-at-rest.png` })

  let mid = null
  await pinch(page, cdp, 120, 560, 20, async i => {
    if (i === 10) {
      mid = await page.evaluate(() => window.__swipe())
      await page.screenshot({ path: `${OUT}/viewer-02-mid-pinch.png` })
    }
  })
  const spread = await page.evaluate(() => window.__swipe())
  await page.screenshot({ path: `${OUT}/viewer-03-zoomed.png` })
  if (spread.image === atRest.image) throw new Error(`viewer: the spread did not scale the image (still ${spread.image})`)
  if (!mid || mid.image === atRest.image) throw new Error('viewer: the image did not track the fingers mid-gesture')

  // Pinch back in from the spread gap: the viewer must return to fit rather than
  // latching at whatever the spread reached.
  await pinch(page, cdp, 560, 100, 20)
  await pinchFrame(cdp, 'touchEnd', 100)
  await page.waitForTimeout(400)
  const back = await page.evaluate(() => window.__swipe())
  await page.screenshot({ path: `${OUT}/viewer-04-back-to-fit.png` })
  console.log(`viewer         rest ${atRest.image} · spread ${spread.image} · back ${back.image}`)
  if (!back.open) throw new Error('viewer: pinching back in closed the viewer — a pinch must never read as a dismiss')
  if (back.image !== atRest.image) throw new Error(`viewer: did not return to fit, image is ${back.image}`)

  // Focal-point anchoring, which ONLY real layout can show: jsdom reports
  // offsetWidth 0, so the pan clamp pins every offset to 0 there and a unit test
  // cannot tell an anchored zoom from a centre-anchored one. Pinching well off
  // centre must move the image (a non-zero translate), because holding the content
  // under the fingers is exactly what a centre-anchored scale fails to do.
  const OFF_CENTRE = { x: 110, y: 240 }
  await pinch(page, cdp, 120, 520, 16, undefined, OFF_CENTRE)
  await pinchFrame(cdp, 'touchEnd', 520, OFF_CENTRE)
  await page.waitForTimeout(300)
  const offCentre = await page.evaluate(() => window.__swipe())
  await page.screenshot({ path: `${OUT}/viewer-05-anchored.png` })
  const translated = /matrix\([^)]*?,\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)$/.exec(offCentre.image)
  const dx = translated ? Math.abs(parseFloat(translated[1])) : 0
  const dy = translated ? Math.abs(parseFloat(translated[2])) : 0
  console.log(`viewer         off-centre pinch ${offCentre.image} · translate ${dx.toFixed(1)},${dy.toFixed(1)}`)
  if (dx + dy < 1) throw new Error(`viewer: an off-centre pinch produced no pan (${offCentre.image}) — the zoom is anchored at the element centre, so a detail under the fingers slides away from them`)

  const videoPath = await page.video().path()
  await ctx.close()
  console.log(`viewer         video ${videoPath}`)
}

await browser.close()
console.log('files:', readdirSync(OUT).join(' '))
