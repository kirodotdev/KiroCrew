/**
 * Screenshots of the per-state crew avatar work (capture/crew-avatar-expressions.html).
 *
 * Self-checking before every frame: the states scene must render four DISTINCT
 * faces (a reaction layer that silently resolved to the idle face would
 * photograph as four identical ghosts and look correct), and the builder scene
 * must be on the Expressions tab with all three state rows present. A screenshot
 * of the wrong state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort    # in another shell
 *   node scripts/capture-crew-avatar-expressions.mjs http://127.0.0.1:6831 ../temp-screenshots/crew-avatar-expressions
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/crew-avatar-expressions'
mkdirSync(OUT, { recursive: true })

const STATE_ROWS = ['working', 'done', 'error']

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 940 }, deviceScaleFactor: 2 })

/** Every rendered ghost's data URI, in document order. */
const faces = () =>
  page.$$eval('img', imgs => imgs.map(i => i.getAttribute('src') || '').filter(s => s.startsWith('data:image/svg')))

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/crew-avatar-expressions.html?scene=states&theme=${theme}`)
  await page.waitForFunction(() => document.body.innerText.includes('One crew, four moments'))
  const shown = await faces()
  if (shown.length < 8) throw new Error(`expected 8 composed faces, got ${shown.length}`)
  // The first four are the pinned crew's idle/working/done/error.
  const distinct = new Set(shown.slice(0, 4))
  if (distinct.size !== 4) {
    throw new Error(`the four states rendered ${distinct.size} distinct faces — the overlay is not applying`)
  }
  await page.screenshot({ path: join(OUT, `states-${theme}.png`), fullPage: true })
  console.log(`captured states-${theme}.png`)
}

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/crew-avatar-expressions.html?scene=builder&theme=${theme}`)
  await page.getByRole('button', { name: 'Expressions' }).click()
  await page.getByTestId('avatar-expressions-pane').waitFor()
  for (const state of STATE_ROWS) {
    await page.getByTestId(`avatar-state-row-${state}`).waitFor()
    await page.getByTestId(`avatar-state-preview-${state}`).waitFor()
    await page.getByTestId(`avatar-state-sound-${state}`).waitFor()
  }
  // i18next returns the key itself for a missing key, so a pane full of
  // `components.avatarBuilder.*` renders as a plausible UI and photographs as
  // one. Assert the copy, not just the structure.
  const paneText = await page.getByTestId('avatar-expressions-pane').innerText()
  if (paneText.includes('components.avatarBuilder')) {
    throw new Error(`raw catalog keys are rendering instead of copy:\n${paneText}`)
  }
  for (const label of ['Working', 'Finished', 'Failed', 'Sound', 'Eyes', 'Mouth']) {
    if (!paneText.includes(label)) throw new Error(`the pane is missing its "${label}" label`)
  }
  // The stored record pins an expression for every state, so no row may still
  // read "same as usual" on both axes.
  const selected = await page.locator('[data-testid^="avatar-expr-opt-"][aria-selected="true"]').count()
  if (selected !== STATE_ROWS.length * 2) {
    throw new Error(`expected ${STATE_ROWS.length * 2} pre-filled picks, got ${selected}`)
  }
  await page.screenshot({ path: join(OUT, `builder-${theme}.png`) })
  console.log(`captured builder-${theme}.png`)
}

// Narrow width: the pane's option rows scroll horizontally rather than clipping.
await page.setViewportSize({ width: 390, height: 900 })
await page.goto(`${BASE}/capture/crew-avatar-expressions.html?scene=builder&theme=dark`)
await page.getByRole('button', { name: 'Expressions' }).click()
await page.getByTestId('avatar-expressions-pane').waitFor()
await page.screenshot({ path: join(OUT, 'builder-narrow-390.png') })
console.log('captured builder-narrow-390.png')

await browser.close()
console.log(`done → ${OUT}`)
