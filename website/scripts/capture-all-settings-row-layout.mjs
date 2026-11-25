/**
 * Screenshots of Settings -> Advanced -> All settings rows that hold a long
 * current value (a link row, a read-only row, an edit row).
 *
 * Drives the ISOLATED capture entry (website/capture/all-settings-row-layout.html),
 * which mounts the REAL AllSettingsList with the gateway's answers replaced by
 * fixtures. Each frame prints the width of every row's label column, so a
 * squeezed label shows up as a number, not only in the picture.
 * Component render, not the live page.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6894 --strictPort   # in another shell
 *   node scripts/capture-all-settings-row-layout.mjs http://127.0.0.1:6894 <out-dir> [suffix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import path from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6894'
const OUT = path.resolve(process.argv[3] || '../temp-screenshots/all-settings-row-layout')
const SUFFIX = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : undefined,
)

const frames = [
  { name: 'dark', theme: 'dark', width: 1100 },
  { name: 'light', theme: 'light', width: 1100 },
  { name: 'narrow', theme: 'dark', width: 700 },
  { name: 'pane316', theme: 'dark', width: 1100, pane: 316 },
  { name: 'expanded', theme: 'dark', width: 1100, expand: true },
]
for (const f of frames) {
  const ctx = await browser.newContext({ viewport: { width: f.width, height: 900 }, deviceScaleFactor: 2, colorScheme: f.theme, locale: 'en-US' })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto(`${BASE}/capture/all-settings-row-layout.html?theme=${f.theme}${f.pane ? `&pane=${f.pane}` : ''}`, { waitUntil: 'load', timeout: 150000 })
  await page.locator('[data-setting-key="agent.apps_trusted"]').waitFor({ timeout: 60000 })
  if (f.expand) await page.locator('[data-setting-key="agent.apps_trusted"] button[aria-expanded="false"]').click()
  const overflow = await page.$$eval('[data-setting-key]', rows => rows.filter(r => r.scrollWidth > r.clientWidth + 1).map(r => r.getAttribute('data-setting-key')))
  if (overflow.length) console.log(`${f.name}: rows wider than the pane: ${overflow.join(' ')}`)
  const widths = await page.$$eval('[data-setting-key]', rows => rows.map(r => {
    const label = r.firstElementChild
    return `${r.getAttribute('data-setting-key')}=${Math.round(label.getBoundingClientRect().width)}px`
  }))
  console.log(`${f.name}: label widths ${widths.join(' ')}`)
  const file = path.join(OUT, `all-settings-row-layout-${f.name}-${SUFFIX}.png`)
  await page.screenshot({ path: file, fullPage: true })
  if (errors.length) throw new Error(`${f.name}: page errors: ${errors.join('; ')}`)
  console.log(file)
  await ctx.close()
}
await browser.close()
