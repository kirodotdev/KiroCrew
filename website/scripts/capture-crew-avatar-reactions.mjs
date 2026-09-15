/**
 * Screenshots of the crew avatar's reaction layer (capture/crew-avatar-reactions.html).
 *
 * Self-checking before every frame, because a reaction that silently resolved to
 * the resting face photographs as a correct-looking ghost. The states scene must
 * render four DISTINCT faces AND carry the keyframes of each chosen motion in the
 * composed markup; the builder scene must be on the Reactions tab with all three
 * state rows, and its two motion selects must show the picks the record stores. A
 * screenshot of the wrong state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort    # in another shell
 *   node scripts/capture-crew-avatar-reactions.mjs http://127.0.0.1:6831 ../temp-screenshots/crew-avatar-reactions
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/crew-avatar-reactions'
mkdirSync(OUT, { recursive: true })

const STATE_ROWS = ['working', 'done', 'error']
/** The keyframe names the pinned crew's two chosen reactions must animate with. */
const CHOSEN_KEYFRAMES = ['kg-swell', 'kg-droop']

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 1180 }, deviceScaleFactor: 2 })

/**
 * The library list, answered here.
 *
 * There is no gateway behind this page, and the Library pane is one of the two
 * that owe the user an explanation for the missing Reactions tab — so the frame
 * has to show that pane as a user sees it, not as a failed read. Only the LIST is
 * answered: the built-in card composes its own thumbnail locally (its slot route
 * is a deliberate 404), so no art has to be invented here.
 */
const LIBRARY = {
  packs: [
    { id: 'kiro-ghost', name: 'Kiro ghost', author: 'Kiro Crew', type: 'builtin', format: 'svg' },
  ],
}
await page.route('**/api/appearances', route =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(LIBRARY) }),
)

/** Every rendered ghost's data URI, in document order. */
const faces = () =>
  page.$$eval('img', imgs => imgs.map(i => i.getAttribute('src') || '').filter(s => s.startsWith('data:image/svg')))

/**
 * Every tab label in the strip, with the numbers that decide whether it is CUT
 * OFF rather than merely narrow.
 *
 * `innerText` is the wrong instrument and was the bug in the first version of
 * this script: the DOM holds the whole string while the label's own box clips it
 * (`SegmentedControl` animates label width under `overflow-hidden
 * whitespace-nowrap`), so a frame showing "Reaction" passed an assertion reading
 * "Reactions". `scrollWidth > clientWidth` is the same question the pixels
 * answer.
 */
const tabLabels = () =>
  page.$$eval('button span.whitespace-nowrap', spans =>
    spans.map(el => ({
      text: (el.textContent || '').trim(),
      overflow: el.scrollWidth - el.clientWidth,
    })),
  )

/**
 * Which tab the strip is DRAWING as the current one, by the class that colours
 * it. Asserted alongside the pane's own testid because those are two different
 * claims: the pane can be the picture's while the strip still paints another tab
 * as selected, and a reader believes the strip.
 */
const activeTab = () =>
  page.$$eval('[data-testid="avatar-builder-tabs"] button', buttons =>
    buttons
      .filter(b => b.className.includes('text-accent'))
      .map(b => (b.getAttribute('title') || b.textContent || '').trim()),
  )

/** Throws unless every tab label is drawn whole. */
const assertActiveTab = async (where, expected) => {
  const on = await activeTab()
  if (on.length !== 1 || on[0] !== expected) {
    throw new Error(`${where}: the strip carries the selected CLASS on ${JSON.stringify(on)}, expected ["${expected}"]`)
  }
  // The class lands one frame after the click; the COLOUR does not.
  // `SegmentedControl` crossfades it (`transition-colors`, ~150ms), so a shutter
  // that fires on the class alone caught the previous tab still accent-coloured —
  // and a reader of that frame could not tell which pane they were looking at.
  // Same DOM-vs-pixels trap the label measurement documents, one property along.
  // Settled here means two reads 150ms apart AGREE, and the expected tab's colour
  // is unique in the strip: exactly one tab may look selected.
  await page.waitForFunction(
    async (name) => {
      const strip = document.querySelector('[data-testid="avatar-builder-tabs"]')
      if (!strip) return false
      const tabs = [...strip.querySelectorAll('button')]
      const mine = tabs.findIndex(b => (b.getAttribute('title') || b.textContent || '').trim() === name)
      if (mine < 0) return false
      const read = () => tabs.map(b => getComputedStyle(b).color)
      const before = read()
      await new Promise(done => setTimeout(done, 150))
      const after = read()
      if (before.some((colour, i) => colour !== after[i])) return false
      return after.filter(colour => colour === after[mine]).length === 1
    },
    expected,
    { timeout: 5000 },
  )
  console.log(`${where}: strip selection is "${expected}", colour settled`)
}

const assertNoClippedTab = async (where) => {
  const labels = await tabLabels()
  if (!labels.length) throw new Error(`${where}: found no tab labels to measure`)
  // 1px of slack: a fractional layout width rounds up into scrollWidth on a
  // fractional device pixel ratio, and that is not a clip.
  const clipped = labels.filter(l => l.overflow > 1)
  if (clipped.length) {
    throw new Error(
      `${where}: tab label(s) clipped — ${clipped.map(l => `"${l.text}" by ${l.overflow}px`).join(', ')}`,
    )
  }
  console.log(`${where}: tabs drawn whole — ${labels.map(l => l.text).join(' | ')}`)
}

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=states&theme=${theme}`)
  await page.waitForFunction(() => document.body.innerText.includes('One crew, four moments'))
  const shown = await faces()
  if (shown.length < 8) throw new Error(`expected at least 8 composed faces, got ${shown.length}`)
  // The first four are the pinned crew's idle/working/done/error.
  const distinct = new Set(shown.slice(0, 4))
  if (distinct.size !== 4) {
    throw new Error(`the four states rendered ${distinct.size} distinct faces — the layer is not applying`)
  }
  // A still frame cannot show motion, so assert the motion is THERE: the done and
  // error faces must carry their own keyframes, not just differ from idle.
  const [, , done, error] = shown.slice(0, 4).map(decodeURIComponent)
  for (const [markup, keyframe] of [[done, CHOSEN_KEYFRAMES[0]], [error, CHOSEN_KEYFRAMES[1]]]) {
    if (!markup.includes(`@keyframes ${keyframe}`)) {
      throw new Error(`the composed face is missing its ${keyframe} animation`)
    }
  }
  await page.screenshot({ path: join(OUT, `states-${theme}.png`), fullPage: true })
  console.log(`captured states-${theme}.png`)
}

// The tab strip animates each label's WIDTH while clipping overflow, so a frame
// taken before the spring lands photographs labels that are cut off and are not
// cut off a moment later. `SegmentedControl` honours `prefers-reduced-motion`
// for exactly this reason, so the builder scenes are captured with it set: the
// labels are final on the first paint and the frames are deterministic. The
// states scene above deliberately keeps motion enabled, because what it is
// evidence OF is the motion.
await page.emulateMedia({ reducedMotion: 'reduce' })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=builder&theme=${theme}`)
  // `exact`: the footer's reset link reads "Reset face and reactions", and
  // Playwright's accessible-name match is a substring by default.
  await page.getByRole('button', { name: 'Reactions', exact: true }).click()
  await page.getByTestId('avatar-reactions-pane').waitFor()
  for (const state of STATE_ROWS) {
    await page.getByTestId(`avatar-state-row-${state}`).waitFor()
    await page.getByTestId(`avatar-state-sound-${state}`).waitFor()
  }
  // Only the two reacting states carry a motion select and a preview; `working`
  // has neither, and a frame showing three would mean the pane grew a control
  // for an animation the ghost does not own.
  for (const state of ['done', 'error']) {
    await page.getByTestId(`avatar-state-motion-${state}`).waitFor()
    await page.getByTestId(`avatar-state-preview-${state}`).waitFor()
  }
  if (await page.getByTestId('avatar-state-motion-working').count()) {
    throw new Error('the working row grew a motion select')
  }
  // i18next returns the key itself for a missing key, so a pane full of
  // `components.avatarBuilder.*` renders as a plausible UI and photographs as
  // one. Assert the copy, not just the structure.
  const paneText = await page.getByTestId('avatar-reactions-pane').innerText()
  if (paneText.includes('components.avatarBuilder')) {
    throw new Error(`raw catalog keys are rendering instead of copy:\n${paneText}`)
  }
  for (const label of ['Working', 'Finished', 'Failed', 'Motion', 'Sound', 'Sparkle', 'Droop']) {
    if (!paneText.includes(label)) throw new Error(`the pane is missing its "${label}" label`)
  }
  // The hint names all three moments, `working` included, and says the working
  // sound fires when work STARTS rather than for as long as it runs -- the cue
  // plays once on the transition. Pinned as a phrase because a screenshot cannot
  // show which revision of a sentence it caught, so the script names the
  // revision it is evidence of.
  if (!paneText.includes('when work starts')) {
    throw new Error(`the pane is showing older hint copy:\n${paneText}`)
  }
  await assertNoClippedTab(`builder-${theme}`)
  await assertActiveTab(`builder-${theme}`, 'Reactions')
  await page.screenshot({ path: join(OUT, `builder-${theme}.png`) })
  console.log(`captured builder-${theme}.png`)
}

// The Reactions tab is the ghost's: a picture must not offer it at all — and the
// absence is EXPLAINED, so the frame has to show the line that explains it.
// `retiredCue` is on for this scene because that is the one crew the line matters
// to: one saved with a chime it no longer plays -- saved ON THIS tier, so the
// frame cannot show the other tier's explanation leaking across.
await page.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=builder&theme=dark&retiredCue=image`)
await page.getByRole('button', { name: 'Picture' }).click()
await page.getByTestId('avatar-upload-pane').waitFor()
if (await page.getByRole('button', { name: 'Reactions', exact: true }).count()) {
  throw new Error('the Picture tier is offering a Reactions tab')
}
for (const id of ['avatar-reactions-absent-picture', 'avatar-reactions-retired-cue-picture']) {
  const note = page.getByTestId(id)
  await note.waitFor()
  const said = (await note.innerText()).trim()
  if (!said || said.includes('components.avatarBuilder')) {
    throw new Error(`${id} is not rendering its copy: "${said}"`)
  }
  console.log(`  ${id}: ${said}`)
}
await assertNoClippedTab('builder-picture')
await assertActiveTab('builder-picture', 'Picture')
await page.screenshot({ path: join(OUT, 'builder-picture-no-tab.png') })
console.log('captured builder-picture-no-tab.png')

// The SAME pane in light, for the lost-cue callout alone. It is the one element
// on these tiers whose colours are theme-resolved rather than fixed -- it draws
// `--warn` ink on a `--warn-subtle` tint, and those two tokens inflect per theme
// (the dark themes' `--warn-fg` is `#000`, which is how this callout shipped
// black-on-brown for a round). A dark-only frame cannot show that the light
// resolution reads either, so the light one is evidence in its own right.
await page.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=builder&theme=light&retiredCue=image`)
await page.getByRole('button', { name: 'Picture' }).click()
await page.getByTestId('avatar-upload-pane').waitFor()
const lightNote = page.getByTestId('avatar-reactions-retired-cue-picture')
await lightNote.waitFor()
const lightInk = await lightNote.evaluate(el => {
  const style = getComputedStyle(el)
  return { color: style.color, background: style.backgroundColor }
})
// Asserted, not eyeballed: a screenshot reviewer read `#000` on a dark tint as
// "warm pale amber" once, so the frame is accompanied by the resolved values.
if (lightInk.color === 'rgb(0, 0, 0)') {
  throw new Error(`the light callout resolved to pure black ink: ${JSON.stringify(lightInk)}`)
}
await page.screenshot({ path: join(OUT, 'builder-picture-no-tab-light.png') })
console.log(`captured builder-picture-no-tab-light.png (ink ${lightInk.color} on ${lightInk.background})`)

// The vocabulary itself, open. A closed trigger shows the ONE name a crew
// happens to have picked, so the choice on offer ("Still / Bounce / Nod /
// Sparkle") appeared in no frame at all — which is what a reader needs to judge
// whether the layer is worth using.
await page.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=builder&theme=dark`)
await page.getByRole('button', { name: 'Reactions', exact: true }).click()
await page.getByTestId('avatar-reactions-pane').waitFor()
await page.getByTestId('avatar-state-motion-done').getByRole('combobox').click()
// Radix renders the list in a portal; wait for the options rather than a delay.
const OPTION_NAMES = ['Still', 'Bounce', 'Nod', 'Sparkle']
for (const name of OPTION_NAMES) {
  await page.getByRole('option', { name, exact: true }).waitFor()
}
const listed = await page.getByRole('option').allInnerTexts()
if (listed.length !== OPTION_NAMES.length) {
  throw new Error(`the done vocabulary lists ${JSON.stringify(listed)}, expected ${JSON.stringify(OPTION_NAMES)}`)
}
await page.screenshot({ path: join(OUT, 'builder-motion-open.png') })
console.log(`captured builder-motion-open.png (${listed.join(' / ')})`)
await page.keyboard.press('Escape')

// The Library (pack) tier: same absent tab, same two lines. Captured separately
// from the picture tier because the notes are per-pane, and a cold reader told
// the second round they had seen only one of them.
await page.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=builder&theme=dark&retiredCue=pack`)
await page.getByRole('button', { name: 'Library' }).click()
if (await page.getByRole('button', { name: 'Reactions', exact: true }).count()) {
  throw new Error('the Library tier is offering a Reactions tab')
}
for (const id of ['avatar-reactions-absent-pack', 'avatar-reactions-retired-cue-pack']) {
  const note = page.getByTestId(id)
  await note.waitFor()
  const said = (await note.innerText()).trim()
  if (!said || said.includes('components.avatarBuilder')) {
    throw new Error(`${id} is not rendering its copy: "${said}"`)
  }
  console.log(`  ${id}: ${said}`)
}
await assertNoClippedTab('builder-pack')
await assertActiveTab('builder-pack', 'Library')
await page.screenshot({ path: join(OUT, 'builder-pack-no-tab.png') })
console.log('captured builder-pack-no-tab.png')

/**
 * The two narrow widths the repo's own rule names: a phone (390px) and the
 * narrowest supported (320px).
 *
 * What each frame has to prove is not "it fits" but "it is USABLE": the tab
 * labels are whole, the row has STACKED (the preview sits above the controls
 * rather than beside them, which is what leaves the selects the full width), and
 * a motion select is wide enough to read a name in — at 320px the two-column
 * shape left about 64px of trigger, which is where this pair of assertions comes
 * from.
 */
const MIN_SELECT_PX = 140
for (const width of [390, 320]) {
  await page.setViewportSize({ width, height: 900 })
  await page.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=builder&theme=dark`)
  await page.getByRole('button', { name: 'Reactions', exact: true }).click()
  await page.getByTestId('avatar-reactions-pane').waitFor()
  await assertNoClippedTab(`builder-narrow-${width}`)
  await assertActiveTab(`builder-narrow-${width}`, 'Reactions')
  const preview = await page.getByTestId('avatar-state-preview-done').boundingBox()
  const controls = await page.getByTestId('avatar-state-motion-done').boundingBox()
  if (!preview || !controls) throw new Error(`the done row lost a control at ${width}px`)
  if (preview.y + preview.height > controls.y + 1) {
    throw new Error(
      `at ${width}px the row is still side-by-side (preview ends at ${Math.round(preview.y + preview.height)}, ` +
        `controls start at ${Math.round(controls.y)}) — the narrow layout is not applying`,
    )
  }
  const trigger = await page.getByTestId('avatar-state-motion-done').getByRole('combobox').boundingBox()
  if (!trigger || trigger.width < MIN_SELECT_PX) {
    throw new Error(`at ${width}px the motion select is ${Math.round(trigger?.width ?? 0)}px — under ${MIN_SELECT_PX}px`)
  }
  await page.screenshot({ path: join(OUT, `builder-narrow-${width}.png`) })
  console.log(`captured builder-narrow-${width}.png (select ${Math.round(trigger.width)}px)`)
}

/**
 * The moving proof.
 *
 * A reaction IS an animation, so the strongest thing a still frame can do is
 * assert that the keyframes are PRESENT — which the states scene above does, and
 * which is not the same as showing them play. This records that scene instead: a
 * fresh context with motion enabled (the reduced-motion emulation belongs to the
 * builder frames, where a spring caught mid-flight is noise, not evidence), held
 * open long enough for every loop to come round.
 *
 * Playwright names the file itself and only flushes it when the CONTEXT closes,
 * so the rename happens after `close()` and takes whatever `.webm` landed rather
 * than guessing a name.
 */
const CLIP = { width: 900, height: 640 }
const clipDir = join(OUT, '.video')
rmSync(clipDir, { recursive: true, force: true })
const filming = await browser.newContext({
  viewport: CLIP,
  recordVideo: { dir: clipDir, size: CLIP },
})
const stage = await filming.newPage()
await stage.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=states&theme=dark`)
await stage.waitForFunction(() => document.body.innerText.includes('One crew, four moments'))
// Long enough for the slowest built-in loop to play several times over.
await stage.waitForTimeout(6000)
await filming.close()
const filmed = readdirSync(clipDir).filter(f => f.endsWith('.webm'))
if (filmed.length !== 1) throw new Error(`expected one recording, got ${filmed.length}`)
renameSync(join(clipDir, filmed[0]), join(OUT, 'reactions-motion.webm'))
rmSync(clipDir, { recursive: true, force: true })
console.log('recorded reactions-motion.webm')

/**
 * The strip, moving.
 *
 * The Reactions tab is ABSENT on the two served tiers, which means the strip
 * gains and loses a tab as the tier changes — a continuity a still frame cannot
 * show, and the reason a reader of one frame asked "why is Reactions gone".
 * Recorded with motion on, one deliberate click at a time.
 */
const stripDir = join(OUT, '.video-strip')
rmSync(stripDir, { recursive: true, force: true })
const stripFilming = await browser.newContext({
  viewport: CLIP,
  recordVideo: { dir: stripDir, size: CLIP },
})
const strip = await stripFilming.newPage()
await strip.goto(`${BASE}/capture/crew-avatar-reactions.html?scene=builder&theme=dark`)
await strip.getByTestId('avatar-builder-tabs').waitFor()
for (const tab of ['Reactions', 'Picture', 'Library', 'Ghost face', 'Reactions']) {
  await strip.getByRole('button', { name: tab, exact: true }).click()
  // Long enough to read the transition, short enough to keep the clip watchable.
  await strip.waitForTimeout(1100)
}
await stripFilming.close()
const stripFilmed = readdirSync(stripDir).filter(f => f.endsWith('.webm'))
if (stripFilmed.length !== 1) throw new Error(`expected one strip recording, got ${stripFilmed.length}`)
renameSync(join(stripDir, stripFilmed[0]), join(OUT, 'reactions-tab-strip.webm'))
rmSync(stripDir, { recursive: true, force: true })
console.log('recorded reactions-tab-strip.webm')

await browser.close()
console.log(`done → ${OUT}`)
