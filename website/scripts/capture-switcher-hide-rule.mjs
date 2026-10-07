/**
 * Screenshot harness for the folded roster's hide rule (CrewmateSwitcher).
 *
 * The Crewmates COLUMN lists rows through `rosterPopulation` / `listedByDefault`
 * -- created, chatted-with, starred rows and the default crew -- while the header
 * CHIP used to be handed the raw roster and listed everything. A user with two
 * crewmates therefore read "2 crewmates" in the column beside a chip that said
 * 10 and offered every sync-generated template.
 *
 * The fixture reproduces exactly that roster: 10 rows, of which 2 are listed
 * (the default crew and one chatted-with crewmate) and 8 are package-sourced
 * templates nobody has chatted with (`has_dm_message: false`,
 * `dashboard_created: false`).
 *
 * Run the SAME script against a dist built before and after the fix:
 *
 *   node scripts/capture-switcher-hide-rule.mjs <outDir> <distDir>
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): no gateway, no auth, no kiro-cli.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'switcher-hide-rule')
const DIST = process.argv[3] ? resolve(process.argv[3]) : DEFAULT_DIST
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: name, workspace: '/srv/repos/kirocrew', memory_store: `member-${name}`,
  memory_version: 2, memory_owner: name, model: '', source: 'kirocrew', ...extra,
})
/** A row the roster LISTS: its DM thread holds a message. */
const chatted = (name, extra = {}) => member(name, { has_dm_message: true, dashboard_created: true, ...extra })
/** A row the roster HIDES: a package template nobody has chatted with. */
const template = (name, extra = {}) =>
  member(name, { source: 'package', has_dm_message: false, dashboard_created: false, ...extra })

/** ROSTER=teams: the shape a user reported -- six crewmates they have all
 *  chatted with, grouped into Research / CREW / no team, beside the package
 *  templates the column hides. The question was which of the two readings is
 *  right when the chip and the column disagree. */
const TEAMS_MEMBERS = [
  chatted('Researcher', { display_name: 'Researcher', kiro_agent: 'kirocrew-research', last_active_ts: now - 120, last_message: "No, you don't need to restart anything. This research is …" }),
  chatted('kirocrew-competitive-intel', { last_active_ts: now - 900, last_message: 'The brief is done and in the vault at /Users/example/D…' }),
  chatted('kirocrew-conductor', { starred: true, last_active_ts: now - 1800, last_message: 'Probably not. This Conductor crew member is best whe…' }),
  chatted('crew-manager-conductor', { last_active_ts: now - 3600, last_message: 'Goal: You asked how I (crew-manager-conductor) diffe…' }),
  chatted('default', { display_name: 'Example Default', kiro_agent: 'kirocrew', last_active_ts: now - 7200, last_message: 'Your vault has a lot on this. Most of it sits in five recent …' }),
  chatted('amzn-pipeline-assistant', { last_active_ts: now - 9000, last_message: 'Yes, I\'m here. The earlier "agent spec unreadab…' }),
  template('kirocrew-heartbeat'),
  template('kirocrew-knowledge'),
  template('kirocrew-ledger-conductor'),
  template('kirocrew-pipeline-conductor'),
  template('kirocrew-security-conductor'),
  template('kirocrew-oncall'),
]
const TEAMS = [
  { id: 'team-research', name: 'Research', members: ['Researcher', 'kirocrew-competitive-intel'] },
  { id: 'team-crew', name: 'CREW', members: ['kirocrew-conductor', 'crew-manager-conductor'] },
]

const DEFAULT_MEMBERS = [
  chatted('crewmate', {
    display_name: 'Crew Mate', kiro_agent: 'kirocrew', running: true, last_active_ts: now - 45,
    last_message: 'Why do I have so many crewmates all of a sudden?',
  }),
  chatted('amzn-pipeline-assistant', {
    kiro_agent: 'amzn-pipeline-assistant', last_active_ts: now - 2400,
    last_message: 'A: (code) needs the real hash (SHA/commit) before it can answer.',
  }),
  template('kirocrew-conductor'),
  template('kirocrew-heartbeat'),
  template('kirocrew-knowledge'),
  template('kirocrew-ledger-conductor'),
  template('kirocrew-pipeline-conductor'),
  template('kirocrew-research'),
  template('kirocrew-security-conductor'),
  template('kirocrew-oncall'),
]

/** Which roster this run captures, and which crewmate's thread it opens. */
const TEAMS_SHAPE = process.env.ROSTER === 'teams'
const MEMBERS = TEAMS_SHAPE ? TEAMS_MEMBERS : DEFAULT_MEMBERS
const TEAM_ROWS = TEAMS_SHAPE ? TEAMS : []
const DEFAULT_CREW = TEAMS_SHAPE ? 'default' : 'crewmate'
const OPEN_MEMBER = TEAMS_SHAPE ? 'Researcher' : 'crewmate'
const AGENTS = MEMBERS.map((m) => ({
  name: m.name, display_name: m.display_name, kiro_agent: m.kiro_agent, workspace: m.workspace,
  memory_store: m.memory_store, model: m.model, description: '', source: m.source,
}))
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()
const SLOTS = MEMBERS.filter((m) => m.has_dm_message).map((m) => ({
  key: m.slot_key, title: m.display_name || m.name, mode: 'member',
  created: iso(86400), last_ts: iso(now - (m.last_active_ts ?? now)), running: !!m.running,
  project: '/srv/repos/kirocrew', agent: m.name,
}))
const STORAGE = {
  'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '1',
  'mc-members-panel-open': '0',
}

const extra = async (path, route) => {
  if (path === '/api/members') { await json(route, { members: MEMBERS, default_agent: DEFAULT_CREW }); return true }
  if (path === '/api/agents') { await json(route, { agents: AGENTS, default_agent: DEFAULT_CREW }); return true }
  if (path === '/api/default-agent') { await json(route, { default_agent: DEFAULT_CREW }); return true }
  if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
  if (path === '/api/cron-folders') { await json(route, []); return true }
  if (path === '/api/teams') { await json(route, { teams: TEAM_ROWS }); return true }
  if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
  const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) {
    const slug = decodeURIComponent(thread[1])
    await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false })
    return true
  }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: '', member: '', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) { await json(route, { slug: '', member: '', supported: true, text: '', updated_ts: now, redacted: false, truncated: false }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  return false
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const context = await browser.newContext({ viewport: { width: 1500, height: 940 }, deviceScaleFactor: 2, colorScheme: 'light' })
const page = await context.newPage()
logPageProblems(page)
await stubDashboardApi(page, { theme: 'light', extra, slots: SLOTS, localStorageEntries: STORAGE })
await page.goto(`${base}/members?member=${encodeURIComponent(OPEN_MEMBER)}`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
await page.waitForTimeout(700)

const chipCount = async () => (await page.getByTestId('crewmate-switcher-count').innerText()).trim()
const rows = async (list) => await list.getByRole('option').evaluateAll((els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()))

// 01: the chip, closed. The number beside the faces is the whole claim.
await page.mouse.move(5, 5)
console.log(`closed chip reads: ${await chipCount()}`)
await page.getByTestId('crewmate-switcher').screenshot({ path: join(OUT, '01-chip-closed.png') })
await page.screenshot({ path: join(OUT, '02-page-chip-closed.png') })

// 03: the chip, open. Which crewmates it offers.
await page.getByTestId('crewmate-switcher').click()
const list = await page.waitForSelector('[data-testid="crewmate-switcher-list"]')
await page.waitForTimeout(500)
const listed = await rows(page.getByTestId('crewmate-switcher-list'))
console.log(`open list offers ${listed.length}: ${listed.map((r) => r.split(' ')[0]).join(', ')}`)
await page.screenshot({ path: join(OUT, '03-chip-open.png') })
await page.locator('[data-testid="crewmate-switcher-list"]').screenshot({ path: join(OUT, '04-list-only.png') })

// 05: a hidden row is still REACHABLE from the search box.
await page.getByTestId('crewmate-switcher-search').fill('conductor')
await page.waitForTimeout(400)
const found = await rows(page.getByTestId('crewmate-switcher-list'))
console.log(`search "conductor" reaches ${found.length}: ${found.map((r) => r.split(' ')[0]).join(', ')}`)
check('the search reaches hidden template rows', found.length >= 3)
await page.locator('[data-testid="crewmate-switcher-list"]').screenshot({ path: join(OUT, '05-search-reaches-hidden.png') })
await page.getByTestId('crewmate-switcher-search').fill('')
await page.waitForTimeout(300)

// 06: the money shot -- the column and the chip in ONE frame, so the two counts
// can be read against each other.
await page.getByTestId('crewmate-switcher-roster').click()
await page.getByTestId('member-roster').waitFor({ state: 'visible', timeout: 15000 })
await page.waitForTimeout(600)
const columnCount = (await page.getByTestId('member-count').innerText()).trim()
await page.getByTestId('crewmate-switcher').click()
await page.waitForSelector('[data-testid="crewmate-switcher-list"]')
await page.waitForTimeout(500)
console.log(`column says "${columnCount}", chip says "${await chipCount()}"`)
await page.screenshot({ path: join(OUT, '06-column-and-chip.png') })

// 07: the roster header's "+" menu. Team creation is hidden for the phase, so
// the menu's only door is New crewmate.
await page.keyboard.press('Escape')
await page.waitForTimeout(300)
await page.getByTestId('member-add').click({ button: 'left' })
const addMenu = await page.waitForSelector('[data-testid="member-add-menu"]')
await page.waitForTimeout(400)
const items = await page.locator('[data-testid="member-add-menu"] [role="menuitem"]').evaluateAll((els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()))
console.log(`"+" menu offers ${items.length}: ${items.join(', ')}`)
check('the "+" menu offers New crewmate alone', items.length === 1 && (await page.getByTestId('member-add-team').count()) === 0)
await addMenu.screenshot({ path: join(OUT, '07-add-menu.png') })
await page.screenshot({ path: join(OUT, '08-page-add-menu.png') })

console.log(`\nshots in ${OUT}`)
await context.close()
await browser.close()
srv.close()
process.exit(failed ? 1 : 0)
