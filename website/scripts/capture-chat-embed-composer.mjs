/**
 * Screenshots for ChatEmbed's composer (chat-core P3-f).
 *
 * Drives website/capture/chat-embed-composer.html: the REAL ChatEmbed under the
 * REAL AppApiProvider, with no Redux Provider and no router, against fixed
 * fake slot data. Frames per theme:
 *
 *  - idle:    a draft typed, nothing running: the Send control.
 *  - queue:   a draft typed while a turn runs: Send reads "Queue message".
 *  - refused: the send is refused (409): the error row shows and the text is
 *             back in the composer.
 *
 * Asserted, not assumed: the button's accessible name per scene, and that the
 * refused scene hands the text back.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-chat-embed-composer.mjs http://127.0.0.1:6811 ../temp-screenshots/chat-embed-composer
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/chat-embed-composer'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 600, height: 470 } })
const pageErrors = []
page.on('pageerror', e => pageErrors.push(String(e)))
let failures = 0
const DRAFT = 'Also cap the per-key burst at 20.'

const scenes = [
  { name: 'idle', query: '', button: 'Send message' },
  { name: 'queue', query: '&running=1', button: 'Queue message' },
  { name: 'refused', query: '&refuse=1', button: 'Send message', send: true },
]

for (const theme of ['dark', 'light']) {
  for (const scene of scenes) {
    await page.goto(`${BASE}/capture/chat-embed-composer.html?theme=${theme}${scene.query}`, { waitUntil: 'networkidle' })
    const input = page.getByLabel('Chat message', { exact: true })
    await input.waitFor()
    await input.fill(DRAFT)
    const btn = page.getByRole('button', { name: scene.button, exact: true })
    if (await btn.count() !== 1) { console.error(`FAIL: ${theme}/${scene.name} expected one "${scene.button}" button`); failures++ }
    if (scene.send) {
      await btn.click()
      await page.waitForFunction(() => /Couldn't send this message/.test(document.querySelector('[data-capture-root]')?.textContent || ''), null, { timeout: 15000 }).catch(() => {})
      const composer = await input.inputValue()
      if (composer !== DRAFT) { console.error(`FAIL: ${theme}/${scene.name} composer should hold the sent text back (got "${composer}")`); failures++ }
    }
    await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished)))
    await page.screenshot({ path: `${OUT}/${scene.name}-${theme}.png` })
    console.log(`${theme}/${scene.name}: ok`)
  }
}

await browser.close()
if (pageErrors.length) { console.error('page errors:', pageErrors); failures++ }
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
