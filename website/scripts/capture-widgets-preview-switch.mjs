/**
 * Review evidence for the `widgets` feature preview: the "Phone widgets" switch under
 * Settings > Developer > Feature previews, off (the default) and on.
 *
 * The built SPA is served on 127.0.0.1 and every API call is stubbed
 * (`lib/stub-dashboard-api.mjs`), so no gateway or token is involved.
 *
 * Usage (after a build): node scripts/capture-widgets-preview-switch.mjs <out-dir> [dist-dir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
const DIST = process.argv[3] || DEFAULT_DIST
if (!OUT) throw new Error('usage: capture-widgets-preview-switch.mjs <out-dir> [dist-dir]')
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

async function scene(file, { on, theme = 'dark' }) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, localStorageEntries: on ? { 'mc-preview-widgets': '1' } : {} })
  await page.goto(base + '/settings/developer/feature-previews?highlight=developer.phone-widgets')
  const row = page.getByRole('switch', { name: /^phone widgets$/i })
  await row.waitFor({ timeout: 20000 })
  await row.scrollIntoViewIfNeeded()
  await page.mouse.move(1275, 5)
  await page.waitForTimeout(1500)
  await page.screenshot({ path: `${OUT}/${file}` })
  await context.close()
}

await scene('widgets-switch-off-dark.png', { on: false })
await scene('widgets-switch-on-dark.png', { on: true })
await scene('widgets-switch-off-light.png', { on: false, theme: 'light' })

await browser.close()
srv.close()
console.log('done')
