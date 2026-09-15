/**
 * Screenshots for the Copy affordance in the artifact comment popover.
 *
 *   00-popover-dark      the popover as it opens over a selection: Copy glyph
 *                        beside the close button, empty focused input
 *   01-popover-light     the same in the light theme
 *   02-copied            after clicking Copy: the glyph is a green tick; the
 *                        clipboard holds the selection VERBATIM, whitespace kept
 *   03-shortcut-copied   Ctrl+C in the still-empty input copies the selection
 *   04-copy-failed       the clipboard refused: inline "Copy failed" notice
 *                        under the input, no tick
 *
 * Every frame is gated on an assertion about the RENDERED state, and the two
 * copy frames additionally on the text that reached the (stubbed) clipboard,
 * so a frame can never document a tick over the wrong payload.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort
 *   node scripts/capture-comment-popover-copy.mjs http://127.0.0.1:6842 <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6842'
const OUT = positional[1] || '../temp-screenshots/comment-popover-copy'
mkdirSync(OUT, { recursive: true })

const SELECTED = ' the popover opens the moment the mouse button comes up '

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function open(query) {
  const page = await browser.newPage({ viewport: { width: 620, height: 320 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.log('PAGE ERROR', e.message); failed = true })
  await page.goto(`${BASE}/capture/comment-popover-copy.html?${query}`)
  await page.getByLabel('Add a comment').waitFor({ state: 'visible' })
  await page.waitForTimeout(250)
  return page
}
const root = page => page.locator('[data-capture-root]')
const copyBtn = page => page.getByRole('button', { name: 'Copy', exact: true })
const state = page => page.evaluate(() => ({
  copied: window.__copied,
  tick: !!document.querySelector('button[aria-label="Copy"] .text-ok'),
  focused: document.activeElement?.getAttribute('aria-label'),
  failed: document.body.textContent.includes('Copy failed'),
}))

// 00 / 01 -- the popover as it opens, both themes.
for (const [n, theme] of [['00-popover-dark', 'dark'], ['01-popover-light', 'light']]) {
  const page = await open(`theme=${theme}`)
  const s = await state(page)
  check(`${n} copy button present, no tick`, (await copyBtn(page).count()) === 1 && !s.tick)
  check(`${n} hint names what is copied`, (await copyBtn(page).getAttribute('title')) === 'Copies the selected text — not your comment — to the clipboard')
  check(`${n} input focused`, s.focused === 'Add a comment', String(s.focused))
  await root(page).screenshot({ path: `${OUT}/${n}.png` })
  await page.close()
}

// 02 -- click Copy: payload asserted verbatim, then the tick photographed.
{
  const page = await open('theme=dark')
  await copyBtn(page).click()
  await page.waitForTimeout(120)
  let s = await state(page)
  check('02 clipboard holds the selection verbatim', s.copied[0] === SELECTED, JSON.stringify(s.copied[0]))
  check('02 tick shown, focus kept in input', s.tick && s.focused === 'Add a comment', JSON.stringify([s.tick, s.focused]))
  await root(page).screenshot({ path: `${OUT}/02-copied.png` })
  await page.waitForTimeout(1700)
  s = await state(page)
  check('02 tick reverted', !s.tick)
  await page.close()
}

// 03 -- Ctrl+C in the empty input copies the selection; with a draft it does not.
{
  const page = await open('theme=dark')
  await page.getByLabel('Add a comment').press('Control+c')
  await page.waitForTimeout(120)
  let s = await state(page)
  check('03 shortcut copied the selection', s.copied.length === 1 && s.copied[0] === SELECTED, JSON.stringify(s.copied))
  await root(page).screenshot({ path: `${OUT}/03-shortcut-copied.png` })
  await page.waitForTimeout(1700)
  await page.getByLabel('Add a comment').fill('tighten this sentence')
  await page.getByLabel('Add a comment').press('Control+c')
  await page.waitForTimeout(120)
  s = await state(page)
  check('03 shortcut left alone once a draft exists', s.copied.length === 1, String(s.copied.length))
  await page.close()
}

// 04 -- the clipboard refuses: the inline notice, no tick.
{
  const page = await open('theme=dark&clipboard=refuse')
  await copyBtn(page).click()
  await page.waitForTimeout(200)
  const s = await state(page)
  check('04 failed notice shown, no tick', s.failed && !s.tick && s.copied.length === 0, JSON.stringify(s))
  await root(page).screenshot({ path: `${OUT}/04-copy-failed.png` })
  await page.close()
}

await browser.close()
if (failed) { console.log('FAILED'); process.exit(1) }
console.log(`wrote 5 frames to ${OUT}`)
