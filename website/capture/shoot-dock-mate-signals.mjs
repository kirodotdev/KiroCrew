/**
 * Gateway-free capture of a starred mate's dock row carrying the same live
 * signals as its Mates page row: a mate with an unread reply and a mate whose
 * turn waits on the user each mark their dock row, on the wide and the narrow
 * dock and on the phone rail.
 *
 * Usage: node capture/shoot-dock-mate-signals.mjs <out-dir>   (after `npm run build`)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { stubDashboardApi, json } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2]
mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

const mate = (name, display_name, extra = {}) => ({
  name, slug: name, display_name, slot_key: `chat-mate-${name}`, running: false,
  last_active_ts: now - 600, last_message: '', kiro_agent: 'kirocrew',
  workspace: 'default', memory_store: name, memory_version: 2, memory_owner: name,
  model: '', avatar: {}, source: 'kirocrew', starred: true, dashboard_created: true,
  has_dm_message: true, has_goal: true, projections: { asOfSeq: -1, values: {} }, ...extra,
})
const MEMBERS = [
  mate('unflaky', 'Unflaky', { running: true, last_message: 'Re-running the flaky shard…' }),
  mate('triage', 'Issue Triage', { last_message: 'Labeled 6 new issues.' }),
  mate('docs', 'Docs Keeper', { last_message: 'Which branch should I update?' }),
].map((m, i) => ({ ...m, star_order: i + 1 }))
const memberSlot = (m, extra = {}) => ({
  key: m.slot_key, title: m.display_name, mode: 'member', agent: m.name, running: false,
  last_message: m.last_message, messages: 2, memory_mode: 'persistent', project: 'default',
  modified: now - 60, source_links: [], source_links_total: 0, ...extra,
})
const SLOTS = [
  { key: 'chat-1-1', title: 'Fix the flaky shard', running: false, last_message: 'ok', messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: 'default', modified: now, source_links: [], source_links_total: 0 },
  memberSlot(MEMBERS[0], { running: true }),
  memberSlot(MEMBERS[1]),
  memberSlot(MEMBERS[2], { needs_input: true }),
]
// Issue Triage has a reply the person has not read.
const UNREAD = { [MEMBERS[1].slot_key]: '' }
const crew = m => ({ name: m.name, display_name: m.display_name, scope: 'global', kiro_agent: 'kirocrew', workspace: 'default', memory_store: m.name, model: '', reasoning_effort: '', description: '', triggers: '', source: 'kirocrew', session_color: '', avatar: {}, starred: true, paused: false, last_active_ts: 0 })

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

async function scene(file, { view = { width: 1280, height: 760 }, collapsed = false, url = '/schedule', theme = 'dark', act } = {}) {
  const EXTRA = async (path, route) => {
    if (path === '/api/members') return json(route, { members: MEMBERS }), true
    if (path === '/api/teams') return json(route, { teams: [] }), true
    if (path.endsWith('/panel')) return json(route, { error: 'none' }, 404), true
    if (path.startsWith('/api/members/')) return json(route, {}), true
    if (path === '/api/agents' || path === '/api/agents/catalog') {
      return json(route, { agents: [crew({ name: 'kirocrew', display_name: '' }), ...MEMBERS.map(crew)], default_agent: 'kirocrew' }), true
    }
    if (path === '/api/crons') return json(route, { jobs: [] }), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
    if (path === '/api/apps') return json(route, []), true
    return false
  }
  const phone = view.width < 600
  const context = await browser.newContext({ viewport: view, hasTouch: phone, isMobile: phone })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    extra: EXTRA, slots: SLOTS, theme,
    localStorageEntries: { 'mc-nav': collapsed ? '1' : '0', 'mc-unread-shared': JSON.stringify(UNREAD) },
  })
  await page.goto(base + url)
  if (act) await act(page)
  else await page.locator('[data-testid^=dock-mate-]').first().waitFor({ state: 'attached', timeout: 20000 })
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/${file}` })
  await context.close()
}

const openRoster = async p => {
  await p.getByTestId('crewmate-switcher').waitFor({ timeout: 20000 })
  await p.getByTestId('crewmate-switcher').click(); await p.waitForTimeout(300)
  await p.getByText('Show the full roster').click(); await p.waitForTimeout(600)
}
const openMenu = async p => { await p.getByRole('button', { name: 'Open menu' }).first().click(); await p.waitForTimeout(900) }

// 1. The Mates page: Issue Triage carries the unread dot, Docs Keeper "Needs you".
await scene('1-mates-page.png', { url: '/members?member=unflaky', act: openRoster })
// 2. The same three mates in the dock, seen from Schedule.
await scene('2-dock.png')
// 3. Narrow dock.
await scene('3-dock-collapsed.png', { collapsed: true })
// 4. Light theme.
await scene('4-dock-light.png', { theme: 'light' })
// 5. Phone menu.
await scene('5-phone-menu.png', { view: { width: 390, height: 844 }, act: openMenu })

await browser.close()
srv.close()
console.log('done')
