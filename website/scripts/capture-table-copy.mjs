/**
 * Screenshots for the copy row under a rendered markdown table.
 *
 *   00-table-resting       the table at rest: no chrome under it -- the row
 *                          is hidden until hover/focus (touch shows it always)
 *   01-table-dark          the table hovered: two labelled buttons,
 *                          right-aligned under the table
 *   02-table-light         the same in the light theme
 *   03-copied-markdown     the Markdown button after a click: green tick,
 *                          label "Copied!", the CSV button unchanged
 *   04-wide-scrolled       a table wider than the frame scrolled to the right:
 *                          the row stays put because it is outside the
 *                          scroll wrapper
 *   05-copy-failed         the clipboard refused: inline "Copy failed" notice
 *                          with its dismiss control, no tick
 *
 * Each frame is gated on an assertion about the RENDERED state -- and frame 03
 * additionally on the text that actually reached the (stubbed) clipboard, so a
 * frame can never document a tick over the wrong payload.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort
 *   node scripts/capture-table-copy.mjs http://127.0.0.1:6841 <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6841'
const OUT = positional[1] || '../temp-screenshots/table-copy'
mkdirSync(OUT, { recursive: true })

const EXPECTED_MD = [
  '| Symbol | Price | MACD Hist | Signal |',
  '| --- | ---: | ---: | :---: |',
  '| `GOOGL` | $344.82 | -0.57 | **STRONG SELL** |',
  '| `AAPL` | $189.10 | 0.12 | HOLD |',
  '| `MSFT` | $412.55 | 1.04 | *BUY* |',
].join('\n')
const EXPECTED_CSV = 'Symbol,Price,MACD Hist,Signal\nGOOGL,$344.82,-0.57,STRONG SELL\nAAPL,$189.10,0.12,HOLD\nMSFT,$412.55,1.04,BUY'

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function open(scene, theme) {
  const page = await browser.newPage({ viewport: { width: 700, height: 420 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.log('PAGE ERROR', e.message); failed = true })
  await page.goto(`${BASE}/capture/table-copy.html?scene=${scene}&theme=${theme}`)
  await page.waitForSelector('[data-capture-root] table')
  await page.getByTestId('table-copy-markdown').waitFor({ state: 'attached' })
  await page.waitForTimeout(200)
  return page
}
/** The row is hover-revealed (like the code block's copy button), so every
 *  frame that shows it hovers the table first and asserts the reveal. */
async function hoverTable(page) {
  await page.hover('[data-capture-root] table')
  await page.waitForTimeout(250)
  const opacity = await page.evaluate(() => getComputedStyle(document.querySelector('[data-testid="table-copy-markdown"]').parentElement).opacity)
  check('row revealed on hover', opacity === '1', `opacity=${opacity}`)
}
const root = page => page.locator('[data-capture-root]')
const labels = page => page.evaluate(() => ({
  md: document.querySelector('[data-testid="table-copy-markdown"]')?.getAttribute('aria-label'),
  csv: document.querySelector('[data-testid="table-copy-csv"]')?.getAttribute('aria-label'),
  copied: window.__copied,
}))

// 00 -- at rest, the row is hidden: no permanent chrome under the table.
{
  const page = await open('default', 'dark')
  const opacity = await page.evaluate(() => getComputedStyle(document.querySelector('[data-testid="table-copy-markdown"]').parentElement).opacity)
  check('00 row hidden at rest', opacity === '0', `opacity=${opacity}`)
  await root(page).screenshot({ path: `${OUT}/00-table-resting.png` })
  await page.close()
}

// 01 / 02 -- the hovered row, both themes.
for (const [n, theme] of [['01-table-dark', 'dark'], ['02-table-light', 'light']]) {
  const page = await open('default', theme)
  await hoverTable(page)
  const l = await labels(page)
  const words = await page.evaluate(() => [document.querySelector('[data-testid="table-copy-markdown"]').textContent, document.querySelector('[data-testid="table-copy-csv"]').textContent])
  check(`${n} visible words`, words[0] === 'Copy Markdown' && words[1] === 'Copy CSV', JSON.stringify(words))
  check(`${n} labels`, l.md === 'Copy table as Markdown' && l.csv === 'Copy table as CSV', JSON.stringify(l))
  // The row is a sibling of the scroll wrapper, never inside it.
  const inWrapper = await page.evaluate(() => !!document.querySelector('.overflow-x-auto [data-testid="table-copy-markdown"]'))
  check(`${n} row outside scroll wrapper`, !inWrapper)
  await root(page).screenshot({ path: `${OUT}/${n}.png` })
  await page.close()
}

// 03 -- click Markdown: payload asserted, then the tick photographed.
{
  const page = await open('default', 'dark')
  await hoverTable(page)
  await page.getByTestId('table-copy-markdown').click()
  await page.waitForTimeout(100)
  let l = await labels(page)
  check('03 markdown payload', l.copied[0] === EXPECTED_MD, JSON.stringify(l.copied[0]))
  check('03 copied label on pressed button only', l.md === 'Copied!' && l.csv === 'Copy table as CSV', JSON.stringify([l.md, l.csv]))
  await root(page).screenshot({ path: `${OUT}/03-copied-markdown.png` })
  // The tick reverts, and the CSV button produces CSV.
  await page.waitForTimeout(1600)
  await page.getByTestId('table-copy-csv').click()
  await page.waitForTimeout(100)
  l = await labels(page)
  check('03 label reverted', l.md === 'Copy table as Markdown', l.md)
  check('03 csv payload', l.copied[1] === EXPECTED_CSV, JSON.stringify(l.copied[1]))
  await page.close()
}

// 04 -- wide table scrolled: the row does not move with the table.
{
  const page = await open('wide', 'dark')
  await hoverTable(page)
  const before = await page.getByTestId('table-copy-markdown').boundingBox()
  const scrolled = await page.evaluate(() => {
    const w = document.querySelector('[data-capture-root] .overflow-x-auto')
    if (!w) return null
    w.scrollLeft = 400
    return w.scrollLeft
  })
  check('04 wrapper scrolled', scrolled != null && scrolled > 0, String(scrolled))
  await page.waitForTimeout(150)
  const after = await page.getByTestId('table-copy-markdown').boundingBox()
  check('04 copy row stayed put', before && after && Math.abs(before.x - after.x) < 1, JSON.stringify([before?.x, after?.x]))
  await root(page).screenshot({ path: `${OUT}/04-wide-scrolled.png` })
  await page.close()
}

// 05 -- the clipboard refuses: the inline notice, dismissable, and no tick.
{
  const page = await open('default', 'dark')
  // Both clipboard paths must refuse: the async API AND the execCommand
  // fallback copyToClipboard() drops to when the API throws.
  await page.evaluate(() => {
    navigator.clipboard.writeText = async () => { throw new Error('denied') }
    document.execCommand = () => false
  })
  await hoverTable(page)
  await page.getByTestId('table-copy-markdown').click()
  await page.waitForTimeout(150)
  const l = await labels(page)
  const notice = await page.getByText('Copy failed').count()
  check('05 failed notice shown, no tick', notice === 1 && l.md === 'Copy table as Markdown', JSON.stringify([notice, l.md]))
  await root(page).screenshot({ path: `${OUT}/05-copy-failed.png` })
  const dismiss = page.locator('[data-testid="markdown-table"] button[aria-label]').filter({ hasNot: page.locator('[data-testid^="table-copy-"]') }).last()
  await dismiss.click()
  await page.waitForTimeout(100)
  check('05 notice dismisses', (await page.getByText('Copy failed').count()) === 0)
  await page.close()
}

await browser.close()
if (failed) { console.log('FAILED'); process.exit(1) }
console.log(`wrote 6 frames to ${OUT}`)
