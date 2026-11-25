/**
 * Screenshots of the switches that keep Bot sessions in the person's Sessions list.
 *
 * Drives the isolated capture entry (website/capture/bot-sessions-in-list.html):
 * a Bot's Sessions list with the all-Bots switch and this Bot's own, with the
 * all-Bots switch off and on, in a 380px card and at phone width. Each scene asserts the switches' states before writing its file, so a run
 * never emits a frame of the wrong state.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort   # in another shell
 *   node scripts/capture-bot-sessions-in-list.mjs http://127.0.0.1:6825 ../temp-screenshots/bot-sessions-in-list
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/bot-sessions-in-list'
mkdirSync(OUT, { recursive: true })

const ALL = 'Show all bots in my Sessions'
const THIS = 'Show Release Bot in my Sessions'

// [file, query, expected all-Bots state, expected this-Bot state (null: not shown), this-Bot locked]
const SCENES = [
  ['sessions-global-off', 'global=0&own=1', 'false', 'true', false],
  ['sessions-global-on', 'global=1&own=0', 'true', 'true', true],
  ['phone-sessions-global-off', 'global=0&own=1&phone=1', 'false', 'true', false],
  ['phone-sessions-global-on', 'global=1&own=0&phone=1', 'true', 'true', true],
]

const browser = await chromium.launch()
const card = await browser.newPage({ viewport: { width: 440, height: 640 }, deviceScaleFactor: 2 })
const phonePage = await browser.newPage({ viewport: { width: 360, height: 640 }, deviceScaleFactor: 3 })
let failed = false
for (const [name, query, allState, thisState, locked] of SCENES) {
  const page = query.includes('phone=1') ? phonePage : card
  await page.goto(`${BASE}/capture/bot-sessions-in-list.html?${query}`)
  await page.addStyleTag({ content: '*, *::before, *::after { transition-duration: 0s !important; animation-duration: 0s !important; }' })
  await page.waitForSelector('[data-capture-root]')
  const all = page.getByRole('switch', { name: ALL })
  await all.waitFor()
  await page.waitForFunction(n => {
    const el = [...document.querySelectorAll('[role="switch"]')].find(e => e.getAttribute('aria-label') === n)
    return el && !el.hasAttribute('aria-disabled')
  }, ALL)
  const got = [await all.getAttribute('aria-checked')]
  const own = page.getByRole('switch', { name: THIS })
  if (thisState === null) {
    if (await own.count()) { console.log(`${name}: this-Bot switch shown`); failed = true; continue }
  } else {
    got.push(await own.getAttribute('aria-checked'), String((await own.getAttribute('aria-disabled')) === 'true'))
  }
  const want = thisState === null ? [allState] : [allState, thisState, String(locked)]
  console.log(`${name}: ${got.join(' ')}`)
  if (got.join() !== want.join()) { failed = true; continue }
  const cut = await page.evaluate(() => [...document.querySelectorAll('[data-testid^="bot-sessions-in-list-"] span.font-semibold')]
    .filter(el => el.scrollWidth > el.clientWidth).length)
  if (cut) { console.log(`${name}: ${cut} label(s) cut off`); failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}.png` })
}
await browser.close()
if (failed) {
  console.error('a scene did not render the expected switch states')
  process.exit(1)
}
