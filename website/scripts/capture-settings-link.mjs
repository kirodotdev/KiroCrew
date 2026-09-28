/**
 * Evidence capture for the SettingsLink deep-link component.
 *
 * Two scenes against a Vite dev server with every /api/* request mocked:
 *   1. about-link.png     Settings → About with the "View all releases" row —
 *                         now rendered by <SettingsLink tab="releases">. The
 *                         anchor's href is asserted to be the settingsPath
 *                         output before shooting.
 *   2. releases-landed.png the state after CLICKING that link — the Releases
 *                         panel mounted at /settings/releases, proving the
 *                         minted route navigates for real.
 *
 * Usage: node scripts/capture-settings-link.mjs <viteBase>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = '../temp-screenshots/settings-link'
mkdirSync(outDir, { recursive: true })

const STATUS = {
  uptime: '2h', start_time: 0, sessions: 1, messages: 3, cron_jobs: 0,
  lessons: 0, subagents: 0, no_crons: false, branch: '', commit: '',
  release_channel: 'stable', version: '0.4.0', version_display: '0.4.0',
  update_available: false, update_can_apply: false,
  update_check_status: 'succeeded', update_command: 'kirocrew update',
  update_latest_version: '0.4.0', update_channel: 'stable',
  update_managed_by: 'kirocrew', update_commits_ahead: 0,
  update_commits_behind: 0, update_can_arm: false,
}

const CHANGELOG = [
  '## [0.4.0] — 2026-08-30',
  '',
  'Settings deep links get one shared write path.',
  '',
  '### Improvements',
  '- **Settings deep links** — prose and empty states can now link straight to a settings tab, sub-page, or a highlighted setting.',
  '',
  '## [0.3.0] — 2026-08-12',
  '',
  'Path-based settings navigation.',
].join('\n')

const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1280, height: 860 } })
await ctx.addInitScript(() => localStorage.setItem('mc-onboarded', '1'))
const p = await ctx.newPage()
await p.route('**/*', route => {
  const u = new URL(route.request().url())
  if (!u.pathname.startsWith('/api/')) return route.continue()
  const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  if (u.pathname === '/api/status') return json(STATUS)
  if (u.pathname.startsWith('/api/update/check')) return json({
    check_status: 'succeeded', update_available: false, error_code: null,
    latest_version: '0.4.0', channel: 'stable', managed_by: 'kirocrew',
    can_apply: false, update_command: 'kirocrew update', current_version: '0.4.0',
    commits_ahead: 0, commits_behind: 0,
  })
  if (u.pathname.startsWith('/api/changelog')) return json({ content: CHANGELOG })
  if (u.pathname.startsWith('/api/models')) return json({ models: [] })
  if (u.pathname.startsWith('/api/instances')) return json({ active: false, instances: [], warm_set_cap: 0, sso: {} })
  if (u.pathname.startsWith('/api/kiro-prerequisite')) return json({ ready: true, initial_setup_complete: true, setup_allowed: true })
  // List-shaped endpoints crash the app when handed `{}` (e.g.
  // pendingApprovals.filter): answer every array consumer with [].
  if (/approvals|sessions|crons|lessons|skills|notifications|artifacts|apps\b/.test(u.pathname)) return json([])
  return json({})
})

await p.goto(`${base}/settings/about`, { waitUntil: 'networkidle' })
const link = p.getByRole('link', { name: /view all releases/i }).first()
await link.waitFor({ state: 'visible', timeout: 20_000 })

// The component contract, asserted on the LIVE DOM: the SettingsLink minted
// exactly the settingsPath route.
const href = await link.getAttribute('href')
if (href !== '/settings/releases') {
  throw new Error(`SettingsLink minted ${href}, expected /settings/releases`)
}
await link.scrollIntoViewIfNeeded()
await p.waitForTimeout(300)
await p.screenshot({ path: `${outDir}/about-link.png` })
console.log(`captured ${outDir}/about-link.png (href asserted: ${href})`)

await link.click()
await p.waitForURL('**/settings/releases', { timeout: 10_000 })
await p.waitForTimeout(500)
await p.screenshot({ path: `${outDir}/releases-landed.png` })
console.log(`captured ${outDir}/releases-landed.png (landed on ${p.url()})`)

await b.close()
