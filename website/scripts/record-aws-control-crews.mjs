/**
 * Video walkthrough of the AWS Control remote crews pane.
 *
 * Third design, because the first two each failed for a measurable reason:
 *
 *  1. Playwright's `recordVideo`: low-bitrate VP8 (420 kbps at 1920x1350 = 0.0065 bits per
 *     pixel per frame, where text mushes below ~0.02), AND the recorder captures at the
 *     VIEWPORT size and pads to `size` without applying `deviceScaleFactor`, so real
 *     content filled 44% of each frame and the rest was grey.
 *  2. One screenshot per beat, held for seconds: frames were visually lossless (PSNR 49.3 dB
 *     against the source stills) but 112 of 117 consecutive sampled frames were IDENTICAL
 *     and the whole clip had 4 perceptible transitions. That is a slideshow, not a
 *     walkthrough -- it shows the screens without showing the product being used.
 *
 * So: capture a CONTINUOUS sequence of lossless screenshots while driving the page, and
 * encode those. Motion is real (the click, the spinner turning, the viewport narrowing) and
 * nothing lossy sits between the browser and the frame.
 *
 * A cursor is drawn into the DOM because Playwright's screenshots contain no pointer, and
 * without one a viewer sees screens changing for no visible reason. It is moved to each
 * target before the click and flashes on press, so causality is legible.
 *
 * Capture rate is bounded by how fast `page.screenshot()` returns (~60-120ms here), so the
 * real rate is ~8-12 fps. Playback is set to the measured average so motion runs at natural
 * speed rather than fast or slow.
 *
 * Fixtures, the route table, the page setup and the phase switch are shared with
 * capture-aws-control-crews.mjs via ./lib/aws-control-crews-fixtures.mjs; they were
 * duplicated here once and the duplication gate (jscpd) reported the clone.
 *
 * Usage: node scripts/record-aws-control-crews.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, rmSync, existsSync, statSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import {
  createFixtureRouter,
  preparePage,
  makeReload,
} from './lib/aws-control-crews-fixtures.mjs'

const OUT = process.argv[2] || '/tmp/aws-control-crews-video'
mkdirSync(OUT, { recursive: true })
const FRAMES = join(OUT, '_frames')
rmSync(FRAMES, { recursive: true, force: true })
mkdirSync(FRAMES, { recursive: true })

const { answer, setMode } = createFixtureRouter()

const { srv: server, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  deviceScaleFactor: 1.5,
})
const page = await context.newPage()
await preparePage(page, answer)
const reload = makeReload(page, base, setMode)

// ---- a cursor the screenshots can see -------------------------------------
// Playwright renders no pointer, so without this the screens change for no visible
// reason. Re-injected after every navigation because a full page load wipes it.
const CURSOR_JS = `
  (() => {
    if (document.getElementById('__rec_cursor')) return;
    const d = document.createElement('div');
    d.id = '__rec_cursor';
    d.style.cssText = [
      'position:fixed', 'z-index:2147483647', 'left:0', 'top:0',
      'width:22px', 'height:22px', 'margin:-11px 0 0 -11px',
      'border-radius:50%', 'pointer-events:none',
      'background:rgba(255,255,255,.92)',
      'box-shadow:0 0 0 2px rgba(0,0,0,.55), 0 2px 10px rgba(0,0,0,.5)',
      'transition:transform .09s linear, width .09s, height .09s',
    ].join(';');
    document.body.appendChild(d);
    window.__recMove = (x, y) => {
      d.style.transform = 'translate(' + x + 'px,' + y + 'px)';
    };
    window.__recPress = (on) => {
      d.style.width = on ? '13px' : '22px';
      d.style.height = on ? '13px' : '22px';
    };
  })()
`
const showCursor = () => page.evaluate(CURSOR_JS)
await showCursor()

// ---- capture loop ---------------------------------------------------------
let n = 0
const shot = async () => {
  await page.screenshot({
    path: join(FRAMES, `f${String(++n).padStart(4, '0')}.png`),
    fullPage: false,
    animations: 'allow', // keep the spinner mid-turn instead of freezing it
  })
}
/** Capture continuously for `ms`, as fast as screenshots return. */
async function rolling(ms) {
  const until = Date.now() + ms
  while (Date.now() < until) await shot()
}
/** Glide the cursor to an element's centre, capturing the whole way. */
async function moveTo(selector, steps = 9) {
  const box = await page.locator(selector).boundingBox()
  if (!box) throw new Error(`no box for ${selector}`)
  const tx = box.x + box.width / 2
  const ty = box.y + box.height / 2
  const from = moveTo._last || { x: 40, y: 40 }
  for (let i = 1; i <= steps; i++) {
    const x = from.x + ((tx - from.x) * i) / steps
    const y = from.y + ((ty - from.y) * i) / steps
    await page.evaluate(([a, b]) => window.__recMove?.(a, b), [x, y])
    await shot()
  }
  moveTo._last = { x: tx, y: ty }
  return { tx, ty }
}
async function clickAt(selector) {
  await moveTo(selector)
  await page.evaluate(() => window.__recPress?.(true))
  await shot()
  await shot()
  await page.locator(selector).click()
  await page.evaluate(() => window.__recPress?.(false))
  await shot()
}

const t0 = Date.now()

console.log('=== the grid ===')
await reload('list')
await showCursor()
await rolling(1600)

console.log('=== open a crew ===')
await clickAt('[data-testid="crew-card"][data-crew="billing-help"]')
await rolling(2200)

console.log('=== refresh, with the spinner turning ===')
await clickAt('[data-testid="crew-detail-refresh"]')
await rolling(2000) // the router holds the response, so this is a real spinner

console.log('=== back out ===')
await clickAt('[data-testid="crew-detail-back"]')
await rolling(1600)

for (const [mode, label, ms] of [
  ['base', 'no shared base stack', 2000],
  ['empty', 'base ready, no crews', 2000],
  ['mismatch', 'account_mismatch', 2200],
]) {
  console.log(`=== ${label} ===`)
  await reload(mode)
  await showCursor()
  await rolling(ms)
}

console.log('=== narrow to 320px ===')
await reload('list')
await showCursor()
await rolling(700)
// Step the width down so the reflow is visible rather than a jump cut.
for (const w of [1080, 900, 720, 560, 430, 320]) {
  await page.setViewportSize({ width: w, height: 900 })
  await showCursor()
  await shot()
  await shot()
}
await rolling(1800)

const elapsed = (Date.now() - t0) / 1000
const fps = n / elapsed
console.log(`\n  captured ${n} frames in ${elapsed.toFixed(1)}s -> ${fps.toFixed(1)} fps`)

await context.close()
await browser.close()
server.close()

// ---- encode ---------------------------------------------------------------
// Playback at the measured capture rate so motion runs at real speed.
const rate = Math.max(6, Math.min(15, Number(fps.toFixed(2))))
const mp4 = join(OUT, 'crews-walkthrough.mp4')
execFileSync('ffmpeg', ['-y', '-loglevel', 'error',
  '-framerate', String(rate), '-i', join(FRAMES, 'f%04d.png'),
  '-vf', 'format=yuv420p', '-r', '25',
  '-c:v', 'libx264', '-preset', 'slow', '-crf', '20', '-movflags', '+faststart',
  mp4], { stdio: 'inherit' })

const gif = join(OUT, 'crews-walkthrough.gif')
const palette = join(FRAMES, 'palette.png')
execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', mp4,
  '-vf', 'fps=10,scale=1100:-1:flags=lanczos,palettegen=max_colors=160', palette],
  { stdio: 'inherit' })
execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', mp4, '-i', palette,
  '-lavfi', 'fps=10,scale=1100:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3',
  gif], { stdio: 'inherit' })

const mb = (p) => (existsSync(p) ? (statSync(p).size / 1048576).toFixed(2) + ' MB' : 'missing')
console.log('\n=== OUTPUT ===')
for (const p of [mp4, gif]) console.log(`  ${p}  ${mb(p)}`)
rmSync(FRAMES, { recursive: true, force: true })
console.log('done')
