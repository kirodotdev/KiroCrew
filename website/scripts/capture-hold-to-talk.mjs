/**
 * Video + stills + assertions for the hold-to-talk gesture (PR #5700).
 *
 * Drives the ISOLATED capture entry (website/capture/hold-to-talk.html), which
 * mounts the real ChatInput and the real useVoiceInput at a 390px viewport. This
 * is the only check that exercises the gesture over REAL layout and REAL capture:
 * the unit suite (src/hooks/useTouchPushToTalk.test.ts) pins the state machine's
 * transitions, but happy-dom computes no layout, so "the cancel cue sits above the
 * thumb", "the bar occupies the composer's own box", and "a real MediaRecorder
 * session survives the gesture" are only observable here.
 *
 * The microphone is Chromium's fake device; `POST /api/stt/transcribe` is the one
 * intercepted boundary, so no gateway or speech backend is required. The audio is
 * really captured and really posted — only the response is synthesised.
 *
 * Assertions (each a way the feature could regress while still looking plausible):
 *  - keyboard mode advertises the gesture in the placeholder
 *  - tapping the mic swaps the textarea for the hold target
 *  - crossing the 56px threshold flips the cue to the discard wording
 *  - a release inside the cancel zone leaves the composer EMPTY (the discard
 *    actually discarded, rather than committing and looking cancelled)
 *  - a release on the bar lands the transcript in the composer
 *  - a draft hands the textarea back and leaves the mic an ENABLED record
 *    control, so dictating onto existing text still works
 *  - a sub-threshold tap discards and says so, leaving nothing running
 *  - a voice round-trip leaves an EMPTY composer at its resting height (the
 *    textarea is parked in a 1px `sr-only` box in voice mode, and measuring it
 *    there pinned the composer at its 140px ceiling for good)
 *  - no drag-resize handle exists under a finger
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-hold-to-talk.mjs http://127.0.0.1:6822 ../temp-screenshots/hold-to-talk
 */
import { chromium } from 'playwright'
import { mkdirSync, mkdtempSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/hold-to-talk'
mkdirSync(OUT, { recursive: true })

/*
 * The exact frames this script publishes. Declared rather than discovered so the
 * startup cleanup below can name every file it deletes: OUT is caller-supplied,
 * so a pattern wide enough to catch our own frames ('NN-*.png') would also catch
 * a caller's unrelated '42-report.png'. shot() rejects a name that is not on this
 * list, which is what stops a frame added later from escaping the cleanup.
 */
const FRAMES = [
  '01-keyboard-mode', '02-hold-mode', '03-holding', '04-armed-cancel', '05-after-discard',
  '06-holding-again', '07-transcript-in-composer', '08-after-tap', '09-draft-mic-records',
  '10-draft-dictation-stoppable', '11-mode-roundtrip',
]
const VIDEO_NAME = 'hold-to-talk.webm'

/*
 * Playwright names its recording by a random id, so it needs a directory of its
 * own to be found in. That directory is created fresh per run rather than being a
 * fixed name under OUT: OUT is caller-supplied, and any name this script picked
 * in advance -- however unlikely -- could be a directory the caller already owns,
 * which the cleanup below then deletes recursively. A path that did not exist
 * until this run created it cannot be anyone else's. It stays under OUT so the
 * rename at the end is a same-filesystem move rather than a cross-device copy.
 */
const VIDEO_DIR = mkdtempSync(join(OUT, '.raw-video-'))
// Sole owner of this directory's removal; runs on the fail() exits too.
process.on('exit', () => rmSync(VIDEO_DIR, { recursive: true, force: true }))

/*
 * Clear this run's own artifacts up front, so a run that crashes half way cannot
 * leave a previous run's frame behind for the PR to embed. Only these exact names
 * are removed -- every other entry in OUT is left untouched.
 */
for (const frame of FRAMES) rmSync(join(OUT, `${frame}.png`), { force: true })
rmSync(join(OUT, VIDEO_NAME), { force: true })

/** iPhone 13/14 CSS viewport. */
const VIEWPORT = { width: 390, height: 844 }
/** Must exceed CANCEL_THRESHOLD_PX (56) to arm the discard. */
const DRAG_UP_PX = 74
/** Comfortably past the default holdMs (500) so the press resolves as a hold. */
const HOLD_MS = 950
/** ChatInput's INPUT_MIN_H — the collapsed one-line height of the textarea. */
const INPUT_MIN_H = 44
const TRANSCRIPT = 'Arm auto-merge on that PR and keep an eye on it'
/** The isolated capture entry. Warm-up and the recorded run must load the same URL. */
const CAPTURE_URL = `${BASE}/capture/hold-to-talk.html?theme=dark`

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env

const browser = await chromium.launch({
  env: browserEnv,
  args: [
    // A real MediaStream with synthetic audio, and no permission prompt — so
    // getUserMedia and MediaRecorder run for real without a human or a device.
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
    // The dictation panel's shader needs WebGL2; headless has no GPU.
    '--enable-unsafe-swiftshader',
  ],
})

/*
 * Recording starts when the context is created, and a dev server compiles its
 * module graph on the first request -- which used to put ~5s of blank white at
 * the head of the video. Pay that cost in a throwaway context first, so the
 * recording opens on a rendered composer instead of an empty page. Trimming the
 * lead-in at the encode step would fix only the file I happen to publish; the
 * next person to re-record from this script would get the blank back.
 */
const warmup = await browser.newContext({ viewport: VIEWPORT, hasTouch: true, isMobile: true })
const warmupPage = await warmup.newPage()
await warmupPage.goto(CAPTURE_URL, { waitUntil: 'networkidle' })
await warmupPage.waitForSelector('[data-testid="input-wrapper"]')
await warmup.close()

const context = await browser.newContext({
  viewport: VIEWPORT,
  // Coarse pointer is what gates the feature. 390px alone already satisfies
  // ChatInput's `isMobile`, but emulating touch keeps the capture honest about
  // the device class the gesture is for.
  hasTouch: true,
  isMobile: true,
  deviceScaleFactor: 2,
  permissions: ['microphone'],
  recordVideo: { dir: VIDEO_DIR, size: VIEWPORT },
})

const page = await context.newPage()

// The one stood-in boundary. Everything before it — capture, encoding, the POST
// itself — is the real path.
await page.route('**/api/stt/transcribe', route =>
  route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ text: TRANSCRIPT }),
  }),
)

let failures = 0
const fail = msg => { console.error(`FAIL ${msg}`); failures++ }
const ok = msg => console.log(`ok   ${msg}`)
const shot = name => {
  if (!FRAMES.includes(name)) fail(`frame '${name}' is not in FRAMES, so the startup cleanup cannot clear it`)
  return page.screenshot({ path: join(OUT, `${name}.png`) })
}

await page.goto(CAPTURE_URL, { waitUntil: 'networkidle' })
await page.waitForSelector('[data-testid="input-wrapper"]')
await page.waitForTimeout(700)

// ── 1. Keyboard mode ────────────────────────────────────────────────────────
const composer = page.locator('textarea[data-composer-input]')
const placeholder = await composer.getAttribute('placeholder')
if (placeholder === 'Send a message, or tap the mic for voice') ok('keyboard mode promises only what the tap delivers')
else fail(`placeholder was ${JSON.stringify(placeholder)}`)
/** The empty composer's resting height, which section 8 requires it to return to. */
const baselineWrapperH = Math.round((await page.locator('[data-testid="input-wrapper"]').boundingBox()).height)
await shot('01-keyboard-mode')

// ── 2. Switch to hold mode ──────────────────────────────────────────────────
const micSwitch = page.getByRole('button', { name: 'Switch to voice', exact: true })
if (await micSwitch.count() === 1) ok('mic renders as a mode switch')
else fail('no "Switch to voice" button')
await micSwitch.click()

const holdBar = page.locator('[data-testid="hold-to-talk"]')
await holdBar.waitFor({ state: 'visible' })
// `sr-only` keeps the textarea in the tree (and so "visible" to Playwright, with
// its own 32x44 box) on purpose — value, caret and IME state must survive the
// swap. What matters is that the user cannot see or reach it, so assert the
// clip: nothing at the textarea's own centre hit-tests back to the textarea.
const unreachable = await page.evaluate(() => {
  const ta = document.querySelector('textarea[data-composer-input]')
  if (!ta) return 'missing'
  const r = ta.getBoundingClientRect()
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
  return hit === ta ? 'reachable' : 'clipped'
})
if (unreachable === 'clipped') ok('textarea clipped but still mounted')
else fail(`textarea is ${unreachable} in hold mode`)
await page.waitForTimeout(500)
await shot('02-hold-mode')

const box = await holdBar.boundingBox()
// The entire justification for swapping the textarea is a target a thumb can hit
// without aiming. A bar that shrink-wraps its label (Btn's base is `inline-flex`)
// silently reverts the design to a small pill while every state assertion below
// still passes, so pin the width.
const wrapperBox = await page.locator('[data-testid="input-wrapper"]').boundingBox()
const fill = box.width / wrapperBox.width
if (fill > 0.85) ok(`hold bar spans the composer (${Math.round(fill * 100)}%)`)
else fail(`hold bar only ${Math.round(fill * 100)}% of the composer — it shrink-wrapped`)
if (box.height >= 44) ok(`hold bar is ${Math.round(box.height)}px tall`)
else fail(`hold bar only ${Math.round(box.height)}px tall, below the 44px touch minimum`)
const cx = box.x + box.width / 2
const cy = box.y + box.height / 2

// ── 3. Take A — hold, drag into the cancel zone, release: DISCARD ───────────
await page.mouse.move(cx, cy)
await page.mouse.down()
await page.waitForTimeout(HOLD_MS)

const cue = page.locator('[data-testid="hold-cancel-cue"]')
await cue.waitFor({ state: 'visible' })
if ((await cue.innerText()).trim() === 'Slide up to cancel') ok('cue offers the discard')
else fail(`cue read ${JSON.stringify((await cue.innerText()).trim())}`)
await shot('03-holding')

for (let i = 1; i <= 6; i++) {
  await page.mouse.move(cx, cy - (DRAG_UP_PX * i) / 6)
  await page.waitForTimeout(70)
}
if ((await cue.innerText()).trim() === 'Release to cancel') ok('threshold arms the discard')
else fail(`armed cue read ${JSON.stringify((await cue.innerText()).trim())}`)
await shot('04-armed-cancel')
await page.waitForTimeout(500)

await page.mouse.up()
await page.waitForTimeout(1800)
if ((await composer.inputValue()) === '') ok('discard left the composer empty')
else fail(`discard still committed: ${JSON.stringify(await composer.inputValue())}`)
if (await holdBar.isVisible()) ok('hold bar returns after a discard')
else fail('hold bar gone after a discard')
await shot('05-after-discard')
await page.waitForTimeout(400)

// ── 4. Take B — a short tap DISCARDS and leaves nothing running ──────────────
// A press that never became a hold is not a recording the user asked for. Capture
// did open on the pointerdown (so the opening word is never clipped), so what this
// pins is that the fragment goes unsent AND that nothing is left live behind an
// idle-looking bar — the shape every latch defect took.
await page.mouse.move(cx, cy)
await page.mouse.down()
await page.waitForTimeout(120)          // deliberately under holdMs — a tap, not a hold
await page.mouse.up()
await page.waitForTimeout(250)          // inside the cue window

// The cue is the whole point: capture opened on the press, so a fragment really
// was dropped, and the most likely first gesture on a new control is a tap.
if ((await holdBar.innerText()).trim() === 'Keep holding to record') ok('a discarded tap says so')
else fail(`bar after a tap read ${JSON.stringify((await holdBar.innerText()).trim())}`)
await shot('08-after-tap')

await page.waitForTimeout(1800)         // past TAP_CUE_MS
if ((await holdBar.innerText()).trim() === 'Hold to talk') ok('the cue clears itself')
else fail(`bar did not return to rest: ${JSON.stringify((await holdBar.innerText()).trim())}`)
if (await holdBar.isEnabled()) ok('the bar is pressable again after a tap')
else fail('bar still disabled after a tap — something is left in flight')
if ((await composer.inputValue()) === '') ok('a tap transcribes nothing')
else fail(`a tap still committed: ${JSON.stringify(await composer.inputValue())}`)
if (await page.getByRole('button', { name: 'Discard', exact: true }).count() === 0) {
  ok('no latch discard control exists')
} else fail('a latch discard control is still rendered')
await page.waitForTimeout(500)

// ── 5. Take C — hold and release in place: COMMIT ───────────────────────────
await page.mouse.move(cx, cy)
await page.mouse.down()
await page.waitForTimeout(HOLD_MS + 350)
await shot('06-holding-again')
await page.mouse.up()

await page.waitForFunction(
  expected => document.querySelector('textarea[data-composer-input]')?.value === expected,
  TRANSCRIPT,
  { timeout: 15000 },
).then(() => ok('release committed the transcript')).catch(() => fail('transcript never landed'))
await page.waitForTimeout(900)
await shot('07-transcript-in-composer')

// ── 6. A draft suspends hold mode, and the mic reverts to a RECORD control ───
if (await composer.isVisible()) ok('draft hands the textarea back')
else fail('textarea still hidden with a draft pending')
if (await holdBar.count() === 0) ok('hold bar yields to the draft')
else fail('hold bar still mounted over a draft')
// The regression this closes: the mic used to be disabled on every draft, on every
// touch device, which removed dictating ONTO existing text entirely. With no mode
// to switch into it is a record button again, exactly as it was before this feature.
const micRecord = page.getByRole('button', { name: 'Voice input', exact: true })
if (await micRecord.count() === 1 && !(await micRecord.isDisabled())) {
  ok('mic reverts to an enabled record control with a draft')
} else fail('mic is not an enabled record control while a draft is pending')
if (await page.getByRole('button', { name: /Clear the draft first/ }).count() === 0) {
  ok('no draft-blocked affordance remains')
} else fail('the draft-blocked label is still rendered')
await shot('09-draft-mic-records')

// ── 7. Dictation STARTED from a draft must keep a way to stop itself ─────────
// The regression this closes: a draft suspends hold mode "unless capture is in
// flight", and that exception matched the mic-as-record-button route too. Tapping
// the mic over a draft promoted the composer into hold mode, where the bar renders
// `settling` (disabled) and the mic renders a disabled mode switch — a live
// microphone with no control on screen able to end it.
await micRecord.click()
await page.waitForTimeout(900)
// NOT a textarea-reachability check: keyboard-mode dictation legitimately swaps the
// textarea for the dictation panel, and has since before this PR. What must hold is
// that the capture landed on THAT surface rather than being promoted into hold mode.
if (await page.getByTestId('voice-dictation-panel').count() === 1) {
  ok('draft-started dictation lands on the keyboard dictation panel')
} else fail('draft-started dictation did not open the dictation panel')
if (await holdBar.count() === 0) ok('no hold bar takes over a draft-started dictation')
else fail('hold bar mounted over a draft-started dictation')
const stopControl = page.getByRole('button', { name: /Voice input|Stop/, exact: false })
if (await stopControl.count() >= 1 && !(await stopControl.first().isDisabled())) {
  ok('draft-started dictation keeps an enabled stop control')
} else fail('draft-started dictation has no enabled control that can stop it')
await shot('10-draft-dictation-stoppable')
await page.waitForTimeout(700)

// ── 8. The mode round-trip must not INFLATE the composer ─────────────────────
// The textarea stays mounted through voice mode, clipped inside `sr-only` — a 1px
// box. The auto-size pass measured it there anyway, read a `scrollHeight` of most
// of a viewport, clamped it to the 140px ceiling and wrote that back as an inline
// height that outlived the parking. So a voice round-trip handed the user back a
// permanently tall empty composer, on the one surface with no way to shrink it
// (the reset is a double-click, and the drag handle is gone under a finger).
// Only observable over real layout: happy-dom computes no `scrollHeight`.
await stopControl.first().click()
await page.waitForTimeout(1200)
await page.evaluate(() => {
  const ta = document.querySelector('textarea[data-composer-input]')
  const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
  setValue.call(ta, '')
  ta.dispatchEvent(new Event('input', { bubbles: true }))
})
// Clearing the draft is what hands the surface back to hold mode, so the composer
// is parked again — with the empty value whose height the bug measured.
await holdBar.waitFor({ state: 'visible' })
await page.waitForTimeout(400)
await page.getByRole('button', { name: 'Switch to keyboard', exact: true }).click()
await composer.waitFor({ state: 'visible' })
await page.waitForTimeout(400)

const roundTrip = await page.evaluate(() => {
  const w = document.querySelector('[data-testid="input-wrapper"]')
  const ta = document.querySelector('textarea[data-composer-input]')
  return { wrapper: Math.round(w.getBoundingClientRect().height), inline: ta.style.height, value: ta.value }
})
if (roundTrip.value === '') ok('the round-trip left the composer empty')
else fail(`composer was not empty: ${JSON.stringify(roundTrip.value)}`)
if (roundTrip.wrapper === baselineWrapperH) {
  ok(`an empty composer is back at its resting ${baselineWrapperH}px after a voice round-trip`)
} else {
  fail(`empty composer is ${roundTrip.wrapper}px after a voice round-trip, was ${baselineWrapperH}px at rest`
    + ` (textarea inline height ${JSON.stringify(roundTrip.inline)})`)
}
// Named separately: the wrapper check above is the symptom, this is the mechanism,
// and a future hider of the textarea would break this one first.
if (roundTrip.inline === `${INPUT_MIN_H}px`) ok('the textarea was not measured while parked')
else fail(`textarea inline height is ${JSON.stringify(roundTrip.inline)}, expected ${INPUT_MIN_H}px`)
// Both forms, deliberately. The testid is this script's handle on the element; the
// class is the affordance itself, and is what the element had BEFORE it carried a
// testid — so a revert of the fix fails here rather than passing vacuously.
const handleByTestId = await page.getByTestId('composer-resize-handle').count()
const handleByClass = await page.locator('.input-area .cursor-row-resize').count()
if (handleByTestId === 0 && handleByClass === 0) {
  ok('no drag handle under a finger')
} else fail(`the drag handle renders on a touch device (testid ${handleByTestId}, class ${handleByClass})`
  + ' — a tap can pin the height with no way back')
await shot('11-mode-roundtrip')
await page.waitForTimeout(500)

/*
 * Hold this run's own video handle. Playwright names the file by a random id, so
 * the only way to be sure the published recording is THIS run's is to ask the
 * page which file is its own -- picking the directory's newest or only .webm
 * would publish a previous run's video whenever one is still lying around.
 */
const video = page.video()
if (!video) fail('no video produced')
// context.close() is what finalizes the file; the path is only resolvable after it.
await context.close()
if (video) {
  // This run's own handle, not a directory scan -- a scan would republish an
  // earlier run's recording whenever one was still lying around.
  renameSync(await video.path(), join(OUT, VIDEO_NAME))
  console.log(`WEBM ${join(OUT, VIDEO_NAME)}`)
}
await browser.close()

console.log(failures === 0 ? '\nhold-to-talk capture: OK' : `\nhold-to-talk capture: ${failures} failure(s)`)
process.exit(failures === 0 ? 0 : 1)
