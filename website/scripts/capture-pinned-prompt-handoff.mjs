/**
 * Recording + measurement runner for capture/pinned-prompt-handoff.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6820 --strictPort
 *   node scripts/capture-pinned-prompt-handoff.mjs http://127.0.0.1:6820 \
 *     ../temp-screenshots/pinned-prompt-handoff
 *
 * The webm is evidence, but the MEASUREMENT is the point. A prompt should stop
 * travelling when it reaches the band and stay there, so the pixel distance
 * between where the bubble was on the frame BEFORE it pinned and where the
 * banner card lands on the frame it pins is the defect: a positive number is
 * content jumping back DOWN the screen after having scrolled past its own
 * resting place.
 *
 * `snapBackPx` per prompt is printed and written to handoff-metrics.json.
 * The runner exits nonzero if it never observed a hand-off at all — a run that
 * photographs no swap is not evidence of anything.
 */
import { chromium } from 'playwright'
import { mkdirSync, mkdtempSync, writeFileSync, renameSync, readdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve, sep } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6820'
const OUT = process.argv[3] || '../temp-screenshots/pinned-prompt-handoff'
/**
 * LABEL and THEME land in filesystem paths, one of which this script later removes
 * recursively. `path.join` normalizes `..`, so an unchecked
 * `CAPTURE_LABEL=../../important` resolves OUT of the output directory and the
 * cleanup deletes whatever sits there instead. Restrict both to a name that cannot
 * hold a separator or a traversal segment, and REFUSE rather than sanitize:
 * silently rewriting a label would put the run's evidence somewhere the caller did
 * not ask for, which is its own kind of wrong answer.
 */
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
function safeName(kind, value) {
  if (!SAFE_NAME.test(value) || value === '.' || value === '..') {
    console.error(`FAIL: ${kind} must match ${SAFE_NAME} — got ${JSON.stringify(value)}`)
    process.exit(2)
  }
  return value
}
const THEME = safeName('CAPTURE_THEME', process.env.CAPTURE_THEME || 'dark')
const LABEL = safeName('CAPTURE_LABEL', process.env.CAPTURE_LABEL || 'run')
/**
 * Which prompt the harness renders. `tall` is the 30-line case: a prompt far
 * taller than the pinned card's resting clamp, which is what makes the fold's
 * worst gap measurable. An allow-list rather than a name gate, because only these
 * two scenarios exist on the page and a typo should fail loudly instead of
 * silently recording the default one and reading as evidence for the other.
 */
const SCENARIOS = ['default', 'tall']
const SCENARIO = process.env.CAPTURE_SCENARIO || 'default'
if (!SCENARIOS.includes(SCENARIO)) {
  console.error(`FAIL: CAPTURE_SCENARIO must be one of ${SCENARIOS.join(' | ')} — got ${JSON.stringify(SCENARIO)}`)
  process.exit(2)
}

/** Scroll pacing. Small steps so the swap frame is not skipped over. */
const STEP_PX = 6
const STEPS = 230
const STEP_MS = 26
/** Dwell before the scroll starts, so the video's opening frames show the
 *  transcript at rest rather than the recorder's blank first paint. */
const LEAD_MS = 1800

mkdirSync(OUT, { recursive: true })
// Per-run video directory. Playwright names the file with a random hash and
// only on context close, so a shared directory forces a "which file is mine"
// guess — and a lexicographic pick over a directory that already holds RENAMED
// videos silently selects one of those instead, republishing an earlier run's
// footage under this run's label.
// Playwright records into a directory of its own, and the cleanup at the end of
// this script deletes that directory recursively. With a FIXED name, a directory
// that ALREADY sits at that path is deleted too -- content this script never
// created, gone on a run that only meant to record a video. `safeName` and the
// gate below stop the path ESCAPING the output directory; neither makes the name
// unused. So the name is made unique per run instead: `mkdtempSync` creates a
// fresh directory and fails if it cannot, so the delete can only ever remove the
// one this process just made.
//
// The gate runs on the PREFIX, before anything is created, and checks the
// resolved path rather than trusting the name rule alone.
const VIDEO_PREFIX = join(OUT, `${LABEL}-${THEME}-video-`)
{
  const outAbs = resolve(OUT)
  const prefixAbs = resolve(VIDEO_PREFIX)
  if (dirname(prefixAbs) !== outAbs || !prefixAbs.startsWith(outAbs + sep)) {
    console.error(`FAIL: video dir escaped the output directory: ${prefixAbs} not directly inside ${outAbs}`)
    process.exit(2)
  }
}
const VIDEO_DIR = mkdtempSync(VIDEO_PREFIX)

const browser = await chromium.launch()
const ctx = await browser.newContext({
  viewport: { width: 1000, height: 700 },
  colorScheme: THEME === 'light' ? 'light' : 'dark',
  recordVideo: { dir: VIDEO_DIR, size: { width: 1000, height: 700 } },
})
const page = await ctx.newPage()
const errors = []
page.on('pageerror', e => errors.push(String(e)))

await page.goto(`${BASE}/capture/pinned-prompt-handoff.html?theme=${THEME}&guides=1${SCENARIO === 'tall' ? '&tall=1' : ''}`, { waitUntil: 'networkidle' })
await page.waitForSelector('[data-capture-root]', { timeout: 20000 })
await page.waitForSelector('[data-display-index="0"]', { timeout: 20000 })
await page.waitForTimeout(700)

/** One frame of geometry, all relative to the fold line. */
const sample = () => page.evaluate(() => {
  const sc = document.querySelector('[data-capture-scroller]')
  const fold = document.querySelector('[aria-hidden].h-0')
  const foldY = fold.getBoundingClientRect().top
  const rows = [...document.querySelectorAll('[data-display-index]')].map(el => {
    const r = el.getBoundingClientRect()
    // The eye tracks the BUBBLE, not the row box: the row carries `py-1` padding
    // the bubble does not, and the banner card is a copy of the bubble. Reporting
    // row edges would credit the fix with the padding as if it were travel.
    const bub = el.querySelector('.user-bubble')
    const b = bub ? bub.getBoundingClientRect() : r
    return {
      idx: Number(el.getAttribute('data-display-index')),
      top: b.top - foldY,
      bottom: b.bottom - foldY,
      h: b.height,
      hidden: getComputedStyle(el).visibility === 'hidden',
    }
  })
  const cardEl = document.querySelector('[data-testid="pinned-prompt"]')
  const cardBox = cardEl ? cardEl.querySelector('.user-bubble') || cardEl : null
  const card = cardBox
    ? { top: cardBox.getBoundingClientRect().top - foldY, h: cardBox.getBoundingClientRect().height }
    : null
  return { scrollTop: sc.scrollTop, rows, card }
})

const frames = []
// Per-step stills are a SECOND pass, off by default: a screenshot between every
// step stalls the scroll, and the recorded video of that pass plays as a stutter
// rather than as the motion the claim is about. Video pass = no stills; strip
// pass = CAPTURE_STILLS=1.
const stills = process.env.CAPTURE_STILLS === '1'
const stillDir = join(OUT, `${LABEL}-${THEME}-frames`)
if (stills) mkdirSync(stillDir, { recursive: true })
const still = i => (stills
  ? page.screenshot({ path: join(stillDir, `f${String(i).padStart(3, '0')}.png`) })
  : Promise.resolve())
await page.waitForTimeout(LEAD_MS)
frames.push(await sample())
await still(0)
for (let i = 0; i < STEPS; i++) {
  await page.evaluate(px => {
    document.querySelector('[data-capture-scroller]').scrollTop += px
  }, STEP_PX)
  await page.waitForTimeout(STEP_MS)
  frames.push(await sample())
  await still(i + 1)
}
await page.waitForTimeout(700)

await ctx.close()
await browser.close()

if (errors.length) {
  console.error('page errors:', errors.slice(0, 5))
}

// Exactly one video exists in this run's own directory. Anything else means the
// recording did not happen, and shipping a stale file under this label would be
// worse than no evidence at all.
const found = readdirSync(VIDEO_DIR).filter(f => f.endsWith('.webm'))
if (found.length !== 1) {
  console.error(`FAIL: expected 1 video in ${VIDEO_DIR}, found ${found.length}`)
  process.exit(1)
}
renameSync(join(VIDEO_DIR, found[0]), join(OUT, `${LABEL}-${THEME}.webm`))
rmSync(VIDEO_DIR, { recursive: true, force: true })

/**
 * A hand-off is the first frame on which a given row becomes the hidden one.
 * The row's position on the PREVIOUS frame is where the bubble visibly was; the
 * card's position on this frame is where the reader's eye is sent instead.
 */
const handoffs = []
let prevHidden = null
for (let i = 1; i < frames.length; i++) {
  const f = frames[i]
  const hidden = f.rows.find(r => r.hidden)
  if (hidden && hidden.idx !== prevHidden) {
    const before = frames[i - 1].rows.find(r => r.idx === hidden.idx)
    // The card is MID-FOLD on the hand-off frame (the morph runs for MORPH_MS),
    // so its height there is an animation frame and not a fact about the design.
    // Read the settled height a few frames later instead, and label both.
    const settled = frames[Math.min(i + 10, frames.length - 1)]
    if (before && f.card) {
      handoffs.push({
        row: hidden.idx,
        frame: i,
        scrollTop: f.scrollTop,
        bubbleTopBefore: +before.top.toFixed(1),
        bubbleH: +before.h.toFixed(1),
        cardTop: +f.card.top.toFixed(1),
        cardHAtHandoff: +f.card.h.toFixed(1),
        cardHSettled: settled.card ? +settled.card.h.toFixed(1) : null,
        // Positive = the banner lands BELOW where the bubble had already
        // travelled to, i.e. the content jumps back down the screen. Bounded
        // below by the scroll step: the pin can only fire on a sampled frame, so
        // a residual smaller than STEP_PX is discretisation, not travel.
        snapBackPx: +(f.card.top - before.top).toFixed(1),
      })
    }
    prevHidden = hidden.idx
  }
  if (!hidden) prevHidden = null
}

/**
 * Frames after the first hand-off on which NO banner is shown at all.
 *
 * Once a prompt has scrolled up to the line there is always a prompt above the
 * fold, so there is always something to pin — a frame with no card is the band
 * blinking out. It is the failure mode of getting the two predicates out of step
 * (the outgoing card is dropped when the incoming row's top reaches the fold, so
 * a row exactly on the line must already be pinnable), and it is invisible in
 * `snapBackPx`, which only looks at hand-off frames.
 */
const firstPin = frames.findIndex(f => f.rows.some(r => r.hidden))
const blankFrames = firstPin < 0 ? [] : frames
  .slice(firstPin)
  .map((f, i) => ({ frame: firstPin + i, card: f.card, scrollTop: f.scrollTop }))
  .filter(f => f.card == null)

writeFileSync(join(OUT, `${LABEL}-${THEME}-handoff-metrics.json`), JSON.stringify({
  label: LABEL, theme: THEME, stepPx: STEP_PX, steps: STEPS, handoffs,
  blankFrameCount: blankFrames.length,
  blankFrames: blankFrames.slice(0, 12),
}, null, 2))

console.log(`video: ${OUT}/${LABEL}-${THEME}.webm`)
console.table(handoffs)
console.log(`blank banner frames after first pin: ${blankFrames.length}`)

if (!handoffs.length) {
  console.error('FAIL: no hand-off observed — the run photographs nothing.')
  process.exit(1)
}
if (blankFrames.length) {
  console.error(`FAIL: banner absent on ${blankFrames.length} frame(s), e.g. scrollTop `
    + blankFrames.slice(0, 5).map(f => f.scrollTop).join(', '))
  process.exit(1)
}
