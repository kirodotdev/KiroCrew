/** Real-browser evidence for #16617: a crewmate's messages carry no author line.
 *
 * Drives website/capture/crewmate-run.html (real ChatMessageList + crewmate
 * renderers) per theme and asserts, on the AFTER tree, that no
 * `crewmate-author` row and no avatar gutter precede any bubble. Pass
 * --expect-author to assert the OPPOSITE on a pre-#16617 tree, which is how the
 * "before" frame of the PR body is taken from the same script.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6882 --strictPort            # in website/
 *   node scripts/capture-crewmate-run.mjs http://127.0.0.1:6882 ../temp-screenshots/crewmate-run after
 *   node scripts/capture-crewmate-run.mjs http://127.0.0.1:6881 ../temp-screenshots/crewmate-run before --expect-author
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6882'
const OUT = resolve(process.argv[3] || '../temp-screenshots/crewmate-run')
const TAG = process.argv[4] || 'after'
const expectAuthor = process.argv.includes('--expect-author')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 640 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/crewmate-run.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-testid="crewmate-message"]'); await page.waitForTimeout(500)
  const rows = page.locator('[data-testid="crewmate-message"]')
  check(`[${theme}/${TAG}] three crewmate messages drawn`, await rows.count() === 3)
  const authors = await page.locator('[data-testid="crewmate-author"]').count()
  const gutters = await rows.evaluateAll(els => els.filter(e => Array.from(e.querySelectorAll('*')).some(c => /pl-\[38px\]/.test(c.className))).length)
  if (expectAuthor) {
    check(`[${theme}/${TAG}] author line on both run openers (pre-#16617)`, authors === 2)
    check(`[${theme}/${TAG}] avatar gutter under every bubble (pre-#16617)`, gutters === 3)
  } else {
    check(`[${theme}/${TAG}] no author line on any message`, authors === 0)
    check(`[${theme}/${TAG}] no avatar gutter under any bubble`, gutters === 0)
  }
  check(`[${theme}/${TAG}] no page errors`, errors.length === 0)
  await page.screenshot({ path: resolve(OUT, `${theme}-${TAG}.png`) })
  await ctx.close()
}
await browser.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
