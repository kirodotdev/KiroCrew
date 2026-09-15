/**
 * Screenshots of the side panel running the REAL native composer.
 *
 * Each scene asserts the shipped composer actually mounted (the
 * `data-composer-input` textarea inside the side panel wrapper) before
 * writing the file, so a frame can never show the old bare-textarea fork.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-sidechat-composer.mjs http://127.0.0.1:6841 ../temp-screenshots/sidechat-composer
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/sidechat-composer'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'idle-dark', scene: 'idle', theme: 'dark', expectSplit: false },
  { name: 'busy-steer-dark', scene: 'busy', theme: 'dark', expectSplit: true },
  { name: 'idle-light', scene: 'idle', theme: 'light', expectSplit: false },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 480, height: 620 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/sidechat-composer.html?scene=${s.scene}&theme=${s.theme}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  const composer = page.locator('[data-side-chat-input] textarea[data-composer-input]')
  await composer.waitFor({ timeout: 5000 })
  // The state under test in the busy scene: the split Steer/Queue button is
  // offered mid-turn. Type first — the split renders once there is a payload.
  if (s.expectSplit) {
    await composer.fill('actually check the folder filter first')
    await page.getByTestId('busy-send-button').waitFor({ timeout: 3000 })
  }
  console.log(`${s.name}: real composer mounted${s.expectSplit ? ' + split steer button' : ''}`)
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) process.exit(1)
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
