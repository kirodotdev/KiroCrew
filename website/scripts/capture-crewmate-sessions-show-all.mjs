/**
 * Real-browser evidence on the REAL Crewmates page: the profile card's Sessions
 * tab lists EVERY session the crewmate is driving. No fold, no "Show all"
 * button, however many rows there are.
 *
 * Runs the built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): no gateway, no auth, no kiro-cli. The fixture
 * seeds eight driven sessions, more than the fold used to show.
 *
 * Frames per theme (light, dark), clipped to the card:
 *   sessions-all-<theme>.png
 *
 * Usage (in website/):
 *   node scripts/capture-crewmate-sessions-show-all.mjs ../temp-screenshots/crewmate-sessions-show-all
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'crewmate-sessions-show-all')
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()
const MEMBER = {
  name: 'kiro', slug: 'kiro', display_name: 'Kiro', bound: true, slot_key: 'member-kiro', running: true,
  kiro_agent: 'frontend', workspace: '/srv/repos/kiro', memory_store: 'member-kiro',
  memory_version: 2, memory_owner: 'kiro', model: '', source: 'kirocrew',
  last_active_ts: now - 60, last_message: 'All 85 checks are green. Only human review is left.',
}
const AGENTS = [{
  name: 'kiro', display_name: 'Kiro', kiro_agent: 'frontend', workspace: '/srv/repos/kiro',
  memory_store: 'member-kiro', model: '', source: 'kirocrew',
  description: 'Frontend crewmate for the Kiro Crew dashboard.',
}]
const DRIVEN = [
  ['w-capture', 'Capture Welcome screenshots at 390px', 40, true],
  ['w-review', 'Review PR #15731 as a new user', 120, true],
  ['w-rebase', 'Rebase topbar-glass onto main', 300, true],
  ['w-ask', 'Pick the mates button look', 700, false],
  ['w-i18n', 'Translate the new Settings copy', 1400, true],
  ['w-perf', 'Profile the sidebar first paint', 2600, false],
  ['w-docs', 'Update the crew-mode spec', 4100, false],
  ['w-done', 'Explain the panel IA from the sketch', 5400, false],
]
const SLOTS = [
  { key: 'member-kiro', title: 'Kiro', mode: 'member', created: iso(86400), last_ts: iso(60), running: true, project: '/srv/repos/kiro', agent: 'kiro' },
  ...DRIVEN.map(([key, title, ago, running], i) => ({
    key, title, created_by: 'member-kiro', created: iso(900 * (i + 1)), last_ts: iso(ago), running, project: '/srv/repos/kiro',
  })),
]
const STORAGE = { 'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '1', 'mc-members-panel-open': '0' }

const extra = async (path, route) => {
  if (path === '/api/members') { await json(route, { members: [MEMBER], default_agent: 'kirocrew' }); return true }
  if (path === '/api/agents') { await json(route, { agents: AGENTS, default_agent: 'kiro' }); return true }
  if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
  if (path === '/api/cron-folders') { await json(route, []); return true }
  if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
  if (/^\/api\/members\/[^/]+\/thread$/.test(path)) { await json(route, { slot_key: 'member-kiro', slug: 'kiro', member: 'kiro', created: false }); return true }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: '', member: '', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/goals$/.test(path)) { await json(route, { goals: [] }); return true }
  if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) { await json(route, { slug: '', member: '', supported: true, text: '', updated_ts: 0, redacted: false, truncated: false }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
  if (/^\/api\/autonudge\/slot\//.test(path)) { await json(route, { enabled: true, loop: null }); return true }
  if (path === '/api/monitors') { await json(route, { enabled: true, monitors: [] }); return true }
  if (/^\/api\/monitors\/slot\//.test(path)) { await json(route, { enabled: true, monitor: null }); return true }
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

for (const theme of ['light', 'dark']) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme, extra, slots: SLOTS, localStorageEntries: STORAGE })
  await page.goto(`${base}/members?member=kiro`, { waitUntil: 'domcontentloaded' })
  const pill = page.getByTestId('member-identity-pill')
  await pill.waitFor({ state: 'visible', timeout: 30000 })
  await pill.click()
  const card = page.getByTestId('crew-profile-card')
  await card.waitFor({ state: 'visible', timeout: 10000 })
  const list = page.getByTestId('crew-profile-driving')
  await list.waitFor({ state: 'visible', timeout: 10000 })
  const rows = await page.getByTestId('crew-profile-driving-row').count()
  check(`[${theme}] every driven session is a row (${rows} of ${DRIVEN.length})`, rows === DRIVEN.length)
  const sessions = page.getByTestId('crew-profile-sessions')
  check(`[${theme}] no fold toggle under the list`, (await sessions.locator('button[aria-expanded]').count()) === 0)
  check(`[${theme}] no "Show all" wording`, !/Show all|Show less/.test(await sessions.innerText()))
  await page.mouse.move(5, 5)
  await card.screenshot({ path: join(OUT, `sessions-all-${theme}.png`) })
  await context.close()
}

await browser.close()
srv.close()
if (failed) { console.error('check(s) failed'); process.exit(1) }
console.log(`wrote ${OUT}`)
