/**
 * Screenshot harness for the phone nav drawer's "Sign in to Kiro" item: a
 * phone-width page on a harness other than Kiro Agent (Claude here), in a
 * build that offers Kiro sign-in, with no Kiro sign-in stored. The drawer is
 * opened and must show the item; clicking it must land on Settings > Kiro account.
 *
 * Usage: npm run build && node scripts/capture-kiro-sign-in-phone-drawer.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/kiro-sign-in-phone-drawer'
mkdirSync(OUT, { recursive: true })

const SIGNED_OUT = {
  authenticated: false, provider: '', identity: '', transport: 'device', expires_at: null,
  expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}
const CONFIG = { ...KIROCREW_CONFIG_FIXTURE, agent: { ...(KIROCREW_CONFIG_FIXTURE.agent || {}), acp_backend: 'claude' } }
const SCHEMA = { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['kiro', 'claude', 'kas'] }] }

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
const page = await context.newPage()
await stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path === '/api/config/kirocrew') { await json(route, CONFIG); return true }
    if (path === '/api/config/schema') { await json(route, SCHEMA); return true }
    if (path === '/api/kas-login') { await json(route, SIGNED_OUT); return true }
    // Off Kiro Agent the usage read is refused (kiro-cli not signed in), so no
    // balance is shown and the sign-in item takes its place.
    if (path === '/api/sessions/usage') { await json(route, { error: 'kiro_prerequisite_required' }, 503); return true }
    return false
  },
})
logPageProblems(page)
await page.goto(`${base}/settings/chat`, { waitUntil: "domcontentloaded" })
await page.locator('[data-ui-location="shell.mobile-menu"]').first().click()
const item = page.getByRole('link', { name: 'Sign in to Kiro' }).or(page.getByRole('button', { name: 'Sign in to Kiro' })).first()
try {
  await item.waitFor({ timeout: 20000 })
} catch {
  fail('the open drawer shows no "Sign in to Kiro" item')
}
await page.waitForTimeout(500)
await page.screenshot({ path: `${OUT}/09-phone-drawer-sign-in-to-kiro.png` })
console.log('wrote 09-phone-drawer-sign-in-to-kiro')
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
