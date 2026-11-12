/** Real-browser evidence for #35598: a chat widget's toolbar shows each
 * button's name on hover, and a refused "Open in new tab" says so. Asserts
 * both (exits non-zero otherwise) and screenshots each theme.
 *
 * Usage: node scripts/capture-widget-toolbar-popout.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2]
const OUT = resolve(process.argv[3] || '../temp-screenshots/widget-toolbar-popout')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 900, height: 360 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/capture/widget-toolbar-popout.html?theme=${theme}`, { waitUntil: 'networkidle' })
  const popout = page.getByRole('button', { name: 'Open in new tab' })
  await popout.waitFor({ timeout: 20000 })
  await page.locator('iframe').first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(600)

  const title = await popout.getAttribute('title')
  await popout.hover()
  const tip = page.getByRole('tooltip')
  await tip.waitFor({ timeout: 5000 }).catch(() => {})
  const tipText = await tip.textContent().catch(() => null)
  console.log(theme, JSON.stringify({ title, tipText }))
  if (title !== null || tipText !== 'Open in new tab') failures++
  await page.screenshot({ path: resolve(OUT, `after-hover-${theme}.png`) })

  await popout.click()
  const notice = page.getByText("Couldn't open this widget in a new tab. The window was blocked.")
  const shown = await notice.waitFor({ timeout: 5000 }).then(() => true, () => false)
  console.log(theme, JSON.stringify({ noticeShown: shown }))
  if (!shown) failures++
  await page.mouse.move(10, 300)
  await page.waitForTimeout(300)
  await page.screenshot({ path: resolve(OUT, `after-refused-${theme}.png`) })
  await ctx.close()
}
await browser.close()
process.exit(failures ? 1 : 0)
