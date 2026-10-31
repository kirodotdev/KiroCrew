/**
 * Screenshots of the simplified Settings -> Browser page.
 *
 * Drives the ISOLATED capture entry (website/capture/browser-settings.html), which
 * mounts the REAL BrowserPanel with the gateway's answers replaced by fixtures.
 * Each frame asserts what it claims before it is written: the card's state, the
 * More group open or closed, a tooltip present.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-browser-settings.mjs http://127.0.0.1:6811 <out-dir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import path from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = path.resolve(process.argv[3] || '../temp-screenshots/browser-settings')
mkdirSync(OUT, { recursive: true })

const DESKTOP = { width: 1280, height: 900 }
const PHONE = { width: 390, height: 844 }

const browser = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : undefined,
)

async function shoot(name, { viewport, state = 'connected', more = false, tip = null, cred = '' }) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: 'dark', locale: 'en-US' })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto(`${BASE}/capture/browser-settings.html?state=${state}&theme=dark${cred ? `&cred=${cred}` : ''}`, { waitUntil: 'networkidle' })
  const card = page.getByTestId('browser-status-card')
  await card.waitFor({ timeout: 15000 })
  const got = await card.getAttribute('data-state')
  if (got !== state) throw new Error(`${name}: card state ${got}, expected ${state}`)
  if (more) {
    const btn = page.getByRole('button', { name: 'More', exact: true })
    await btn.click()
    if ((await btn.getAttribute('aria-expanded')) !== 'true') throw new Error(`${name}: More did not open`)
    await page.getByTestId('browser-engine-versions').waitFor()
  }
  if (tip) {
    // Keyboard focus opens the tip the same way hover does, and is deterministic.
    const glyph = page.locator(tip).getByRole('button', { name: 'More information' }).first()
    await glyph.focus()
    await page.getByRole('tooltip').waitFor({ timeout: 5000 })
  }
  await page.waitForTimeout(300)
  const file = path.join(OUT, `${name}.png`)
  await page.screenshot({ path: file, fullPage: true })
  if (errors.length) throw new Error(`${name}: page errors: ${errors.join('; ')}`)
  console.log(file)
  await ctx.close()
}

await shoot('desktop-connected', { viewport: DESKTOP })
await shoot('desktop-more-open', { viewport: DESKTOP, more: true })
await shoot('desktop-info-tip-open', { viewport: DESKTOP, tip: '[data-setting-key="browser.choice"]' })
await shoot('desktop-state-installing', { viewport: DESKTOP, state: 'installing' })
await shoot('desktop-state-waiting-allow', { viewport: DESKTOP, state: 'waiting_allow' })
await shoot('desktop-state-error', { viewport: DESKTOP, state: 'error' })
await shoot('desktop-state-install-failed', { viewport: DESKTOP, state: 'install_failed' })
await shoot('desktop-signin-applied', { viewport: DESKTOP, cred: 'applied' })
await shoot('desktop-signin-tip-open', { viewport: DESKTOP, cred: 'applied', tip: '[data-setting-key="browser.credentials.enabled"]' })
await shoot('desktop-signin-needs-refresh', { viewport: DESKTOP, cred: 'needs_refresh' })
await shoot('desktop-signin-more-open', { viewport: DESKTOP, cred: 'applied', more: true })
await shoot('phone-connected', { viewport: PHONE })
await shoot('phone-more-open', { viewport: PHONE, more: true })
await browser.close()
