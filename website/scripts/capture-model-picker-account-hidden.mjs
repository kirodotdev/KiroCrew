/**
 * Screenshot runner for capture/model-picker-account-hidden.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 5199 --strictPort
 *   node scripts/capture-model-picker-account-hidden.mjs http://127.0.0.1:5199 <outdir>
 *
 * Three frames per theme, element-scoped to the capture root:
 *   picker        the chat picker without the account-hidden model (and without
 *                 the Crew-hidden one), read back: `claude-haiku-4.5` and `glm-5`
 *                 absent, the four others present;
 *   picker-active the same list while the account-hidden model is the session's
 *                 current one, so it stays listed;
 *   error         the terminal card for a model the account is not served, read
 *                 back: the picker and Settings actions present, no Continue.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:5199'
const OUT = process.argv[3] || '../temp-screenshots/model-picker-account-hidden'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

const rowCount = (page, id) => page.locator('[data-capture-root]').getByText(id, { exact: true }).count()

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 860, height: 660 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/model-picker-account-hidden.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.locator('[data-capture-root]').getByText('gemini-2.5-pro', { exact: true }).waitFor({ timeout: 10000 })
    const present = await Promise.all(['auto', 'claude-opus-4.7', 'claude-sonnet-4.6', 'gemini-2.5-pro'].map(id => rowCount(page, id)))
    const haiku = await rowCount(page, 'claude-haiku-4.5')
    const glm = await rowCount(page, 'glm-5')
    if (present.some(n => n === 0) || haiku !== 0 || glm !== 0) {
      throw new Error(`picker rows: present=${present} haiku=${haiku} glm=${glm}`)
    }
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}-picker.png` })

    await page.goto(`${BASE}/capture/model-picker-account-hidden.html?theme=${theme}&active=claude-haiku-4.5`, { waitUntil: 'networkidle' })
    await page.locator('[data-capture-root]').getByText('claude-haiku-4.5', { exact: true }).waitFor({ timeout: 10000 })
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}-picker-active.png` })

    await page.goto(`${BASE}/capture/model-picker-account-hidden.html?theme=${theme}&scene=error`, { waitUntil: 'networkidle' })
    const card = page.locator('[data-episode="unserved"] [data-testid="error-card"]')
    await card.waitFor({ timeout: 10000 })
    if (await page.locator('[data-episode="unserved"] [data-testid="error-card-continue"]').count()) {
      throw new Error('the unserved card offers Continue')
    }
    const text = (await card.innerText()).replace(/\s+/g, ' ')
    if (!text.includes("'claude-nonexistent-9' is not available on your account") || !text.includes('Retrying will not help')) {
      throw new Error(`unexpected card text: ${text.slice(0, 160)}`)
    }
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}-error.png` })
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    console.log(`${theme}: OK (picker, picker-active, error)`)
  } catch (err) {
    failed += 1
    console.error(`${theme}: FAIL ${err instanceof Error ? err.message : err}`)
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
