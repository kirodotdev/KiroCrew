/**
 * Screenshot runner for capture/resource-failure-notices.html (#9186).
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort
 *   node scripts/capture-resource-failure-notices.mjs http://127.0.0.1:6832 <outdir>
 *
 * Asserts both settled notice texts are on screen before photographing, so a
 * frame cannot capture an empty or wrong tree.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/resource-failure-notices'
mkdirSync(OUT, { recursive: true })

const cp = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.manual.json', import.meta.url), 'utf-8')).pages.chatPage
const expected = [cp.source_hosts_failed_reason, cp.artifact_reference_failed_reason]
  .map(s => s.split('{{reason}}')[0].trim())

const browser = await chromium.launch()
let failed = 0
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 780, height: 600 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/resource-failure-notices.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.addStyleTag({
      content: '*, *::before, *::after { animation-duration: 0s !important;'
        + ' animation-delay: 0s !important; transition-duration: 0s !important;'
        + ' transition-delay: 0s !important; }',
    })
    for (const text of expected) await page.getByText(text).first().waitFor({ timeout: 10000 })
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/after-${theme}.png` })
    console.log(`after-${theme}: OK`)
  } catch (e) {
    console.error(`after-${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}
await browser.close()
process.exit(failed ? 1 : 0)
