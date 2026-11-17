/**
 * Screenshot harness: first run held because the owner check failed (#37516).
 *
 * First run opens only once /api/auth/me says this tab is the owner. When that
 * read fails and first run is still owed, the shell says so with Try again
 * instead of a dashboard that looks finished. These frames show the notice and
 * that Try again, once the check answers, lets first run go on.
 *
 * Runs the REAL SPA with every /api/** call answered from fixtures (no gateway),
 * the same way scripts/capture-onboarding-import-unreadable.mjs does.
 *
 * Two captures at 1100x820:
 *   1. owner-check-failed: the notice, with Try again; no chapter open.
 *   2. after-retry: the check answered as the owner; first run opens.
 *
 * Usage: node scripts/capture-first-run-owner-check-failed.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { handleBootRoute } from './lib/boot-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:7824'
const OUT = process.argv[3] || '../temp-screenshots/first-run-owner-check-failed'

mkdirSync(OUT, { recursive: true })

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

/** The owner read fails until the harness lets it answer. */
const state = { ownerUp: false }

async function preparePage(context) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    // Import and Privacy done, the tour still owed.
    if (path === '/api/theme/boot') {
      return json(route, { mode: 'light', color: '', onboarded: false, import_onboarded: true, privacy_acked: true })
    }
    if (path === '/api/auth/me') {
      return state.ownerUp
        ? json(route, { user_id: 'owner', owner_ok: true })
        : json(route, { error: 'bad gateway' }, 502)
    }
    if (path === '/api/chat/slots') return json(route, [{
      key: 'chat-1', title: 'First run', agent: 'kirocrew', running: false, messages: 0, tags: [],
      last_ts: new Date().toISOString(), created: new Date().toISOString(),
    }])
    if (path === '/api/chat/tags') return json(route, [])
    if (path === '/api/chat/folders') return json(route, [])
    if (path === '/api/members') return json(route, { members: [] })
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    return handleBootRoute(route, path, { project: '/tmp/demo', theme: 'light' })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
    localStorage.removeItem('mc-onboarded')
  })
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)
  return page
}

const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })

async function assertCount(label, locator, want) {
  const count = await locator.count()
  const ok = want === 'some' ? count > 0 : count === want
  console.log(`${label}: ${count} match(es) ${ok ? 'OK' : 'WRONG'}`)
  if (!ok) throw new Error(`${label}: expected ${want}, found ${count}`)
}

const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
try {
  const context = await browser.newContext({ viewport: { width: 1100, height: 820 }, deviceScaleFactor: 2 })
  const page = await preparePage(context)
  if (await page.locator('vite-error-overlay').count() > 0) throw new Error('dev-server error overlay is covering the page')
  const notice = page.getByTestId('first-run-owner-check-error')
  await assertCount('notice shown', notice, 1)
  await assertCount('tour not open', page.getByText('Pick your look'), 0)
  await page.mouse.move(0, 0)
  await shot(page, '1-owner-check-failed')

  state.ownerUp = true
  await notice.getByRole('button', { name: 'Try again' }).click()
  await page.getByText('Pick your look').waitFor({ timeout: 10000 })
  await assertCount('notice gone after retry', page.getByTestId('first-run-owner-check-error'), 0)
  await shot(page, '2-after-retry')
  await context.close()
  console.log(`\nwrote 2 frames to ${OUT}`)
} finally {
  await browser.close()
}
