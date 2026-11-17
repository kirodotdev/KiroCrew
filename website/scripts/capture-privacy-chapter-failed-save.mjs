/**
 * Screenshot harness: the first-run Privacy chapter after a save fails (#37516).
 *
 * The chapter's only exits were Keep on and Turn off, both config writes, so a
 * caller whose writes the gateway refuses (a relay teammate, say) was stuck on it.
 * After a failed save it now offers Close (and Escape), which hides it without
 * recording a choice. These frames show that state and that Close hides it.
 *
 * Runs the REAL SPA with every /api/** call answered from fixtures (no gateway),
 * the same way scripts/capture-onboarding-import-unreadable.mjs does. The theme
 * write answers 403 `owner_required`, as the owner gate does for a teammate.
 *
 * Two captures at 1100x820:
 *   1. failed-save-close: Keep on was refused; the error and the new Close show.
 *   2. after-close: Close hid the chapter; the dashboard is reachable again.
 *
 * Usage: node scripts/capture-privacy-chapter-failed-save.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { handleBootRoute } from './lib/boot-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:7824'
const OUT = process.argv[3] || '../temp-screenshots/privacy-chapter-failed-save'

mkdirSync(OUT, { recursive: true })

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function preparePage(context) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    // Import done, Privacy due: the chapter opens on its own at load.
    if (path === '/api/theme/boot') {
      return json(route, { mode: 'light', color: '', onboarded: false, import_onboarded: true, privacy_acked: false })
    }
    // Every first-run write is refused, as the owner gate refuses a teammate's.
    if (path === '/api/config/theme' && request.method() === 'PUT') {
      return json(route, { error: 'owner required', code: 'owner_required' }, 403)
    }
    if (path === '/api/telemetry/collection') return json(route, { enabled: true, env_pinned: false })
    if (path === '/api/telemetry/beacon') {
      return json(route, {
        enabled: true, would_send: true, reason: 'ready', reason_code: 'ready',
        endpoint_configured: true, env_override: false, env_var: 'KIROCREW_TELEMETRY_DISABLED',
        governance_override: false,
      })
    }
    // A non-empty session list: with zero slots the sidebar's reveal-flash path
    // dereferences null (see capture-onboarding-import-unreadable.mjs).
    if (path === '/api/chat/slots') return json(route, [{
      key: 'chat-1', title: 'First run', agent: 'kirocrew', running: false, messages: 0, tags: [],
      last_ts: new Date().toISOString(), created: new Date().toISOString(),
    }])
    if (path === '/api/chat/tags') return json(route, [])
    // The dock's starred Bots read; unanswered it paints its own error toast.
    if (path === '/api/members') return json(route, { members: [] })
    if (path === '/api/chat/folders') return json(route, [])
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    return handleBootRoute(route, path, { project: '/tmp/demo', theme: 'light' })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.removeItem('mc-onboarded')
    localStorage.removeItem('mc-privacy-acked')
  })
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)
  return page
}

const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })

/** Fail the run when a frame does not show what it is meant to evidence. */
async function assertCount(page, label, locator, want) {
  const count = await locator.count()
  const ok = want === 'some' ? count > 0 : count === want
  console.log(`${label}: ${count} match(es) ${ok ? 'OK' : 'WRONG'}`)
  if (!ok) throw new Error(`${label}: expected ${want}, found ${count}`)
}

async function assertNoOverlay(page) {
  if (await page.locator('vite-error-overlay').count() > 0) {
    const text = await page.locator('vite-error-overlay').innerText().catch(() => '')
    throw new Error(`dev-server error overlay is covering the page:\n${text.slice(0, 400)}`)
  }
}

const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
try {
  const context = await browser.newContext({ viewport: { width: 1100, height: 820 }, deviceScaleFactor: 2 })
  const page = await preparePage(context)
  await assertNoOverlay(page)
  const dialog = page.getByRole('dialog', { name: 'Privacy' })
  await assertCount(page, 'chapter open', dialog, 1)
  await assertCount(page, 'no Close before a failed save', dialog.getByRole('button', { name: 'Close' }), 0)

  await dialog.getByRole('button', { name: 'Keep on' }).click()
  await page.getByText(/Your choice could not be saved/).waitFor()
  await assertCount(page, 'Close offered after the failed save', dialog.getByRole('button', { name: 'Close' }), 1)
  await page.mouse.move(0, 0)
  await shot(page, '1-failed-save-close')

  await dialog.getByRole('button', { name: 'Close' }).click()
  await page.waitForTimeout(800)
  await assertCount(page, 'chapter hidden after Close', page.getByRole('dialog', { name: 'Privacy' }), 0)
  await shot(page, '2-after-close')
  await context.close()
  console.log(`\nwrote 2 frames to ${OUT}`)
} finally {
  await browser.close()
}
