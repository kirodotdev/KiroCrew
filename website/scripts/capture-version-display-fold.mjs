/**
 * Evidence capture for the stable-stamp display fold (PR #6507).
 *
 * Three scenes against a Vite dev server with every /api/* request mocked:
 *   1. after:  status carries `version_display` and the check response carries
 *      `latest_version_display` — the chip AND the "a new version (vX)" line
 *      both show the clean `0.4.0`.
 *   2. before: same raw values, no display fields (an older gateway) — both
 *      surfaces fall back to the raw `0.4.0rc14` stamp.
 *
 * Usage: node scripts/capture-version-display-fold.mjs <viteBase>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = '../temp-screenshots/version-display-fold'
mkdirSync(outDir, { recursive: true })

const RAW = '0.4.0rc14'
const FOLDED = '0.4.0'

function statusBody(withDisplay) {
  return {
    uptime: '2h', start_time: 0, sessions: 1, messages: 3, cron_jobs: 0,
    lessons: 0, subagents: 0, no_crons: false, branch: '', commit: '',
    release_channel: 'stable', version: RAW,
    ...(withDisplay ? { version_display: FOLDED, update_latest_version_display: FOLDED } : {}),
    update_available: true, update_can_apply: false,
    update_check_status: 'succeeded', update_command: 'kirocrew update',
    update_latest_version: RAW, update_channel: 'stable',
    update_managed_by: 'kirocrew', update_commits_ahead: 0,
    update_commits_behind: 0, update_can_arm: false,
  }
}

function checkBody(withDisplay) {
  return {
    check_status: 'succeeded', update_available: true, error_code: null,
    latest_version: RAW, ...(withDisplay ? { latest_version_display: FOLDED } : {}),
    channel: 'stable', managed_by: 'kirocrew', can_apply: false,
    update_command: 'kirocrew update', current_version: FOLDED,
    commits_ahead: 0, commits_behind: 0,
  }
}

const b = await chromium.launch()

async function scene(name, withDisplay) {
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  await ctx.addInitScript(() => localStorage.setItem('mc-onboarded', '1'))
  const p = await ctx.newPage()
  await p.route('**/*', route => {
    const u = new URL(route.request().url())
    if (!u.pathname.startsWith('/api/')) return route.continue()
    const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (u.pathname === '/api/status') return json(statusBody(withDisplay))
    if (u.pathname.startsWith('/api/update/check')) return json(checkBody(withDisplay))
    if (u.pathname.startsWith('/api/changelog')) return json({ content: '' })
    if (u.pathname.startsWith('/api/models')) return json({ models: [] })
    if (u.pathname.startsWith('/api/instances')) return json({ active: false, instances: [], warm_set_cap: 0, sso: {} })
    if (u.pathname.startsWith('/api/kiro-prerequisite')) return json({ ready: true, initial_setup_complete: true, setup_allowed: true })
    // List-shaped endpoints crash the app when handed `{}` (e.g.
    // pendingApprovals.filter): answer every array consumer with [].
    if (/approvals|sessions|crons|lessons|skills|notifications|artifacts|apps\b/.test(u.pathname)) return json([])
    return json({})
  })
  await p.goto(`${base}/settings/about`, { waitUntil: 'networkidle' })
  // The proactive update popup opens over the page — evidence scene 1, then
  // dismiss it (Remind me tomorrow) to reach the About panel underneath.
  const remind = p.getByRole('button', { name: /remind me tomorrow/i }).first()
  await remind.waitFor({ state: 'visible', timeout: 20_000 })
  await p.waitForTimeout(300)
  await p.screenshot({ path: `${outDir}/modal-${name}` })
  console.log(`captured ${outDir}/modal-${name}`)
  await remind.click()
  await p.waitForTimeout(800)
  await p.screenshot({ path: `${outDir}/${name}` })
  console.log(`captured ${outDir}/${name}`)
  await ctx.close()
}

await scene('about-after-chip-and-line-folded.png', true)
await scene('about-before-raw-stamp.png', false)
await b.close()
