/** Real-browser evidence for the type-first selection composer on the artifact surfaces.
 *
 * Drives the ISOLATED capture entry (website/capture/artifact-selection-composer.html),
 * which mounts the REAL `ArtifactDetailPage` / `ArtifactPanel` over a
 * fixture-backed fetch boundary. What a screenshot pair cannot fake is asserted
 * at every stage, per theme:
 *
 *  page-markdown-1-open:      selecting "two weeks" in the rendered body opens the
 *                             comment box at once — no Comment button first — and
 *                             the passage stays highlighted under the box.
 *  page-markdown-2-typed:     a comment typed into the box.
 *  page-markdown-3-posted:    Enter posts it; the POST carries the quote + the
 *                             rendered-text offsets (the page's anchor shape); the
 *                             sidebar shows the comment.
 *  page-widget-1-open:        a selection made INSIDE the sandboxed iframe reaches the
 *                             page through the real bridge and opens the same box.
 *  page-widget-2-posted:      the anchor posted from the frame is quote + context only.
 *  panel-markdown-1-open:     the chat side panel opens the same box.
 *  panel-markdown-2-posted:   ...and posts the anchored comment.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort    # in another shell (website/)
 *   node scripts/capture-artifact-selection-composer.mjs http://127.0.0.1:6823 ../temp-screenshots/artifact-selection-composer
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/artifact-selection-composer'
mkdirSync(OUT, { recursive: true })

const PAGE_VIEWPORT = { width: 1280, height: 800 }
/** Dock width of the real side panel. */
const PANEL_VIEWPORT = { width: 460, height: 720 }
const COMPOSER_INPUT = 'Comment on the selected text'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

/** Select `word` inside the first text node of `root` that contains it, as a
 *  real DOM selection, then release the mouse over it — the gesture the toolbar
 *  listens for. Runs in the page (or in a frame). */
const selectWord = (word) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node = null
  while (walker.nextNode()) {
    if ((walker.currentNode.textContent ?? '').includes(word)) { node = walker.currentNode; break }
  }
  if (!node) return false
  const start = node.textContent.indexOf(word)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  const sel = window.getSelection()
  sel.removeAllRanges()
  sel.addRange(range)
  const r = range.getBoundingClientRect()
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: r.right, clientY: r.bottom }))
  return true
}

/** No stray dialog may sit over a frame we are about to shoot. */
async function assertNoDialog(page, label) {
  check(`[${label}] no unexpected dialog`, (await page.getByRole('dialog').count()) === 0)
}

async function open(page, theme, host, kind) {
  page.on('pageerror', e => { console.error(`[${theme}/${host}/${kind}] pageerror:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/artifact-selection-composer.html?theme=${theme}&host=${host}&kind=${kind}`, { waitUntil: 'networkidle' })
}

for (const theme of ['dark', 'light']) {
  // ── Full artifact page, markdown body ──
  {
    const label = `${theme}/page-markdown`
    const page = await browser.newPage({ viewport: PAGE_VIEWPORT })
    await open(page, theme, 'page', 'markdown')
    await page.getByText('Ship the review workflow', { exact: false }).waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)

    check(`[${label}] selection opens the composer`, await page.evaluate(selectWord, 'two weeks'))
    const input = page.getByLabel(COMPOSER_INPUT)
    await input.waitFor({ state: 'visible', timeout: 5000 })
    // The input IS the comment affordance: no Comment button to find first.
    check(`[${label}] no Comment button`, (await page.getByRole('button', { name: /^Comment$/ }).count()) === 0)
    // Two controls in the row (Add comment, Close) and no third button beside
    // the box. Scoped to the box: the page's own sidebar and header carry an
    // "Add comment" and a "Copy content" button of their own.
    // The passage stays marked once the input took focus and collapsed the
    // selection: the annotate highlight covers exactly the selected text.
    check(`[${label}] the annotated passage is highlighted`,
      await page.evaluate(() => { const h = CSS.highlights?.get('mc-annotate'); if (!h) return false; return Array.from(h).map(r => r.toString()).join('') === 'two weeks' }))
    const box = page.getByTestId('selection-composer')
    check(`[${label}] row holds Add comment + Close only`,
      (await box.getByRole('button', { name: 'Add comment' }).count()) === 1
      && (await box.getByRole('button', { name: 'Close' }).count()) === 1
      && (await box.getByRole('button').count()) === 2)
    await assertNoDialog(page, label)
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/${theme}-page-markdown-1-open.png` })

    await input.fill('Two weeks feels short — justify the window.')
    await page.waitForTimeout(150)
    await page.screenshot({ path: `${OUT}/${theme}-page-markdown-2-typed.png` })

    // Edit unmounts the toolbar and the box with it: over a typed draft the page
    // asks first, in the draft's own words. Cancel keeps the text in the box.
    await page.getByTitle(/edit content/i).click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 5000 })
    check(`[${label}] Edit over a draft asks "Discard your unsaved comment?"`,
      (await dialog.getByText('Discard your unsaved comment?').count()) === 1
      && (await dialog.getByRole('button', { name: 'Discard comment' }).count()) === 1
      && (await dialog.getByRole('button', { name: 'Cancel' }).count()) === 1)
    await page.waitForTimeout(250)
    await page.screenshot({ path: `${OUT}/${theme}-page-markdown-2b-discard-prompt.png` })
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await dialog.waitFor({ state: 'hidden', timeout: 5000 })
    check(`[${label}] Cancel keeps the draft`, (await input.inputValue()) === 'Two weeks feels short — justify the window.')
    check(`[${label}] Cancel stays out of edit mode`, (await page.getByRole('button', { name: /^Save$/ }).count()) === 0)

    await input.press('Enter')
    await input.waitFor({ state: 'detached', timeout: 5000 })
    await page.getByText('Two weeks feels short').first().waitFor({ timeout: 10000 })
    const posted = await page.evaluate(() => window.__posted)
    const anchor = posted[0]?.anchor ?? {}
    check(`[${label}] posted anchor pins the selection`,
      posted.length === 1 && anchor.quote === 'two weeks'
      && typeof anchor.start_offset === 'number' && anchor.end_offset === anchor.start_offset + 'two weeks'.length)
    await assertNoDialog(page, label)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${theme}-page-markdown-3-posted.png` })
    await page.close()
  }

  // ── Full artifact page: a typed draft survives leaving the page ──
  {
    const label = `${theme}/page-markdown-draft`
    const page = await browser.newPage({ viewport: PAGE_VIEWPORT })
    await open(page, theme, 'page', 'markdown')
    await page.getByText('Ship the review workflow', { exact: false }).waitFor({ timeout: 15000 })
    await page.evaluate(() => sessionStorage.clear())
    await page.waitForTimeout(300)
    check(`[${label}] selection opens the composer`, await page.evaluate(selectWord, 'remaining teams'))
    const input = page.getByLabel(COMPOSER_INPUT)
    await input.waitFor({ state: 'visible', timeout: 5000 })
    await input.fill('Which teams are "remaining"? List them.')
    await page.waitForTimeout(150)
    await page.screenshot({ path: `${OUT}/${theme}-page-markdown-4-draft-typed.png` })

    // A full reload is the teardown no in-page guard can catch: the toolbar is
    // gone with the document. Selecting the same passage again brings the
    // draft back from the per-artifact, per-passage store.
    await page.reload({ waitUntil: 'networkidle' })
    await page.getByText('Ship the review workflow', { exact: false }).waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
    check(`[${label}] a fresh document shows no box until a selection`, (await page.getByLabel(COMPOSER_INPUT).count()) === 0)
    check(`[${label}] re-selecting the passage opens the composer`, await page.evaluate(selectWord, 'remaining teams'))
    const restored = page.getByLabel(COMPOSER_INPUT)
    await restored.waitFor({ state: 'visible', timeout: 5000 })
    check(`[${label}] the draft came back for the same passage`, (await restored.inputValue()) === 'Which teams are "remaining"? List them.')
    // Escape over the restored draft asks the same question; Cancel keeps it.
    await restored.press('Escape')
    const dlg = page.getByRole('dialog')
    await dlg.waitFor({ state: 'visible', timeout: 5000 })
    check(`[${label}] Escape over the restored draft asks first`, (await dlg.getByText('Discard your unsaved comment?').count()) === 1)
    await dlg.getByRole('button', { name: 'Cancel' }).click()
    await dlg.waitFor({ state: 'hidden', timeout: 5000 })
    await assertNoDialog(page, label)
    await page.waitForTimeout(250)
    await page.screenshot({ path: `${OUT}/${theme}-page-markdown-5-draft-restored.png` })
    await page.close()
  }

  // ── Full artifact page, widget (sandboxed iframe) body ──
  {
    const label = `${theme}/page-widget`
    const page = await browser.newPage({ viewport: PAGE_VIEWPORT })
    await open(page, theme, 'page', 'widget')
    const frameEl = page.locator('iframe').first()
    await frameEl.waitFor({ timeout: 15000 })
    const frame = await (await frameEl.elementHandle()).contentFrame()
    await frame.getByText('Ship the review workflow', { exact: false }).waitFor({ timeout: 15000 })
    // Give the bridge its `mc-comment-ready` round trip.
    await page.waitForTimeout(500)

    check(`[${label}] in-frame selection reaches the page`, await frame.evaluate(selectWord, 'Pilot with one team'))
    const input = page.getByLabel(COMPOSER_INPUT)
    await input.waitFor({ state: 'visible', timeout: 5000 })
    await assertNoDialog(page, label)
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/${theme}-page-widget-1-open.png` })

    await input.fill('Which team? Name it.')
    await input.press('Enter')
    await input.waitFor({ state: 'detached', timeout: 5000 })
    await page.getByText('Which team? Name it.').first().waitFor({ timeout: 10000 })
    const posted = await page.evaluate(() => window.__posted)
    const anchor = posted[0]?.anchor ?? {}
    check(`[${label}] frame anchor is quote + context, no parent offsets`,
      posted.length === 1 && anchor.quote === 'Pilot with one team' && typeof anchor.prefix === 'string'
      && typeof anchor.suffix === 'string' && anchor.start_offset === undefined)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${theme}-page-widget-2-posted.png` })
    await page.close()
  }

  // ── Chat side panel, markdown body ──
  {
    const label = `${theme}/panel-markdown`
    const page = await browser.newPage({ viewport: PANEL_VIEWPORT })
    await open(page, theme, 'panel', 'markdown')
    await page.getByText('Ship the review workflow', { exact: false }).waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)

    check(`[${label}] selection opens the composer`, await page.evaluate(selectWord, 'rollout checklist'))
    const input = page.getByLabel(COMPOSER_INPUT)
    await input.waitFor({ state: 'visible', timeout: 5000 })
    check(`[${label}] no Comment button`, (await page.getByRole('button', { name: /^Comment$/ }).count()) === 0)
    check(`[${label}] the annotated passage is highlighted`,
      await page.evaluate(() => { const h = CSS.highlights?.get('mc-annotate'); if (!h) return false; return Array.from(h).map(r => r.toString()).join('') === 'rollout checklist' }))
    await assertNoDialog(page, label)
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/${theme}-panel-markdown-1-open.png` })

    await input.fill('Who owns the checklist?')
    await input.press('Enter')
    await input.waitFor({ state: 'detached', timeout: 5000 })
    const posted = await page.evaluate(() => window.__posted)
    const anchor = posted[0]?.anchor ?? {}
    check(`[${label}] posted anchor pins the selection`,
      posted.length === 1 && anchor.quote === 'rollout checklist' && typeof anchor.start_offset === 'number')
    // The panel's comment badge counts the new comment.
    await page.getByRole('button', { name: /comments/i }).first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${theme}-panel-markdown-2-posted.png` })
    await page.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
