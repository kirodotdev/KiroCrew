/**
 * Screenshot harness for the terminal font picker.
 *
 * The claim needing evidence is that choosing a terminal font no longer means
 * typing a family name blind: the row is a searchable list of families the
 * BROWSER's machine actually has, each row rendered in its own family so the name
 * is its own preview, and that typing an arbitrary name still commits.
 *
 *   01-closed        the Terminal card at rest, Font row showing the default
 *   02-open          the list open: detected families, each in its own family
 *   03-custom        a name absent from the list, offered as a committable row
 *
 * The detected list is whatever THIS machine has installed, which is the honest
 * shot: the probe is measuring a real font book, not a fixture. The
 * "List all installed fonts…" action row appears only where the Local Font Access
 * API exists (Chromium, secure context), so its presence here is itself the
 * evidence that the row is feature-gated rather than always drawn.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-terminal-font-picker.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-font-picker'
const PROJECT = '/home/user/.kiro/crew/workspace'

mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({
  viewport: { width: 900, height: 820 },
  // The rows carry 11px preview glyphs; a 1x shot renders them too soft to judge.
  deviceScaleFactor: 2,
})
await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})
await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

const shoot = async (name, locator) => {
  await page.waitForTimeout(300)
  await locator.screenshot({ path: `${OUT}/${name}` })
  console.log(`wrote ${OUT}/${name}`)
}

await page.goto(`${base}/settings?tab=display`, { waitUntil: 'domcontentloaded' })

// The Terminal card is the crop for every frame: the whole settings page would
// bury an 11px preview row in an 8000px screenshot.
const fontRow = page.locator('[data-setting-label="Font"]').last()
await fontRow.waitFor({ timeout: 20000 })
const card = fontRow.locator('xpath=ancestor::div[contains(@class,"card-glow")][1]')

// The probe runs in an effect, so let it resolve before claiming the row is empty.
await page.waitForTimeout(1200)
await shoot('01-closed.png', card)

const trigger = page.getByRole('button', { name: 'Font' }).last()
await trigger.click()
await page.getByRole('listbox', { name: 'Font' }).waitFor({ timeout: 10000 })
await page.waitForTimeout(600)
const detected = await page.getByRole('option').count()
console.log(`detected rows in the open list: ${detected}`)
await shoot('02-open.png', card)

await page.getByRole('textbox', { name: 'Search fonts…' }).fill('Berkeley Mono')
await page.waitForTimeout(400)
await shoot('03-custom.png', card)

await browser.close()
srv.close()
