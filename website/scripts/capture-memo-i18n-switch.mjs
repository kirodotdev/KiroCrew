/**
 * Before/after evidence for the memo()+i18nT() language-switch fix (#5225).
 *
 * Drives the isolated capture entry (website/capture/memo-i18n-switch.html),
 * which mounts the REAL PastedChip (fixed: memo + useLanguageGeneration) next
 * to a pre-fix replica (memo, no subscription) under the real LanguageProvider.
 *
 * Assertions, not assumptions:
 *  - before the switch both rows render English;
 *  - after `setLanguage('zh-CN')` the fixed chip renders Chinese while the
 *    pre-fix replica still renders English — proving both that the hook works
 *    and that the defect it closes is real (a replica that also switched would
 *    make the comparison meaningless).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6809 --strictPort   # in another shell
 *   node scripts/capture-memo-i18n-switch.mjs http://127.0.0.1:6809 ../temp-screenshots/memo-i18n-switch
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6809'
const OUT = process.argv[3] || '../temp-screenshots/memo-i18n-switch'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 760, height: 260 } })
let failures = 0

await page.goto(`${BASE}/capture/memo-i18n-switch.html`, { waitUntil: 'networkidle' })
await page.waitForSelector('[data-cap="fixed"]')

const hasCJK = (s) => /[\u4e00-\u9fff]/.test(s)

const before = await page.evaluate(() => window.__texts())
console.log('before switch:', JSON.stringify(before))
if (hasCJK(before.fixed) || hasCJK(before.bare)) {
  console.error('FAIL: expected both rows to start in English')
  failures++
}
await page.screenshot({ path: `${OUT}/before-switch-en.png`, fullPage: true })

await page.evaluate(() => window.__switch())
await page.waitForFunction(() => /[\u4e00-\u9fff]/.test(window.__texts().fixed), { timeout: 5000 })

const after = await page.evaluate(() => window.__texts())
console.log('after switch:', JSON.stringify(after))
if (!hasCJK(after.fixed)) {
  console.error('FAIL: fixed PastedChip did not switch to Chinese')
  failures++
}
if (hasCJK(after.bare)) {
  console.error('FAIL: pre-fix replica switched too — the reproduction is gone and the comparison proves nothing')
  failures++
}
await page.screenshot({ path: `${OUT}/after-switch-zh-fixed-vs-stale.png`, fullPage: true })

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
