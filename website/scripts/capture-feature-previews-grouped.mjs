/**
 * Review evidence for #34855: full-page shots of Settings > Developer >
 * Feature Previews, grouped into boxed cards.
 *
 * Usage (after a build): node scripts/capture-feature-previews-grouped.mjs <out-dir> [dist-dir] [prefix]
 * Pass another checkout's `website/dist` and a prefix to shoot the "before" page.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
const DIST = process.argv[3] || DEFAULT_DIST
const PREFIX = process.argv[4] || 'after'
if (!OUT) throw new Error('usage: capture-feature-previews-grouped.mjs <out-dir> [dist-dir] [prefix]')
mkdirSync(OUT, { recursive: true })

// A few switches on, so the shot shows both states of the row.
const ON = { 'mc-preview-crew-mode': '1', 'mc-preview-explore': '1', 'mc-preview-mate-sessions': '1' }

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

async function scene(file, { width = 1280, height = 900, theme = 'dark' } = {}) {
  const phone = width < 600
  const context = await browser.newContext({ viewport: { width, height }, hasTouch: phone, isMobile: phone })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, localStorageEntries: ON })
  await page.goto(base + '/settings/developer/feature-previews')
  await page.locator('[data-setting-key="feature-previews-section"]').waitFor({ timeout: 20000 })
  await page.waitForTimeout(2500)
  // Grow the viewport by every inner scroller's overflow so one shot holds the whole page.
  const extra = await page.evaluate(() => Math.max(0, ...[...document.querySelectorAll('div, main')]
    .filter(d => ['auto', 'scroll'].includes(getComputedStyle(d).overflowY))
    .map(d => d.scrollHeight - d.clientHeight)))
  await page.setViewportSize({ width, height: height + extra })
  await page.mouse.move(width - 5, 5)
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/${PREFIX}-${file}` })
  // The Engines and Workspaces group on its own, where it exists (after only).
  const group = page.locator('div:has(> div > h4:text-is("Engines and Workspaces"))').last()
  if (await group.count()) await group.screenshot({ path: `${OUT}/${PREFIX}-engines-${file}` })
  await context.close()
}

await scene('1280-dark.png')
await scene('1280-light.png', { theme: 'light' })
await scene('1920-dark.png', { width: 1920, height: 1080 })
await scene('phone-390-dark.png', { width: 390, height: 844 })

await browser.close()
srv.close()
console.log('done')
