/**
 * Screenshots of the workflow-library change cards from the isolated capture entry
 * website/capture/workflow-save-card.html: a `workflow.save` card (name,
 * description, command and the whole script), a `workflow.update` card (revision
 * step and the script diff, then its Details opened on the whole new script), an
 * update whose two far-apart hunks are split by a separator row, and an applied
 * save, each light and dark.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort   # in another shell
 *   node scripts/capture-workflow-save-card.mjs http://127.0.0.1:6815 ../temp-screenshots/workflow-save-card
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/workflow-save-card'
mkdirSync(OUT, { recursive: true })

const SCENES = ['save', 'update', 'update-far', 'applied']
const THEMES = ['dark', 'light']
/** The script blocks each scene must show before Details is opened. */
const CODE_BLOCKS = { save: 1, update: 0, 'update-far': 0, applied: 0 }
/** The diff rows the update scene must show, by kind, in order. */
const DIFF_KINDS = 'context,context,context,remove,add,context,context,context,context,remove,add'
/** The far-apart update: each hunk opens with a separator row naming its lines. */
const FAR_GAPS = 'Unchanged lines hidden · showing lines 2–8|Unchanged lines hidden · showing lines 21–27'
/** Scenes whose script or diff has a line wider than the card: each says so under the box. */
const OVERFLOW_NOTES = { save: 1, update: 1, 'update-far': 1, applied: 0 }
const FAR_KINDS = 'context,context,context,remove,add,context,context,context,context,context,context,add,context,context,context'
/** The save scene's script is nine source lines, one numbered row each. */
const SAVE_LINE_NUMBERS = '1,2,3,4,5,6,7,8,9'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const shot = async (locator, name) => {
  const path = `${OUT}/${name}.png`
  await locator.screenshot({ path })
  return path
}
for (const scene of SCENES) {
  const frames = {}
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 760, height: 900 }, deviceScaleFactor: 2 })
    page.on('pageerror', e => { console.error(`  pageerror: ${e.message}`); failures++ })
    await page.goto(`${BASE}/capture/workflow-save-card.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
    const root = page.locator('[data-capture-root]')
    await page.locator('[data-testid="change-card"]').waitFor()
    await page.waitForTimeout(400)
    const blocks = await page.locator('[data-testid="change-card-code"]:visible').count()
    if (blocks !== CODE_BLOCKS[scene]) {
      console.error(`FAIL: scene=${scene} theme=${theme} shows ${blocks} code block(s), want ${CODE_BLOCKS[scene]}`); failures++
    }
    // Script and diff lines never wrap: each source line is one row.
    const wrapped = await page.locator('[data-testid="change-card-line-text"]:visible').evaluateAll(
      els => els.filter(e => getComputedStyle(e).whiteSpace !== 'pre').length)
    if (wrapped) { console.error(`FAIL: scene=${scene} theme=${theme} has ${wrapped} wrapping line(s)`); failures++ }
    if (scene === 'save') {
      const numbers = await page.locator('[data-testid="change-card-line-number"]').allTextContents()
      if (numbers.join(',') !== SAVE_LINE_NUMBERS) { console.error(`FAIL: save line numbers are ${numbers.join(',')}, want ${SAVE_LINE_NUMBERS}`); failures++ }
    }
    const notes = await page.locator('[data-testid="change-card-code-overflow"]:visible').count()
    if (notes !== OVERFLOW_NOTES[scene]) {
      console.error(`FAIL: scene=${scene} theme=${theme} shows ${notes} overflow line(s), want ${OVERFLOW_NOTES[scene]}`); failures++
    }
    if (scene === 'update') {
      const kinds = await page.locator('[data-testid="change-card-diff-line"]').evaluateAll(rows => rows.map(r => r.getAttribute('data-kind')).join(','))
      if (kinds !== DIFF_KINDS) { console.error(`FAIL: update diff rows are ${kinds}, want ${DIFF_KINDS}`); failures++ }
      // Its one hunk opens at line 1, so nothing above it is hidden and no gap row is drawn.
      const gaps = await page.locator('[data-testid="change-card-diff-gap"]').count()
      if (gaps !== 0) { console.error(`FAIL: update draws ${gaps} gap row(s) for a hunk at line 1, want 0`); failures++ }
    }
    if (scene === 'update-far') {
      const gaps = await page.locator('[data-testid="change-card-diff-gap"]').evaluateAll(
        els => els.map(e => e.textContent.replace(/\u22ef/g, '').trim()).join('|'))
      if (gaps !== FAR_GAPS) { console.error(`FAIL: update-far separators are ${gaps}, want ${FAR_GAPS}`); failures++ }
      const kinds = await page.locator('[data-testid="change-card-diff-line"]').evaluateAll(rows => rows.map(r => r.getAttribute('data-kind')).join(','))
      if (kinds !== FAR_KINDS) { console.error(`FAIL: update-far diff rows are ${kinds}, want ${FAR_KINDS}`); failures++ }
      const roles = await page.locator('[role="separator"][aria-label]').count()
      if (roles !== 2) { console.error(`FAIL: update-far has ${roles} labelled separator(s), want 2`); failures++ }
    }
    frames[theme] = await shot(root, `${scene}-${theme}`)
    if (scene === 'update') {
      await page.locator('[data-testid="change-card-details"] summary').click()
      await page.waitForTimeout(300)
      const opened = await page.locator('[data-testid="change-card-code"]:visible').count()
      if (opened !== 1) { console.error(`FAIL: update details shows ${opened} code block(s), want 1`); failures++ }
      await shot(root, `update-details-${theme}`)
    }
    console.log(scene, theme, JSON.stringify((await root.textContent())?.slice(0, 200)))
    await page.close()
  }
  if (readFileSync(frames.dark).equals(readFileSync(frames.light))) {
    console.error(`FAIL: scene=${scene} light and dark frames are identical`); failures++
  }
}
await browser.close()
console.log(`wrote ${OUT}`)
process.exit(failures ? 1 : 0)
