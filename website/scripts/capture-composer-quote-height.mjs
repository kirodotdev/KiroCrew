/** Real-browser evidence for #35680: the Lexical composer's height with four
 * quotes staged. Asserts the editor stays at or under the 140px autosize cap
 * (exits non-zero otherwise) and screenshots each theme.
 *
 * Usage: node scripts/capture-composer-quote-height.mjs <baseUrl> <outDir> <label>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2]
const OUT = resolve(process.argv[3] || '../temp-screenshots/composer-quote-height')
const LABEL = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 720 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/capture/composer-quote-height.html?theme=${theme}`, { waitUntil: 'networkidle' })
  const editor = page.locator('[data-lexical-composer]')
  await editor.waitFor({ timeout: 20000 })
  await page.waitForTimeout(600)
  const m = await editor.evaluate(el => ({ h: el.getBoundingClientRect().height, scroll: el.scrollHeight, vh: window.innerHeight }))
  const transcript = await page.locator('[data-testid="transcript"]').evaluate(el => el.getBoundingClientRect().height)
  console.log(theme, LABEL, JSON.stringify({ ...m, transcript }))
  if (LABEL === 'after' && !(m.h <= 141 && m.scroll > m.h)) failures++
  await page.screenshot({ path: resolve(OUT, `${LABEL}-${theme}.png`) })
  await ctx.close()
}
await browser.close()
process.exit(failures ? 1 : 0)
