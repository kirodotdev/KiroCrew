/**
 * Screenshots of a crewmate's goal card with its Heartbeat checkbox, from the
 * isolated capture entry website/capture/goal-heartbeat-card.html: checked (the
 * default), unchecked, and the card with no checkbox once the Heartbeat is on,
 * light and dark, plus the checked card in Chinese.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort   # in another shell
 *   node scripts/capture-goal-heartbeat-card.mjs http://127.0.0.1:6816 ../temp-screenshots/goal-heartbeat-card
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/goal-heartbeat-card'
mkdirSync(OUT, { recursive: true })

const SHOTS = [
  ['checked', 'dark', 'en'], ['checked', 'light', 'en'],
  ['unchecked', 'dark', 'en'], ['none', 'dark', 'en'],
  ['checked', 'dark', 'zh-CN'],
]

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
for (const [scene, theme, lang] of SHOTS) {
  const page = await browser.newPage({ viewport: { width: 600, height: 700 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.error(`  pageerror: ${e.message}`); failures++ })
  await page.goto(`${BASE}/capture/goal-heartbeat-card.html?scene=${scene}&theme=${theme}&lang=${lang}`, { waitUntil: 'networkidle' })
  await page.locator('[data-testid="change-card"]').waitFor()
  const boxes = page.locator('[data-testid="change-card-goal-heartbeat-box"]')
  const want = scene === 'none' ? 0 : 1
  if ((await boxes.count()) !== want) { console.error(`FAIL: ${scene} shows ${await boxes.count()} box(es), want ${want}`); failures++ }
  if (want && (await boxes.isChecked()) !== (scene === 'checked')) { console.error(`FAIL: ${scene} box checked state`); failures++ }
  const name = `${scene}-${theme}-${lang}`
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}.png` })
  console.log(name, JSON.stringify((await page.locator('[data-testid="change-card-goal-heartbeat"]').textContent().catch(() => '')) || ''))
  await page.close()
}
await browser.close()
console.log(`wrote ${OUT}`)
process.exit(failures ? 1 : 0)
