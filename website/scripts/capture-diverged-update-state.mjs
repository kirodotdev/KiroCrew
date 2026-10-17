/**
 * Screenshot harness: the diverged-git-checkout update state (issue #4498).
 *
 * A diverged checkout (local commits on top of a moved upstream) reports
 * `update_available: false` by design — the apply path is a destructive
 * `git reset --hard` — and the About panel used to render that as "You're on
 * the latest version". This captures the third verdict the panel now renders,
 * so "diverged says rebase/merge, not up to date" is a picture rather than a
 * claim.
 *
 * Runs the REAL SPA with every /api/** call answered from fixtures (no
 * gateway), the same way scripts/capture-list-view-tag-filter.mjs does.
 *
 * Two captures at 1400x900:
 *   1. diverged   — check response carrying commits_ahead=3 /
 *      commits_behind=219: the warning line with both counts and the
 *      rebase/merge instruction, and NO Update button.
 *   2. up-to-date — same layout with zero counts: the existing success line,
 *      unchanged (the regression half of the evidence).
 *
 * Output filenames are the ones committed under
 * `temp-screenshots/diverged-update-state/`, so re-running this after a change
 * lands on the reviewed files instead of a parallel set someone copies by hand.
 *
 * Usage: node scripts/capture-diverged-update-state.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:4173'
const OUT = process.argv[3] || '../temp-screenshots/diverged-update-state'

mkdirSync(OUT, { recursive: true })

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

/** @param diverged true answers /api/update/check with the diverged counts;
 *  false answers with a genuinely current checkout (both counts 0). */
async function preparePage(context, { diverged }) {
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/update/check') return json(route, {
      supported: true, managed_by: 'git', mode: 'git', can_download: false,
      can_apply: true, requires_restart: true, channel: '',
      latest_version: '0.3.0', changes: '', check_status: 'succeeded',
      update_available: false, version_newer: false,
      commits_ahead: diverged ? 3 : 0, commits_behind: diverged ? 219 : 0,
      error_code: null, unavailable_reason: null, remediation: null,
      current_version: '0.3.0', auto_update: true,
    })
    if (path === '/api/kiro-prerequisite') return json(route, {
      platform: 'linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: false,
      operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
    })
    if (path === '/api/status') return json(route, { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: '0.3.0' })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/changelog') return json(route, { content: '' })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary|prerequisite)/.test(path)
    return json(route, objectish ? {} : [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('kc-onboarded', '1')
  })
  await page.goto(`${BASE}/settings?tab=about`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  return page
}

async function pressCheck(page) {
  const btn = page.getByRole('button', { name: /check for updates/i }).first()
  await btn.waitFor({ timeout: 15_000 })
  await btn.click()
  await page.waitForTimeout(1200)
}

const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })

/** Fail the run when a frame does not contain what it is meant to evidence. */
async function assertVisible(page, testId, label) {
  const n = await page.locator(`[data-testid="${testId}"]`).count()
  console.log(`${label}: ${testId} count=${n}`)
  if (n !== 1) throw new Error(`${label}: expected exactly one ${testId}, got ${n}`)
}

// A mise-managed node exports its own `lib/node` on LD_LIBRARY_PATH inside the
// node process, and the browser child inherits it — point the browser at the
// system path; harmless when node is not mise-managed.
const browser = await chromium.launch({
  env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' },
})
try {
  // 1. Diverged: the counts, the rebase/merge instruction, no Update button.
  {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    const page = await preparePage(context, { diverged: true })
    await pressCheck(page)
    await assertVisible(page, 'diverged', 'diverged frame')
    await assertVisible(page, 'hero-diverged', 'diverged frame (hero badge)')
    const upToDate = await page.locator('[data-testid="up-to-date"]').count()
    if (upToDate !== 0) throw new Error(`diverged frame: up-to-date line must be absent, got ${upToDate}`)
    const heroOk = await page.locator('[data-testid="hero-up-to-date"]').count()
    if (heroOk !== 0) throw new Error(`diverged frame: hero up-to-date badge must be absent, got ${heroOk}`)
    await page.locator('[data-testid="diverged"]').scrollIntoViewIfNeeded()
    await shot(page, '1-diverged')
    await context.close()
  }

  // 2. Current: the success line, exactly as before the change.
  {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    const page = await preparePage(context, { diverged: false })
    await pressCheck(page)
    await assertVisible(page, 'up-to-date', 'up-to-date frame')
    await assertVisible(page, 'hero-up-to-date', 'up-to-date frame (hero badge)')
    const div = await page.locator('[data-testid="diverged"]').count()
    if (div !== 0) throw new Error(`up-to-date frame: diverged line must be absent, got ${div}`)
    await page.locator('[data-testid="up-to-date"]').scrollIntoViewIfNeeded()
    await shot(page, '2-up-to-date')
    await context.close()
  }
  console.log('captures written to', OUT)
} finally {
  await browser.close()
}
