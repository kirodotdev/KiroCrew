/**
 * Screenshots of Settings -> Advanced -> All settings rows that carry a risk line.
 *
 * Drives the ISOLATED capture entry (website/capture/all-settings-risk.html), which
 * mounts the REAL AllSettingsList with the gateway's answers replaced by fixtures.
 * Each frame checks that every risky row drew its line before it is written.
 * Component render, not the live page.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6893 --strictPort   # in another shell
 *   node scripts/capture-all-settings-risk.mjs http://127.0.0.1:6893 <out-dir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import path from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6893'
const OUT = path.resolve(process.argv[3] || '../temp-screenshots/all-settings-risk')
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : undefined,
)

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 900 }, deviceScaleFactor: 2, colorScheme: theme, locale: 'en-US' })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto(`${BASE}/capture/all-settings-risk.html?theme=${theme}`, { waitUntil: 'load', timeout: 150000 })
  await page.locator('[data-risk]').first().waitFor({ timeout: 60000 })
  const n = await page.locator('[data-risk]').count()
  if (n !== 6) throw new Error(`${theme}: expected 6 risk lines, got ${n}`)
  if ((await page.locator('textarea').count()) < 3) throw new Error(`${theme}: the multi-line box is missing`)
  const file = path.join(OUT, `all-settings-risk-${theme}.png`)
  await page.screenshot({ path: file, fullPage: true })
  if (errors.length) throw new Error(`${theme}: page errors: ${errors.join('; ')}`)
  console.log(file)
  await ctx.close()
}
await browser.close()
