/**
 * Recording + still harness for the mobile sessions-drawer gesture.
 *
 * WHAT IT PROVES, and why a still cannot. The change replaces a touchend
 * threshold detector (`useSwipeEdge`) with a drag that tracks the finger
 * (`useDrawerSwipe`). The whole delta is in the frames BETWEEN touchstart and
 * touchend — a before/after still of the closed and open states is identical
 * across the change. So this drives a REAL touch sequence and asserts on the
 * panel's measured offset at each step, then records the same flow to video.
 *
 * Real input, not synthesised events: touches go through Playwright's
 * `hasTouch` context, so the browser dispatches them the way a phone does. A
 * `page.evaluate` that constructed TouchEvents by hand would prove only that
 * the hook responds to objects the harness made up.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures,
 * the same gateway-free technique as every other capture script here. The
 * gesture is entirely client-side and reads nothing from the API, so the stub
 * is not standing in for any part of the code under test.
 *
 * 390x844 is the narrow-viewport baseline in AUTOSDE, and below useIsMobile's
 * 768px breakpoint, which is what puts the drawer in gesture mode at all.
 *
 * Usage: node scripts/capture-mobile-drawer-gesture.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mobile-drawer-gesture'
const VIEW = { width: 390, height: 844 }
mkdirSync(OUT, { recursive: true })
mkdirSync(join(OUT, 'video'), { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slot = (key, title, minsAgo) => ({
  key, title, running: false, last_message: '', messages: 6, agent: 'kirocrew',
  memory_mode: 'persistent', folder_id: '', modified: now - minsAgo * 60,
  source_links: [], source_links_total: 0,
})
const SLOTS = [
  slot('chat-drawer-a', 'Mobile drawer gesture', 2),
  slot('chat-drawer-b', 'Safe-area insets on iOS', 48),
  slot('chat-drawer-c', 'Native picker for Settings', 190),
  slot('chat-drawer-d', 'Video upload pipeline', 1450),
]

/** The drawer panel. Its transform is the value the gesture drives. */
const PANEL = '.mobile-sessions-overlay'

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

/** Measured x offset of the panel, in px. 0 = at rest, negative = offscreen. */
async function offsetOf(page) {
  return page.evaluate(sel => {
    const el = document.querySelector(sel)
    if (!el) return null
    // Read the composited matrix rather than the style string: the value is
    // written by a MotionValue, so `style.transform` may lag a frame.
    const m = new DOMMatrixReadOnly(getComputedStyle(el).transform)
    return Math.round(m.m41)
  }, PANEL)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
    recordVideo: { dir: join(OUT, 'video'), size: VIEW },
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    slots: SLOTS,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/history')) { await json(route, { messages: [] }); return true }
      if (path === '/api/agents') { await json(route, { agents: [{ name: 'kirocrew' }], default_agent: 'kirocrew' }); return true }
      if (path === '/api/models') { await json(route, []); return true }
      return false
    },
  })
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="chat-input"], textarea', { timeout: 20000 })
  await page.waitForTimeout(600)

  const cdp = await context.newCDPSession(page)
  /** One real touch point, dispatched the way the platform does. */
  const touch = async (type, x, y) => {
    await cdp.send('Input.dispatchTouchEvent', {
      type,
      touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }],
    })
  }

  const Y = 420           // mid-pane, clear of the header and the composer
  const START = 60        // inside the drawer's band, outboard of the OS strip
  const closed = -VIEW.width

  if (await offsetOf(page) !== null) fail('drawer is mounted before any gesture')
  await page.screenshot({ path: join(OUT, '01-closed.png') })

  // ── A partial drag, held ────────────────────────────────────────────────
  // The frame the predecessor could not produce: the panel sitting where the
  // finger left it, with the scrim dimmed in proportion.
  await touch('touchStart', START, Y)
  for (const x of [START + 12, START + 60, START + 120, START + 150]) {
    await touch('touchMove', x, Y)
    await page.waitForTimeout(40)
  }
  const partial = await offsetOf(page)
  if (partial === null) fail('panel never mounted during the drag')
  else if (partial <= closed + 20 || partial >= -20) {
    fail(`panel is not tracking the finger — offset ${partial}, expected between ${closed + 20} and -20`)
  }
  await page.screenshot({ path: join(OUT, '02-partial-drag.png') })

  // ── Drag back and release: the gesture cancels ──────────────────────────
  for (const x of [START + 90, START + 30, START + 4]) {
    await touch('touchMove', x, Y)
    await page.waitForTimeout(40)
  }
  await touch('touchEnd', START + 4, Y)
  await page.waitForTimeout(600)
  if (await offsetOf(page) !== null) fail('a cancelled drag left the drawer open')
  await page.screenshot({ path: join(OUT, '03-cancelled.png') })

  // ── Drag past halfway and release: the gesture commits ──────────────────
  await touch('touchStart', START, Y)
  for (const x of [START + 20, START + 90, START + 180, START + 260, START + 310]) {
    await touch('touchMove', x, Y)
    await page.waitForTimeout(40)
  }
  await touch('touchEnd', START + 310, Y)
  await page.waitForTimeout(700)
  const settled = await offsetOf(page)
  if (settled === null) fail('committed drag did not leave the drawer mounted')
  else if (Math.abs(settled) > 2) fail(`drawer settled at ${settled}, expected 0`)
  await page.screenshot({ path: join(OUT, '04-open.png') })

  // ── Close it the same way, in the other direction ───────────────────────
  await touch('touchStart', 320, Y)
  for (const x of [300, 220, 120, 40]) {
    await touch('touchMove', x, Y)
    await page.waitForTimeout(40)
  }
  const closing = await offsetOf(page)
  if (closing === null || closing >= -80) fail(`close drag is not tracking — offset ${closing}`)
  await page.screenshot({ path: join(OUT, '05-closing-drag.png') })
  await touch('touchEnd', 40, Y)
  await page.waitForTimeout(700)
  if (await offsetOf(page) !== null) fail('close gesture left the drawer mounted')

  // ── The band is a band ─────────────────────────────────────────────────
  // 137px was inside the predecessor's 35%-of-viewport zone, so a rightward
  // drag begun mid-message opened the drawer.
  await touch('touchStart', 137, Y)
  for (const x of [160, 240, 320]) { await touch('touchMove', x, Y); await page.waitForTimeout(30) }
  await touch('touchEnd', 320, Y)
  await page.waitForTimeout(400)
  if (await offsetOf(page) !== null) fail('a drag begun mid-pane still opened the drawer')

  // ── An interrupted gesture must not strand the panel ────────────────────
  // A cancelled gesture never reaches the release handler, so if abandoning
  // only stopped tracking, the panel sat mounted and half-open with the scrim
  // half-dimmed and no animation coming. Here a second finger arrives mid-drag.
  await touch('touchStart', START, Y)
  for (const x of [START + 20, START + 120, START + 220]) {
    await touch('touchMove', x, Y)
    await page.waitForTimeout(40)
  }
  const beforePinch = await offsetOf(page)
  if (beforePinch === null || beforePinch >= -20) fail(`drag did not take the panel over — offset ${beforePinch}`)
  // A new touch point is introduced by a touchStart carrying BOTH points — a
  // touchMove naming an id the browser has not seen is not a pinch, it is a
  // malformed event, and dispatching one here silently tested nothing.
  await cdp.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: START + 220, y: Y, id: 1 }, { x: START + 260, y: Y, id: 2 }],
  })
  await page.waitForTimeout(700)
  if (await offsetOf(page) !== null) fail('a pinch mid-drag left the drawer stranded half-open')
  await touch('touchEnd', START + 220, Y)   // lift both points
  await page.waitForTimeout(150)
  // ...and the gesture still works afterwards.
  await touch('touchStart', START, Y)
  for (const x of [START + 40, START + 200, START + 320]) {
    await touch('touchMove', x, Y)
    await page.waitForTimeout(40)
  }
  await touch('touchEnd', START + 320, Y)
  await page.waitForTimeout(700)
  const afterRecovery = await offsetOf(page)
  if (afterRecovery === null || Math.abs(afterRecovery) > 2) {
    fail(`gesture did not recover after an interruption — offset ${afterRecovery}`)
  }
  await page.screenshot({ path: join(OUT, '06-recovered-after-interrupt.png') })

  const video = page.video()
  await page.close()
  await context.close()
  await browser.close()
  srv.close()

  const webm = await video.path()
  console.log(`WEBM ${webm}`)

  // mp4 + palette GIF when ffmpeg is around; the webm is the evidence either way.
  const ffmpeg = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' }).status === 0
  if (ffmpeg && existsSync(webm)) {
    const mp4 = join(OUT, 'drawer-gesture.mp4')
    const gif = join(OUT, 'drawer-gesture.gif')
    const palette = join(OUT, 'video', 'palette.png')
    const filters = "fps=12,scale='min(700,iw)':-1:flags=lanczos"
    const run = args => spawnSync('ffmpeg', ['-y', '-loglevel', 'error', ...args], { stdio: 'inherit' })
    run(['-i', webm, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', mp4])
    run(['-i', webm, '-vf', `${filters},palettegen`, palette])
    run(['-i', webm, '-i', palette, '-lavfi', `${filters} [x]; [x][1:v] paletteuse`, gif])
    console.log(`MP4 ${mp4}`)
    console.log(`GIF ${gif}`)
  } else {
    console.log('ffmpeg not found — webm only')
  }

  if (failures) { console.error(`\n${failures} assertion(s) failed`); process.exit(1) }
  console.log(`\nOK — 10 assertions passed, frames in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
