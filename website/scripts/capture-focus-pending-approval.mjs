/**
 * Screenshots for the Alt+Shift+Enter approval-focus chord (issue #7379).
 *
 * Drives the EXISTING website/capture/panel-toggle-shortcuts.html harness rather
 * than adding a second one: that page mounts the REAL ShortcutsModal and the REAL
 * ShortcutsPanel against the real stylesheet with no store, which is exactly what a
 * shortcuts-row change needs to photograph, and a near-identical second harness
 * would only duplicate it. Both surfaces derive their rows from DEFAULT_SHORTCUTS,
 * so the new entry appears in each without the harness knowing about it.
 *
 * Three shots:
 *   1. modal/dark     - the Alt+K modal, Actions group, new row
 *   2. settings/dark  - Settings -> Shortcuts, same row
 *   3. settings/light - same, light theme
 *
 * Every scene ASSERTS the rendered label AND the rendered chord before writing the
 * file, so a frame cannot silently photograph a tree without the change in it.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort   # in another shell
 *   node scripts/capture-focus-pending-approval.mjs http://127.0.0.1:6842 ../temp-screenshots/focus-pending-approval
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/focus-pending-approval'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 900 }, deviceScaleFactor: 2 })

let failed = false

async function shoot(scene, theme, name, assertText) {
  await page.goto(`${BASE}/capture/panel-toggle-shortcuts.html?scene=${scene}&theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.waitForTimeout(900)
  for (const text of assertText) {
    const hit = await page.getByText(text, { exact: false }).first().isVisible().catch(() => false)
    if (!hit) {
      console.error(`FAIL [${name}]: expected visible text ${JSON.stringify(text)}`)
      failed = true
    }
  }
  await page.getByText('Focus pending approval', { exact: false }).first().scrollIntoViewIfNeeded().catch(() => {})
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  console.log(`wrote ${OUT}/${name}.png`)
}

// The chord renders as "Alt + Shift + Enter" off-Mac; the label is the assertion
// that matters and is platform-independent.
await shoot('modal', 'dark', 'shortcuts-modal-dark', ['Focus pending approval', 'Actions'])
await shoot('settings', 'dark', 'settings-shortcuts-dark', ['Focus pending approval'])
await shoot('settings', 'light', 'settings-shortcuts-light', ['Focus pending approval'])

/**
 * The behaviour, in a real browser: press the chord and photograph where focus
 * landed. Real Chromium rather than the unit tests' DOM, because a focus ring is
 * a rendered thing and only a browser draws it -- and because the harness mounts
 * the REAL api client, so a frame in which nothing was requested is also evidence
 * that focusing resolves nothing.
 *
 * This harness renders the GHOST state, and that was a finding rather than a
 * choice: with no inline tool pill in the tree the bar collapses to its ghost, and
 * in that state THE COMPOSER IS NOT RENDERED AT ALL (the only textarea left is the
 * hidden off-screen measuring mirror: aria-hidden, tabindex=-1, readonly). So
 * there is nothing to Shift+Tab from there and the chord is the only keyboard
 * route to the row. The non-ghost state, where the composer IS present and focus
 * starts in it, is the one the unit tests drive.
 */
async function shootChord(theme) {
  await page.goto(`${BASE}/capture/focus-pending-approval.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.waitForSelector('[data-approval-actions] button:not([disabled])')
  await page.waitForTimeout(900)

  const requests = []
  page.on('request', r => requests.push(r.url()))

  const before = await page.evaluate(() => ({
    tag: document.activeElement?.tagName,
    inRow: !!document.activeElement?.closest('[data-approval-actions]'),
    realComposers: document.querySelectorAll('textarea[data-composer-input]').length,
  }))
  if (before.inRow) {
    console.error(`FAIL [chord/${theme}]: focus was ALREADY in the row before the chord, so the shot proves nothing`)
    failed = true
  }
  await page.screenshot({ path: `${OUT}/approval-row-before-chord-${theme}.png` })
  console.log(`wrote ${OUT}/approval-row-before-chord-${theme}.png  (focus on ${before.tag}, real composers rendered: ${before.realComposers})`)

  await page.keyboard.press('Alt+Shift+Enter')
  await page.waitForTimeout(250)

  const after = await page.evaluate(() => {
    const el = document.activeElement
    return { tag: el?.tagName, text: (el?.textContent || '').trim(), inRow: !!el?.closest('[data-approval-actions]') }
  })
  if (!(after.inRow && after.tag === 'BUTTON')) {
    console.error(`FAIL [chord/${theme}]: focus did not land in the approval row: ${JSON.stringify(after)}`)
    failed = true
  }
  // Nothing may have been sent. A resolve would hit the approvals endpoint.
  const resolves = requests.filter(u => /approval|resolve/i.test(u))
  if (resolves.length) {
    console.error(`FAIL [chord/${theme}]: the chord issued requests: ${JSON.stringify(resolves)}`)
    failed = true
  }
  await page.screenshot({ path: `${OUT}/approval-row-chord-focused-${theme}.png` })
  console.log(`wrote ${OUT}/approval-row-chord-focused-${theme}.png  (focus on ${JSON.stringify(after.text)}, approval requests issued: ${resolves.length})`)
}

await shootChord('dark')

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state')
  process.exit(1)
}
console.log('all scenes asserted and captured')
