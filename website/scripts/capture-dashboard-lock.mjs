/**
 * Screenshots for PR #18687: the lock badge on the Dashboard tab (dark + light),
 * and the lock switch on the Profile card (dark + light, locked + unlocked).
 *
 * Boots an in-process Vite dev server (port 0 so no collisions), then drives
 * the REAL built entries:
 *   capture/dashboard-lock.html          – badge on Dashboard tab
 *   capture/profile-dashboard-lock.html  – switch on Profile card
 *
 * Usage:  node scripts/capture-dashboard-lock.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'

const OUT = process.argv[2] || '../temp-screenshots/dashboard-lock'
mkdirSync(OUT, { recursive: true })

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const server = await createServer({
  configFile: 'vite.config.ts',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const addr = server.httpServer.address()
const BASE = `http://127.0.0.1:${addr.port}`
console.log(`serving ${BASE}`)

const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok, detail = '') => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'} ${detail}`)
  if (!ok) failures++
}

/* ── Badge: small Locked indicator on the Dashboard tab ─────────────────── */
async function shootBadge(theme, tag) {
  const page = await browser.newPage({ viewport: { width: 430, height: 320 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.error(`pageerror [${tag}]:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/dashboard-lock.html?theme=${theme}&locked=1`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const badge = page.locator('[data-testid="crew-dashboard-lock-badge"]')
  await badge.waitFor({ state: 'visible', timeout: 15000 })
  const text = await badge.innerText()
  check(`${tag}: badge says Layout locked`, text.trim().startsWith('Layout locked'), JSON.stringify(text))
  check(`${tag}: no switch control`, (await page.locator('[role="switch"]').count()) === 0)
  const painted = await page.evaluate(() => document.documentElement.dataset.theme)
  check(`${tag}: theme kiro-${theme}`, painted === `kiro-${theme}`)
  await page.screenshot({ path: `${OUT}/${tag}.png` })
  await page.close()
}

/* ── Badge on a rendered page (iframe content visible) ─────────────────── */
async function shootBadgeWorking(theme, tag) {
  const page = await browser.newPage({ viewport: { width: 430, height: 420 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.error(`pageerror [${tag}]:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/dashboard-lock.html?theme=${theme}&locked=1`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  const badge = page.locator('[data-testid="crew-dashboard-lock-badge"]')
  await badge.waitFor({ state: 'visible', timeout: 15000 })
  // Wait for the iframe to appear so the screenshot shows real content, not the error view.
  await page.locator('[data-testid="crew-dashboard-iframe"]').waitFor({ state: 'visible', timeout: 10000 })
  check(`${tag}: badge present`, (await badge.count()) === 1)
  check(`${tag}: no switch/button`, (await page.locator('[role="switch"]').count()) === 0)
  check(`${tag}: iframe present`, (await page.locator('[data-testid="crew-dashboard-iframe"]').count()) === 1)
  const painted = await page.evaluate(() => document.documentElement.dataset.theme)
  check(`${tag}: theme kiro-${theme}`, painted === `kiro-${theme}`)
  await page.screenshot({ path: `${OUT}/${tag}.png` })
  await page.close()
}

/* ── Profile switch flip error (lock mutation fails, error shown under switch) ── */
async function shootSwitchFlipError(theme, tag) {
  const page = await browser.newPage({ viewport: { width: 380, height: 580 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.error(`pageerror [${tag}]:`, e.message); failures++ })
  await page.goto(
    `${BASE}/capture/profile-dashboard-lock.html?theme=${theme}&locked=0&flipError=1`,
    { waitUntil: 'domcontentloaded' },
  )
  await page.waitForSelector('[data-capture-root]', { timeout: 20000 })
  const sw = page.getByRole('switch', { name: 'Lock dashboard layout' })
  await sw.waitFor({ state: 'visible', timeout: 10000 })
  // Trigger a lock flip that the mock will reject with 500.
  await sw.click()
  const err = page.locator('[data-testid="crew-profile-dashboard-lock-error"]')
  await err.waitFor({ state: 'visible', timeout: 10000 })
  check(`${tag}: error notice visible`, (await err.count()) === 1)
  const painted = await page.evaluate(() => document.documentElement.dataset.theme)
  check(`${tag}: theme kiro-${theme}`, painted === `kiro-${theme}`)
  await page.screenshot({ path: `${OUT}/${tag}.png` })
  await page.close()
}

/* ── Profile switch error state (lock-read fails, switch hidden) ─────────── */
async function shootSwitchError(theme, tag) {
  const page = await browser.newPage({ viewport: { width: 380, height: 640 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.error(`pageerror [${tag}]:`, e.message); failures++ })
  await page.goto(
    `${BASE}/capture/profile-dashboard-lock-error.html?theme=${theme}`,
    { waitUntil: 'domcontentloaded' },
  )
  await page.waitForSelector('[data-capture-root]', { timeout: 20000 })
  const err = page.locator('[data-testid="crew-profile-dashboard-lock-error"]')
  await err.waitFor({ state: 'visible', timeout: 10000 })
  check(`${tag}: error notice visible`, (await err.count()) === 1)
  check(`${tag}: switch hidden on error`, (await page.locator('[role="switch"]').count()) === 0)
  const painted = await page.evaluate(() => document.documentElement.dataset.theme)
  check(`${tag}: theme kiro-${theme}`, painted === `kiro-${theme}`)
  await page.screenshot({ path: `${OUT}/${tag}.png` })
  await page.close()
}

/* ── Profile switch: Lock dashboard layout toggle ──────────────────── */
async function shootSwitch(theme, locked, tag) {
  const page = await browser.newPage({ viewport: { width: 380, height: 580 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => { console.error(`pageerror [${tag}]:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/profile-dashboard-lock.html?theme=${theme}&locked=${locked ? 1 : 0}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-capture-root]', { timeout: 20000 })
  const lockRow = page.locator('[data-testid="crew-profile-dashboard-lock"]')
  await lockRow.waitFor({ state: 'visible', timeout: 10000 })
  const sw = page.getByRole('switch', { name: 'Lock dashboard layout' })
  await sw.waitFor({ state: 'visible' })
  const checked = await sw.getAttribute('aria-checked')
  check(`${tag}: switch aria-checked=${locked}`, checked === String(locked))
  const painted = await page.evaluate(() => document.documentElement.dataset.theme)
  check(`${tag}: theme kiro-${theme}`, painted === `kiro-${theme}`)
  await page.screenshot({ path: `${OUT}/${tag}.png` })
  await page.close()
}

// Badge (locked Dashboard tab, compact state)
await shootBadge('dark', 'dashboard-badge-dark')
await shootBadge('light', 'dashboard-badge-light')

// Badge on a rendered page
await shootBadgeWorking('dark',  'badge-working-dark')
await shootBadgeWorking('light', 'badge-working-light')

// Profile switch error state (lock-read failure: switch hidden)
await shootSwitchError('dark', 'switch-error-dark')

// Profile switch flip error (lock mutation fails: error shown under switch)
await shootSwitchFlipError('dark', 'switch-flip-error-dark')

// Profile switch — locked and unlocked in dark; locked in light
await shootSwitch('dark', true,  'profile-switch-locked-dark')
await shootSwitch('dark', false, 'profile-switch-unlocked-dark')
await shootSwitch('light', true, 'profile-switch-locked-light')

await browser.close()
await server.close()

if (failures) {
  console.error(`\n${failures} assertion(s) FAILED`)
  process.exit(1)
}
console.log('\nall frames verified')
