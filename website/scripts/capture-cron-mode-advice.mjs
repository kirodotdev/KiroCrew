/**
 * Screenshots for the Schedule create dialog's `Minimal context` toggle and the
 * mode-advice note that sits above it.
 *
 * Drives the isolated capture entry (website/capture/cron-mode-advice.html),
 * which mounts the REAL JobForm in the create shape the job dialog uses
 * (layout="vertical", externalSubmit, no `job`). The note is never seeded: each
 * frame types a prompt into the REAL `Message` textarea and lets
 * `utils/cronModeAdvice.ts` decide, so a frame cannot document a state the
 * shipped code would not produce.
 *
 * Every frame asserts its own state before writing — the note's presence, and
 * for a present note the FULL string it must carry. A note that rendered the
 * other variant's copy, or an empty accent box, fails here instead of shipping
 * as evidence.
 *
 * Frames: `-form` shows the whole dialog (the typed prompt and the note in one
 * shot, which is what a cold reviewer needs) and `-note` is the tight crop from
 * the `Hide in chat` row down through the whole `Minimal context` row, so the
 * note copy is legible at 2x. Scene 04 gets no crop: its evidence is the prompt,
 * which the crop region excludes.
 *
 *   01-no-advice          dialog as opened: the new toggle, no note
 *   02-script-advice      a mechanical prompt draws the script-mode advice
 *   03-minimal-context    a reasoning prompt draws the minimal-context advice
 *   04-context-prompt     a reasoning prompt that names saved context draws
 *                         NOTHING — proves the note is judged, not merely
 *                         triggered by typing
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6801 --strictPort   # in another shell
 *   node scripts/capture-cron-mode-advice.mjs http://127.0.0.1:6801 ../temp-screenshots/cron-mode-advice
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6801'
const OUT = process.argv[3] || '../temp-screenshots/cron-mode-advice'
mkdirSync(OUT, { recursive: true })

/** The exact shipped copy, from website/src/i18n/locales/en.manual.json. A
 *  frame is only written when the note carries the whole string. */
const SCRIPT_COPY = 'This job reads like a mechanical check that needs no reasoning at all. The cheapest form is a script job, which takes no agent turn; ask in chat to create one. Minimal context is the next best thing here.'
const MINIMAL_COPY = 'This job needs the agent, but it reads as though it does not need your saved context. Minimal context keeps the agent and drops the rest.'

const NOTE = '[data-testid="jobform-mode-advice"]'
const TOGGLE_LABEL = 'Minimal context'
const SIBLING_LABEL = 'Hide in chat'
/** SettingsToggle stamps its label onto the whole ROW (settings.tsx), so these
 *  address the label, the description and the switch together. Addressing the
 *  label TEXT instead cropped the switch off the bottom of the frame. */
const TOGGLE_ROW = `[data-setting-label="${TOGGLE_LABEL}"]`
const SIBLING_ROW = `[data-setting-label="${SIBLING_LABEL}"]`

const SCENES = [
  {
    name: '01-no-advice',
    // The dialog as it opens. Below cronModeAdvice's 12-character floor there is
    // nothing to say, so this is also the state a half-typed prompt shows.
    message: '',
    expect: null,
    noteCrop: true,
  },
  {
    name: '02-script-advice',
    message: 'Check whether disk usage is above 80.',
    expect: SCRIPT_COPY,
    noteCrop: true,
  },
  {
    name: '03-minimal-context-advice',
    message: 'Summarize the new failures for me.',
    expect: MINIMAL_COPY,
    noteCrop: true,
  },
  {
    name: '04-context-prompt-no-advice',
    // Reads as judgement work AND names saved context, so neither cheaper mode
    // applies. Without this frame the evidence cannot tell a judged note from a
    // note that fires on any long prompt.
    message: 'Summarize the new failures using my notes from earlier sessions.',
    expect: null,
    // No crop: this scene's whole point is the PROMPT that drew nothing, and the
    // crop region excludes the Message field — it came out byte-identical to
    // scene 01's, so it would be a second copy of a frame already in the folder.
    noteCrop: false,
  },
]

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failed = false
let written = 0

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

/**
 * Clip spanning the whole `Hide in chat` row down to the bottom of the whole
 * `Minimal context` row, padded. Both rows are read from the live DOM, so the
 * crop follows the real layout instead of hard-coded pixels that would silently
 * slide off the note when a sibling field changes height. Row boxes, not label
 * boxes: a label box ends above the row's switch, and a crop built from it cut
 * the new toggle's own control out of the frame.
 */
async function noteClip(page) {
  const sibling = await page.locator(SIBLING_ROW).boundingBox()
  const row = await page.locator(TOGGLE_ROW).boundingBox()
  const form = await page.locator('[data-capture-root]').boundingBox()
  if (!sibling || !row || !form) throw new Error('could not locate the toggle rows for the crop')
  const pad = 14
  const top = sibling.y - pad
  const bottom = row.y + row.height + pad
  const clip = { x: form.x, y: top, width: form.width, height: bottom - top }
  // A crop that failed to reach past the new row's own bottom edge would drop
  // the very control the frame exists to show, so this is asserted, not assumed.
  if (clip.y + clip.height < row.y + row.height) {
    throw new Error(`crop ends at ${clip.y + clip.height} but the ${TOGGLE_LABEL} row ends at ${row.y + row.height}`)
  }
  return clip
}

for (const scene of SCENES) {
  const page = await browser.newPage({
    // Tall enough that the whole create form fits without scrolling: the
    // reviewer sees the typed prompt and the note it produced in one frame.
    viewport: { width: 640, height: 1180 },
    deviceScaleFactor: 2,
    reducedMotion: 'reduce',
  })
  // Gateway-free: answer every REAL API call the mounted form makes. Predicate
  // on the pathname — a glob like **/api/** would also swallow vite-served
  // source modules such as /src/api/client.ts and break boot. Array-shaped
  // endpoints must answer [] ({} crashes their .map consumers).
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /models|agents|skills|commands|sessions/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/cron-mode-advice.html?theme=dark`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-capture-root]')
  // The toggle is the feature's own control; waiting on it means a frame is
  // never taken from a form that mounted without it.
  await page.locator(TOGGLE_ROW).waitFor()

  if (scene.message) {
    // The real textarea and the real onChange path, not a seeded prop.
    await page.locator('#jobform-message').fill(scene.message)
  }
  // Let the advice memo settle before reading the DOM.
  await page.waitForTimeout(250)

  const noteCount = await page.locator(NOTE).count()
  const noteText = noteCount ? (await page.locator(NOTE).innerText()).trim() : ''
  const role = noteCount ? await page.locator(NOTE).getAttribute('role') : null
  const iconCount = noteCount ? await page.locator(`${NOTE} svg`).count() : 0
  const toggleVisible = await page.locator(TOGGLE_ROW).isVisible()
  // The row must carry a real switch, not just its label copy.
  const switchCount = await page.locator(`${TOGGLE_ROW} [role="switch"], ${TOGGLE_ROW} button, ${TOGGLE_ROW} input[type="checkbox"]`).count()

  const ok = (scene.expect === null
    // A note here would mean the judged 'none' path regressed.
    ? noteCount === 0
    // Full-string equality, so the two variants can never be confused, plus the
    // role and the icon the surface promises.
    : noteCount === 1 && noteText === scene.expect && role === 'note' && iconCount === 1
  ) && toggleVisible && switchCount >= 1

  const detail = scene.expect === null
    ? `notes=${noteCount} toggle=${toggleVisible} switch=${switchCount}`
    : `notes=${noteCount} role=${role} icon=${iconCount} toggle=${toggleVisible} switch=${switchCount} text=${JSON.stringify(noteText.slice(0, 60))}`

  if (check(scene.name, ok, detail)) {
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${scene.name}-form.png` })
    written++
    if (scene.noteCrop) {
      await page.screenshot({ path: `${OUT}/${scene.name}-note.png`, clip: await noteClip(page) })
      written++
    }
  }
  await page.close()
}

await browser.close()
if (failed) {
  console.error('at least one frame did not match its expected state; no stale evidence was written for it')
  process.exit(1)
}
console.log(`ALL GREEN — wrote ${written} frames to ${OUT}/`)
