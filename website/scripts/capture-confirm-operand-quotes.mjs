/**
 * Screenshots for the destructive-confirm operand quoting (#4657).
 *
 * Drives website/capture/confirm-operand-quotes.html. Two scenes:
 *   - mochi-reset: opens the REAL ChatPanel context menu, clicks the real
 *     "Reset Everything" item, and photographs the shipped reset dialog.
 *   - papyrus-delete: photographs the real delete_file_confirm i18n string in
 *     labelled harness chrome (the product uses native window.confirm, which
 *     has no DOM).
 *
 * Each scene ASSERTS THE RENDERED TEXT before writing the file, so a frame
 * cannot silently photograph the wrong state:
 *   default (after) mode expects the QUOTED operand — Reset “Everything”? /
 *   Delete “Everything”? — and fails on the bare string.
 *   --before mode inverts the assertion (run it against base code, e.g. with
 *   the locale changes stashed) and expects the BARE operand.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-confirm-operand-quotes.mjs http://127.0.0.1:6823 ../temp-screenshots/confirm-operand-quotes [prefix] [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/confirm-operand-quotes'
const PREFIX = (process.argv[4] && !process.argv[4].startsWith('--')) ? process.argv[4] : ''
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const Q_OPEN = '\u201c'
const Q_CLOSE = '\u201d'
const QUOTED_RESET = `Reset ${Q_OPEN}Everything${Q_CLOSE}?`
const BARE_RESET = 'Reset Everything?'
const QUOTED_DELETE = `Delete ${Q_OPEN}Everything${Q_CLOSE}?`
const BARE_DELETE = 'Delete Everything?'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 460, height: 520 }, deviceScaleFactor: 2 })

let failed = false

// --- Scene 1: the real Mochi reset dialog ---
{
  await page.goto(`${BASE}/capture/confirm-operand-quotes.html?scene=mochi-reset`)
  await page.waitForSelector('[data-capture-root]')
  // The reset item lives in the real context menu; the menu label itself
  // interpolates the pet name ("Reset Everything"), which doubles as proof the
  // stubbed settings read actually drove resolvePetName.
  const root = page.locator('[data-capture-root]')
  await root.click({ button: 'right', position: { x: 200, y: 240 } })
  const item = page.getByRole('menuitem', { name: 'Reset Everything' })
  await item.waitFor({ timeout: 10000 })
  await item.click()
  const expected = BEFORE ? BARE_RESET : QUOTED_RESET
  const wrong = BEFORE ? QUOTED_RESET : BARE_RESET
  const title = page.getByText(expected, { exact: true })
  try {
    await title.waitFor({ timeout: 5000 })
    console.log(`mochi-reset: title=${JSON.stringify(expected)} OK`)
    await page.screenshot({ path: `${OUT}/${PREFIX}mochi-reset-confirm.png` })
  } catch {
    const alt = await page.getByText(wrong, { exact: true }).count()
    console.error(`mochi-reset: expected ${JSON.stringify(expected)} not found` + (alt ? ` (found ${JSON.stringify(wrong)} — wrong mode?)` : ''))
    failed = true
  }
}

// --- Scene 2: the real Papyrus delete_file_confirm string ---
{
  await page.goto(`${BASE}/capture/confirm-operand-quotes.html?scene=papyrus-delete`)
  await page.waitForSelector('[data-confirm-message]')
  const text = (await page.locator('[data-confirm-message]').innerText()).trim()
  const expected = BEFORE ? BARE_DELETE : QUOTED_DELETE
  if (text === expected) {
    console.log(`papyrus-delete: message=${JSON.stringify(text)} OK`)
    await page.screenshot({ path: `${OUT}/${PREFIX}papyrus-delete-confirm.png` })
  } else {
    console.error(`papyrus-delete: message=${JSON.stringify(text)} expected ${JSON.stringify(expected)}`)
    failed = true
  }
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected text — no misleading frame written')
  process.exit(1)
}
console.log(`wrote screenshots to ${OUT}`)
