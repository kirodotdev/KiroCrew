/**
 * Evidence for swipe-down-to-dismiss on the image viewer.
 *
 * Drives the ISOLATED capture entry (website/capture/lightbox-swipe.html), which
 * mounts the REAL `Lightbox` opened through the same `lightbox` CustomEvent
 * production dispatches.
 *
 * The gesture is injected as a GENUINE touch sequence via CDP
 * Input.dispatchTouchEvent, not a mouse drag: the handler is gated on
 * `pointerType !== 'mouse'`, so a synthetic mouse drag would prove nothing. The
 * dispatched touches make Chromium synthesize the same pointerdown/move/up with
 * `pointerType: 'touch'` a phone produces.
 *
 * Two runs, because the interesting states are motion and a threshold:
 *
 *   dismiss     390px, coarse pointer — a slow 320px pull released past the 96px
 *               threshold. Recorded as video (the whole point is that the image
 *               tracks the finger), plus stills at rest / mid-drag / dismissed.
 *   springback  the same viewport, a 60px pull released short of the threshold.
 *               Stills only: the image returns and the viewer stays open.
 *
 * Each run asserts the live transform and backdrop through window.__swipe(), so a
 * regression that renders a plausible frame without moving anything fails here
 * rather than in review.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-lightbox-swipe.mjs http://127.0.0.1:6841 ../temp-screenshots/lightbox-swipe
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/lightbox-swipe'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 390, height: 844 }
const START = { x: 195, y: 300 }

/** Dispatch one touch frame. `points` empty ends the sequence (touchEnd). */
async function touch(cdp, type, y) {
  await cdp.send('Input.dispatchTouchEvent', {
    type,
    touchPoints: type === 'touchEnd' ? [] : [{ x: START.x, y, id: 1 }],
  })
}

/** Pull the finger from START.y down by `distance`px over `steps` frames, holding
 *  ~16ms between frames so the injected gesture arrives at roughly a real touch
 *  frame rate rather than as one teleporting jump. */
async function drag(page, cdp, distance, steps, onFrame) {
  await touch(cdp, 'touchStart', START.y)
  for (let i = 1; i <= steps; i++) {
    await touch(cdp, 'touchMove', START.y + Math.round((distance * i) / steps))
    await page.waitForTimeout(16)
    if (onFrame) await onFrame(i)
  }
}

async function run({ name, distance, steps, video }) {
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    ...(video ? { recordVideo: { dir: OUT, size: VIEWPORT } } : {}),
  })
  const page = await ctx.newPage()
  const cdp = await ctx.newCDPSession(page)
  await page.goto(`${BASE}/capture/lightbox-swipe.html?theme=light`)
  await page.waitForSelector('[role="button"].fixed.inset-0', { timeout: 20000 })
  // Let the subject image decode and the open transition settle before the
  // at-rest frame, so "no transform" is a real reading rather than a race.
  await page.waitForTimeout(500)

  const atRest = await page.evaluate(() => window.__swipe())
  if (!atRest.open) throw new Error(`${name}: the viewer did not open`)
  if (atRest.transform !== 'none') throw new Error(`${name}: expected no transform at rest, got ${atRest.transform}`)
  await page.screenshot({ path: `${OUT}/${name}-01-at-rest.png` })

  let mid = null
  await drag(page, cdp, distance, steps, async i => {
    if (i === Math.round(steps / 2)) {
      mid = await page.evaluate(() => window.__swipe())
      await page.screenshot({ path: `${OUT}/${name}-02-mid-drag.png` })
    }
  })
  if (!mid || mid.transform === 'none') throw new Error(`${name}: the image did not move during the drag`)
  if (mid.backdrop === atRest.backdrop) throw new Error(`${name}: the backdrop did not fade during the drag`)

  await touch(cdp, 'touchEnd', START.y + distance)
  await page.waitForTimeout(500)
  const after = await page.evaluate(() => window.__swipe())
  await page.screenshot({ path: `${OUT}/${name}-03-released.png` })
  console.log(`${name.padEnd(11)} pull ${distance}px · mid ${mid.transform} · after release ${after.open ? 'still open' : 'dismissed'}`)

  const videoPath = video ? await page.video().path() : null
  await ctx.close() // flushes the video file
  if (videoPath) {
    const webm = join(OUT, `${name}.webm`)
    renameSync(videoPath, webm)
    console.log(`${name.padEnd(11)} video ${webm}`)
  }
  return after
}

const browser = await chromium.launch()

// Past the 96px threshold: the viewer must be gone.
const dismissed = await run({ name: 'dismiss', distance: 320, steps: 20, video: true })
if (dismissed.open) throw new Error('dismiss: a 320px pull left the viewer open')

// Short of it: the image must spring back and the viewer stay open.
const sprung = await run({ name: 'springback', distance: 60, steps: 8, video: false })
if (!sprung.open) throw new Error('springback: a 60px pull dismissed the viewer')
if (sprung.transform !== 'none') throw new Error(`springback: the image did not return, transform is ${sprung.transform}`)

await browser.close()
console.log('files:', readdirSync(OUT).join(' '))
