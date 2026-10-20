/**
 * Screenshot harness for the roster's hidden-crewmates line (#21852).
 *
 * The Crewmates column lists a crewmate unasked only when its thread holds a
 * message, it is starred, or it is Mate, and its count covers the listed rows
 * only. The fixture is the GUI user test's shape: three crewmates, two chatted
 * with and one created but never chatted with, so the column reads
 * "2 crewmates" and the third is reached only by searching for it.
 *
 * Run the SAME script against a dist built before and after the fix:
 *
 *   node scripts/capture-members-hidden-hint.mjs <outDir> <distDir>
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): no gateway, no auth, no kiro-cli. It prints
 * what the DOM says (count, the hidden line, the listed rows) for each frame.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'members-hidden-hint')
const DIST = process.argv[3] ? resolve(process.argv[3]) : DEFAULT_DIST
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: '/srv/repos/example', memory_store: `member-${name}`,
  memory_version: 2, memory_owner: name, model: '', source: 'kirocrew', dashboard_created: true, ...extra,
})
const MEMBERS = [
  member('trip-planner', {
    display_name: 'Trip Planner', has_dm_message: true, last_active_ts: now - 300,
    last_message: 'Here are three options for the weekend.',
  }),
  member('garden-helper', {
    display_name: 'Garden Helper', has_dm_message: true, last_active_ts: now - 5400,
    last_message: 'Water the tomatoes in the morning.',
  }),
  // Created on the dashboard, never chatted with: the row the rule hides.
  member('recipe-box', { display_name: 'Recipe Box', has_dm_message: false }),
  // ROSTER=many: three more hidden package rows, so the line counts instead of naming.
  ...(process.env.ROSTER === 'many'
    ? ['pkg-notes', 'pkg-weather', 'pkg-budget'].map((name) =>
      member(name, { source: 'package', dashboard_created: false, has_dm_message: false }))
    : []),
]
const AGENTS = MEMBERS.map((m) => ({
  name: m.name, display_name: m.display_name, kiro_agent: m.kiro_agent, workspace: m.workspace,
  memory_store: m.memory_store, model: m.model, description: '', source: m.source,
}))
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()
const SLOTS = MEMBERS.filter((m) => m.has_dm_message).map((m) => ({
  key: m.slot_key, title: m.display_name, mode: 'member', created: iso(86400),
  last_ts: iso(now - m.last_active_ts), running: false, project: '/srv/repos/example', agent: m.name,
}))
const STORAGE = {
  'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '1',
  'mc-members-panel-open': '0',
}

/** Exact-path fixtures, then the per-crewmate reads keyed by their last segment. */
const FIXTURES = {
  '/api/members': { members: MEMBERS, default_agent: 'trip-planner' },
  '/api/agents': { agents: AGENTS, default_agent: 'trip-planner' },
  '/api/default-agent': { default_agent: 'trip-planner' },
  '/api/crons': { jobs: [] },
  '/api/cron-folders': [],
  '/api/teams': { teams: [] },
  '/api/autonudge': { enabled: true, loops: [] },
  '/api/crew-board': { conductor: null, conductor_alive: 'closed', items: [], take_over_available: false },
}
const PER_MEMBER = {
  thread: (slug) => ({ slot_key: `member-${slug}`, slug, member: slug, created: false }),
  activity: () => ({ slug: '', member: '', capped: false, entries: [] }),
  briefing: () => ({ slug: '', member: '', supported: true, text: '', updated_ts: now, redacted: false, truncated: false }),
  panel: () => ({ panel: null, html: null }),
}

const extra = async (path, route) => {
  if (Object.hasOwn(FIXTURES, path)) {
    await json(route, FIXTURES[path])
    return true
  }
  const hit = path.match(/^\/api\/members\/([^/]+)\/(thread|activity|briefing|panel)$/)
  if (!hit) return false
  await json(route, PER_MEMBER[hit[2]](decodeURIComponent(hit[1])))
  return true
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 2, colorScheme: 'light' })
const page = await context.newPage()
logPageProblems(page)
await stubDashboardApi(page, { theme: 'light', extra, slots: SLOTS, localStorageEntries: STORAGE })
await page.goto(`${base}/members?member=trip-planner`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
// A crewmate is open, so the roster column is folded; pin it back beside the thread.
await page.getByTestId('crewmate-switcher').click()
await page.getByTestId('crewmate-switcher-roster').click()
const roster = page.getByTestId('member-roster')
await roster.waitFor({ state: 'visible', timeout: 15000 })
await page.waitForTimeout(700)

const read = async () => ({
  count: (await page.getByTestId('member-count').innerText()).trim(),
  hint: (await page.getByTestId('member-hidden-hint').count())
    ? (await page.getByTestId('member-hidden-hint').innerText()).trim()
    : '(none)',
  rows: await page.locator('[data-testid^="member-star-"]').evaluateAll((els) =>
    els.map((e) => e.getAttribute('data-testid').replace('member-star-', ''))),
})

const atRest = await read()
console.log(`at rest: count="${atRest.count}" rows=${atRest.rows.join(',')} hidden-line="${atRest.hint}"`)
await roster.screenshot({ path: join(OUT, '01-roster-at-rest.png') })

await page.getByTestId('member-search').fill('recipe')
await page.waitForTimeout(400)
const searched = await read()
console.log(`search "recipe": count="${searched.count}" rows=${searched.rows.join(',')} hidden-line="${searched.hint}"`)
await roster.screenshot({ path: join(OUT, '02-roster-search.png') })

await browser.close()
srv.close()
console.log(`shots in ${OUT}`)
