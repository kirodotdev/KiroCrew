/**
 * Screenshot harness for the Crew switcher's Team Crews group when the person is
 * signed out of Kiro: the list read is refused with SIGNED_OUT, the group shows
 * the refusal, and a "Sign in to Kiro" menu item leads to Settings > Kiro
 * account. The frame asserts the item before writing the PNG, then clicks it and
 * asserts the route.
 *
 * The desktop bridge (`window.relayCrewAPI`) is stubbed so the switcher mounts;
 * every /api/** call is answered from fixtures.
 *
 * Usage: npm run build && node scripts/capture-team-crews-signed-out.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/team-crews-signed-out'
mkdirSync(OUT, { recursive: true })

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
const page = await context.newPage()
logPageProblems(page)
await stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path === '/api/relay-viewer/crews') { await json(route, { error: 'not signed in', code: 'SIGNED_OUT' }, 401); return true }
    return false
  },
})
await page.addInitScript(() => { window.relayCrewAPI = { open: async () => ({ ok: true }) } })
await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
await page.getByRole('button', { name: /^Switch crew/ }).first().click({ timeout: 30000 })
const group = page.getByTestId('team-crews-group')
await group.waitFor({ timeout: 10000 })
const item = page.getByRole("menuitem", { name: "Sign in to Kiro" })
await item.waitFor({ timeout: 10000 }).catch(() => fail('the signed-out Team Crews refusal has no "Sign in to Kiro" item'))
if (!/Sign in to Kiro to see your Team Crews/.test((await group.innerText()) || '')) fail('the group does not show the signed-out refusal')
await page.waitForTimeout(400)
const box = await page.getByRole('menu').boundingBox()
await page.screenshot({ path: `${OUT}/12-team-crews-signed-out.png`, clip: { x: Math.max(0, box.x - 8), y: Math.max(0, box.y - 50), width: box.width + 16, height: box.height + 58 } })
console.log('wrote 12-team-crews-signed-out')
if (!failures) {
  await item.click()
  await page.waitForURL(/\/settings\/kiro-account/, { timeout: 10000 }).catch(() => fail('the item does not open Settings > Kiro account'))
}

await browser.close()
srv.close()
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
