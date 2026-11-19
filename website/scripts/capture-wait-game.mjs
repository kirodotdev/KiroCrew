/**
 * Screenshots and a short recording of the laundry game on the dashboard's wait
 * surfaces (PR evidence). Drives the isolated entry `capture/wait-game.html`.
 *
 * Each scene asserts its marker and the script exits nonzero if one is missing,
 * so it cannot emit a screenshot of the wrong state. The game is started with
 * Space, then played by keyboard: two items folded and stacked, which is enough
 * for the first ghost (it comes on the first or second stack).
 *
 * Usage: node scripts/capture-wait-game.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6807'
const OUT = process.argv[3] || '../temp-screenshots/wait-game'
mkdirSync(OUT, { recursive: true })

async function start(page, scene, theme) {
  await page.goto(`${BASE}/capture/wait-game.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
  const status = page.locator(scene === 'update' ? '[data-testid="update-reconnecting"]' : '[data-testid="host-offline"]')
  await status.waitFor({ state: 'visible', timeout: 15_000 })
  if (await page.locator('[data-testid="wait-game"]').count()) throw new Error('game shown before the delay or Space')
  await page.keyboard.press('Space')
  await page.locator('[data-testid="wait-game"]').waitFor({ state: 'visible', timeout: 5_000 })
  await page.waitForTimeout(1200) // the basket's drop-in animation settles; evidence only, not a test barrier
}

// Fold one basket item and put it on the pile, by keyboard.
async function foldAndStack(page) {
  const item = page.locator('[data-testid="wait-game"] .laundry-keys button').first()
  await item.focus()
  await page.keyboard.press('Enter') // basket -> table
  await page.waitForTimeout(400)
  const onTable = page.locator('[data-testid="wait-game"] .laundry-keys button[aria-label^="Item on the table"]').first()
  await onTable.focus()
  for (let i = 0; i < 3; i++) { await page.keyboard.press('Enter'); await page.waitForTimeout(560) }
  const folded = page.locator('[data-testid="wait-game"] .laundry-keys button[aria-label^="Folded item"]').first()
  await folded.focus()
  await page.keyboard.press('Space')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('Space')
  await page.waitForTimeout(500)
}

const b = await chromium.launch()
for (const [vw, vh, tag] of [[1512, 862, 'desktop'], [320, 640, 'narrow']]) {
  for (const theme of ['dark', 'light']) {
    const page = await (await b.newContext({ viewport: { width: vw, height: vh }, deviceScaleFactor: 2 })).newPage()
    await start(page, 'host', theme)
    await page.screenshot({ path: `${OUT}/host-${tag}-${theme}.png` })
    console.log(`captured host-${tag}-${theme}.png`)
    await page.close()
  }
}
for (const [vw, vh, tag] of [[1000, 720, 'desktop'], [320, 640, 'narrow']]) {
  const page = await (await b.newContext({ viewport: { width: vw, height: vh }, deviceScaleFactor: 2 })).newPage()
  await start(page, 'update', 'dark')
  await page.screenshot({ path: `${OUT}/update-${tag}-dark.png` })
  console.log(`captured update-${tag}-dark.png`)
  await page.close()
}
{
  // A short landscape window: the cover scrolls instead of clipping its title.
  const page = await (await b.newContext({ viewport: { width: 740, height: 360 }, deviceScaleFactor: 2 })).newPage()
  await start(page, 'host', 'dark')
  await page.screenshot({ path: `${OUT}/host-short-dark.png` })
  console.log('captured host-short-dark.png')
  await page.close()
}
{
  const ctx = await b.newContext({ viewport: { width: 1200, height: 760 }, deviceScaleFactor: 1, recordVideo: { dir: OUT, size: { width: 1200, height: 760 } } })
  const page = await ctx.newPage()
  await start(page, 'host', 'dark')
  await page.screenshot({ path: `${OUT}/play-0-start.png` })
  await foldAndStack(page)
  await page.screenshot({ path: `${OUT}/play-1-stacked.png` })
  await foldAndStack(page)
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/play-2-ghost.png` })
  await page.waitForTimeout(3500)
  await page.screenshot({ path: `${OUT}/play-3-after.png` })
  console.log('captured play-*.png and a recording')
  await ctx.close()
}
await b.close()
