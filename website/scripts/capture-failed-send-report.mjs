/**
 * Screenshots of the two failure surfaces fixed in #4198
 * (website/capture/failed-send-report.tsx).
 *
 *  - feature: the feature-request transcript with the error row a refused
 *    send now appends (rendered through the real row registry).
 *  - scene: the scene popover driven END-TO-END — this script clicks the
 *    agent, types a message, sends it into a stubbed 409, and captures the
 *    red Retry state with the payload handed back to the draft. The send
 *    RESOLVES (no rejection), so reaching 'failed' here is the receipt
 *    check working.
 *
 * Every scene asserts its own marker and the script EXITS NONZERO if one is
 * missing, so it can never quietly emit a screenshot of the wrong state.
 *
 * Usage: node scripts/capture-failed-send-report.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6807'
const OUT = process.argv[3] || '../temp-screenshots/failed-send-report'
mkdirSync(OUT, { recursive: true })

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    // ── feature-request transcript ──────────────────────────────────────────
    {
      const ctx = await browser.newContext({ viewport: { width: 820, height: 400 }, deviceScaleFactor: 2 })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`${BASE}/capture/failed-send-report.html?site=feature&theme=${theme}`, { waitUntil: 'networkidle' })
      try {
        await page.waitForSelector('text=Send failed: slot agent mismatch', { timeout: 10000 })
        await page.waitForSelector("text=I'd like to request a feature", { timeout: 10000 })
        const root = page.locator('[data-capture-root]')
        await root.screenshot({ path: `${OUT}/feature-request-error-row-${theme}.png` })
        console.log(`ok  feature ${theme}`)
      } catch (e) {
        failed += 1
        console.error(`FAIL feature ${theme}: ${e.message}`, errors)
      }
      await ctx.close()
    }
    // ── scene popover, real send path ───────────────────────────────────────
    {
      const ctx = await browser.newContext({ viewport: { width: 820, height: 500 }, deviceScaleFactor: 2 })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`${BASE}/capture/failed-send-report.html?site=scene&theme=${theme}`, { waitUntil: 'networkidle' })
      try {
        const canvas = page.locator('[data-testid="scene"]')
        await canvas.click({ position: { x: 150, y: 150 } })
        const box = page.getByRole('textbox')
        await box.waitFor({ timeout: 10000 })
        await box.fill('ship the receipt check')
        await page.getByRole('button', { name: 'Send message' }).click()
        // The refused send RESOLVES with {ok:false}; the retry state proves
        // the receipt check caught it. The draft must still hold the payload.
        await page.getByRole('button', { name: 'Retry sending message' }).waitFor({ timeout: 10000 })
        await page.waitForSelector('text=Send failed: slot agent mismatch', { timeout: 10000 })
        const value = await box.inputValue()
        if (value !== 'ship the receipt check') throw new Error(`draft not handed back: ${JSON.stringify(value)}`)
        const root = page.locator('[data-capture-root]')
        await root.screenshot({ path: `${OUT}/scene-popover-retry-${theme}.png` })
        console.log(`ok  scene ${theme}`)
      } catch (e) {
        failed += 1
        console.error(`FAIL scene ${theme}: ${e.message}`, errors)
      }
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) { console.error(`${failed} scene(s) failed`); process.exit(1) }
}

run()
