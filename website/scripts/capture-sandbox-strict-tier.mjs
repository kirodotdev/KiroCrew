/**
 * Screenshots of the Developer > Config "Sandbox" select offering the `strict`
 * tier beside `auto` and `off`, on the gateway-free API stub (no token, no
 * backend): the row at rest with the shipped `auto` still selected, and the
 * open list showing all three tiers.
 *
 * Usage: node scripts/capture-sandbox-strict-tier.mjs [outDir]
 * Requires a built `dist/` (`npm run build`).
 */
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sandbox-strict-tier'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await (
  await browser.newContext({ viewport: { width: 1180, height: 760 }, deviceScaleFactor: 2 })
).newPage()
await stubDashboardApi(page, { theme: 'dark' })

await page.goto(`${base}/developer?tab=config`, { waitUntil: 'domcontentloaded' })
const trigger = page.getByRole('combobox', { name: 'Sandbox' })
await trigger.waitFor({ timeout: 20_000 })
// The shipped default must still be what the row shows before anyone touches it.
await trigger.getByText('auto', { exact: true }).waitFor({ timeout: 20_000 })
await trigger.scrollIntoViewIfNeeded()
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/sandbox-row-default-auto.png` })

await trigger.click()
const listbox = page.getByRole('listbox')
await listbox.waitFor({ timeout: 20_000 })
for (const tier of ['auto', 'strict', 'off']) {
  await listbox.getByRole('option', { name: tier, exact: true }).waitFor({ timeout: 20_000 })
}
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/sandbox-select-open-three-tiers.png` })

console.log(`captured 2 shots into ${OUT}`)
await browser.close()
srv.close()
