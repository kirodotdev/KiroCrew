/**
 * Screenshots of the folder tab's listing row menu (#43258).
 *
 * Drives the ISOLATED capture entry (website/capture/folder-panel-row-menu.html),
 * which mounts the real FolderPanel on a `/tmp/...` listing against the real
 * stylesheet and theme tokens. Frames per theme: the listing at rest, a row
 * hovered with its "..." button revealed, the menu open from that button
 * (Add to chat and Download beside the row), and the menu open from a
 * right-click on another row. The script fails if the menu does not render,
 * lacks either built-in row, or lands outside the viewport.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6843 --strictPort   # in another shell
 *   node scripts/capture-folder-panel-row-menu.mjs http://127.0.0.1:6843 ../temp-screenshots/folder-panel-row-menu
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6843'
const OUT = process.argv[3] || '../temp-screenshots/folder-panel-row-menu'
mkdirSync(OUT, { recursive: true })

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 900, height: 520 }, deviceScaleFactor: 2, colorScheme: theme })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
    await page.goto(`${BASE}/capture/folder-panel-row-menu.html?theme=${theme}`, { waitUntil: 'networkidle' })
    // A row's accessible name folds in its "..." button's label, so address it by
    // the path it carries as its title instead of by name.
    const row = name => page.locator(`[role="button"][title="/tmp/pr42850-evidence/${name}"]`)
    await row('notes.md').waitFor({ timeout: 20000 })
    await page.screenshot({ path: `${OUT}/01-listing-${theme}.png` })

    // Hover: the row's "..." button is revealed (opacity 0 -> 1 on group-hover).
    await row('01-before-dark.png').hover()
    const kebab = row('01-before-dark.png').getByTestId('folder-row-menu')
    await kebab.waitFor()
    await page.screenshot({ path: `${OUT}/02-row-hover-${theme}.png` })

    // Open from the button: right edges aligned under the button.
    await kebab.click()
    const menu = page.getByRole('menu')
    await menu.waitFor({ timeout: 5000 })
    for (const label of ['Add to chat', 'Download']) {
      if (!(await menu.getByRole('menuitem', { name: label, exact: true }).count())) {
        console.error(`MISSING menu row ${label} in ${theme}`); failed++
      }
    }
    const box = await menu.boundingBox()
    const vp = page.viewportSize()
    if (!box || box.x < 0 || box.y < 0 || box.x + box.width > vp.width || box.y + box.height > vp.height) {
      console.error(`MENU OUTSIDE VIEWPORT in ${theme}:`, box); failed++
    }
    await page.screenshot({ path: `${OUT}/03-menu-from-button-${theme}.png` })
    await page.keyboard.press('Escape')
    await menu.waitFor({ state: 'detached' })

    // Open from a right-click on another row: anchored at the pointer.
    await row('notes.md').click({ button: 'right' })
    await menu.waitFor({ timeout: 5000 })
    await page.screenshot({ path: `${OUT}/04-menu-from-right-click-${theme}.png` })
    await page.keyboard.press('Escape')
    await menu.waitFor({ state: 'detached' })

    if (errors.length) { console.error(`PAGE ERRORS ${theme}:`, errors.slice(0, 3)); failed++ }
    else console.log(`ok ${theme}: 4 frames`)
    await ctx.close()
  }
  await browser.close()
  if (failed) { console.error(`${failed} check(s) failed`); process.exit(1) }
}

run().catch(e => { console.error(e); process.exit(1) })
