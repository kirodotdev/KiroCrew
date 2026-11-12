/**
 * Gateway-free capture of the left dock's Mates group (issue #28789): a
 * starred mate is a dock row, the dock lists starred mates in `star_order`,
 * dragging a row writes the new order, and a failed write says so.
 *
 * Usage: node capture/shoot-mate-dock.mjs <out-dir>   (after `npm run build`)
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
  name, slug: name, display_name, slot_key: '', running: false,
  last_active_ts: now - 600, last_message: '', kiro_agent: 'kirocrew',
  workspace: 'default', memory_store: 'default', memory_version: 2, memory_owner: name,
  model: '', avatar: {}, source: 'kirocrew', starred: false, dashboard_created: true,
  has_dm_message: true, has_goal: true, projections: { asOfSeq: -1, values: {} }, ...extra,
})
const BASE = [
  mate('unflaky', 'Unflaky', { running: true, last_message: 'Re-running the flaky shard on PR #28461…', heartbeat_on: true }),
  mate('triage', 'Issue Triage', { last_message: 'Labeled 6 new issues.', last_active_ts: now - 3600 }),
  mate('docs', 'Docs Keeper', { last_message: 'Docs are in sync with main.', last_active_ts: now - 7200 }),
  mate('release', 'Release Captain', { last_message: 'rc6 is ready to cut.', last_active_ts: now - 86400 }),
  mate('security', 'Security Watch', { last_message: 'No new advisories.', last_active_ts: now - 90000 }),
  mate('perf', 'Perf Guard', { last_message: 'p95 is flat this week.', last_active_ts: now - 100000 }),
  mate('i18n', 'Translator', { last_message: '12 locales up to date.', last_active_ts: now - 120000 }),
]
const APPS = ['Ledger Lens','Task Runner','Code Review','Notebook','Meetings','Radar'].map((label, i) => {
  const name = 'demo-app-' + i
  return { name, version: '1.0.0', displayName: label, enabled: true, origin: 'local',
    manifest: { name, version: '1.0.0', displayName: label, description: 'x', ui: { entry: 'index.mjs', pages: [{ route: '/apps/' + name, label }] } } }
})
const SLOT = 'chat-1-1'
const SLOTS = [{ key: SLOT, title: 'Fix the flaky shard', running: false, last_message: 'ok', messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: 'default', modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0 }]
const crew = m => ({ name: m.name, display_name: m.display_name, scope: 'global', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', reasoning_effort: '', description: '', triggers: '', source: 'kirocrew', session_color: '', avatar: {}, starred: !!m.starred, paused: false, last_active_ts: 0 })

let VIEW = { width: 1280, height: 760 }
const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

async function scene(file, { starred = [], count = 4, collapsed = false, url = '/members', act, failPut = false, theme = 'dark', video = false, noApps = false, expanded = false, failMembers = false } = {}) {
  const members = BASE.slice(0, count).map(m => ({ ...m, starred: starred.includes(m.name), star_order: starred.indexOf(m.name) + 1 }))
  const writes = []
  const EXTRA = async (path, route) => {
    const req = route.request()
    if (path === '/api/members') return failMembers ? json(route, { error: 'roster unavailable' }, 500) : json(route, { members }), true
    if (path === '/api/teams') return json(route, { teams: [] }), true
    if (path.endsWith('/panel')) return json(route, { error: 'none' }, 404), true
    if (path.startsWith('/api/members/')) return json(route, {}), true
    if (path.startsWith('/api/agents/') && req.method() === 'PUT') {
      const name = decodeURIComponent(path.split('/').pop())
      const body = JSON.parse(req.postData() || '{}')
      writes.push({ name, ...body })
      if (failPut) return json(route, { error: 'config locked' }, 500), true
      const m = members.find(x => x.name === name)
      if (m && 'starred' in body) m.starred = body.starred
      if (m && 'star_order' in body) m.star_order = body.star_order
      if (m && !m.starred) m.star_order = 0
      return json(route, { ok: true }), true
    }
    if (path === '/api/agents' || path === '/api/agents/catalog') {
      return json(route, { agents: [crew({ name: 'kirocrew', display_name: '' }), ...members.map(crew)], default_agent: 'kirocrew' }), true
    }
    if (path === '/api/crons') return json(route, { jobs: [] }), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
    if (path === '/api/apps') return json(route, noApps ? [] : APPS), true
    return false
  }
  const context = await browser.newContext({ viewport: VIEW, hasTouch: VIEW.width < 600, isMobile: VIEW.width < 600, ...(video ? { recordVideo: { dir: OUT, size: { width: 1280, height: 760 } } } : {}) })
  const page = await context.newPage()
  await stubDashboardApi(page, { extra: EXTRA, slots: SLOTS, theme, localStorageEntries: { 'mc-nav': collapsed ? '1' : '0', 'mc-dock-mates-expanded': expanded ? '1' : '0' } })
  await page.goto(base + url)
  await page.locator(VIEW.width < 600 || !starred.length || failMembers ? 'body' : '[data-testid^=dock-mate-]').first().waitFor({ state: 'attached', timeout: 20000 })
  await page.waitForTimeout(800)
  if (act) await act(page)
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/${file}` })
  const recording = video ? page.video() : null
  await context.close()
  if (recording) await recording.saveAs(`${OUT}/${file.replace(/\.png$/, '.webm')}`)
  return writes
}

/** Mouse-drag one dock row onto another (MouseSensor arms after 8px). */
async function dragRow(p, fromSlug, toSlug) {
  const a = await p.getByTestId(`dock-mate-${fromSlug}`).boundingBox()
  const b = await p.getByTestId(`dock-mate-${toSlug}`).boundingBox()
  await p.mouse.move(a.x + a.width / 2, a.y + a.height / 2)
  await p.mouse.down()
  await p.mouse.move(a.x + a.width / 2, a.y + a.height / 2 - 12, { steps: 4 })
  await p.mouse.move(b.x + b.width / 2, b.y + 4, { steps: 12 })
  return { up: async () => { await p.mouse.up(); await p.mouse.move(900, 500) } }
}


const openRoster = async p => {
  await p.getByTestId('crewmate-switcher').click(); await p.waitForTimeout(300)
  await p.getByText('Show the full roster').click(); await p.waitForTimeout(600)
}
// 1. Full roster: hover a mate, the star shows.
await scene('1-star-in-roster.png', { url: '/members?member=unflaky', act: async p => {
  await openRoster(p); await p.getByTestId('member-star-triage').hover() } })
// 2. Click the star: the mate shows up under "Mates" in the dock.
await scene('2-starred-one.png', { url: '/members?member=unflaky', act: async p => {
  await openRoster(p); await p.getByTestId('member-star-triage').click(); await p.mouse.move(900, 500) } })
// 3. Three starred, seen from Schedule; the working mate carries the dot.
await scene('3-three-starred.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule' })
// 4. Click a starred mate in the dock: its chat opens and only its row lights.
await scene('4-open-from-dock.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule',
  act: async p => { await p.getByTestId('dock-mate-triage').click(); await p.waitForTimeout(1200) } })
// 5. Narrow dock: 16px faces like every other icon.
await scene('5-collapsed.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule', collapsed: true })
// 6. Seven starred: the first 3, then "4 more".
await scene('6-more.png', { count: 7, starred: BASE.map(m => m.name), url: '/schedule' })
// 6b. Same seven, unfolded with the toggle: every starred mate shows and can be dragged.
await scene('6b-expanded.png', { count: 7, starred: BASE.map(m => m.name), url: '/schedule', expanded: true })
// 6c. The roster fails to load: the notice says so beside the rail.
await scene('6c-load-failed.png', { url: '/schedule', failMembers: true,
  act: async p => { await p.getByTestId('dock-mates-load-error').waitFor({ timeout: 30000 }) } })
// 7. Mid-drag: Docs Keeper lifted above Unflaky.
await scene('7-dragging.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule',
  act: async p => { await dragRow(p, 'docs', 'unflaky'); await p.waitForTimeout(300) } })
// 8. Dropped: new order, and the writes it sent.
const dropWrites = await scene('8-dropped.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule',
  act: async p => { const d = await dragRow(p, 'docs', 'unflaky'); await p.waitForTimeout(200); await d.up(); await p.waitForTimeout(600) } })
console.log('drop writes:', JSON.stringify(dropWrites))
// 8b. The same drop, recorded: the row travels to its new place.
await scene('8b-drag.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule', video: true,
  act: async p => { await p.waitForTimeout(500); const d = await dragRow(p, 'docs', 'unflaky'); await p.waitForTimeout(500); await d.up(); await p.waitForTimeout(900) } })
// 9. The save failed: the order snaps back and the notice says so.
await scene('9-reorder-failed.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule', failPut: true,
  act: async p => { const d = await dragRow(p, 'docs', 'unflaky'); await p.waitForTimeout(200); await d.up(); await p.getByTestId('dock-mates-reorder-error').waitFor({ timeout: 5000 }) } })
// 10. Same failure on the narrow dock: the notice sits beside the rail.
await scene('10-reorder-failed-collapsed.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule', failPut: true, collapsed: true,
  act: async p => { const d = await dragRow(p, 'docs', 'unflaky'); await p.waitForTimeout(200); await d.up(); await p.getByTestId('dock-mates-reorder-error').waitFor({ timeout: 5000 }) } })
// 11. Light theme.
await scene('11-light.png', { starred: ['unflaky', 'triage', 'docs'], url: '/schedule', theme: 'light' })

// 12+. Layout on short windows and phones, with six apps installed: the
// Mates group must never push the apps or the rail's footer off screen.
const openSessions = async p => { await p.getByRole('button', { name: 'Toggle sessions' }).first().click(); await p.waitForTimeout(900) }
const openMenu = async p => { await p.getByRole('button', { name: 'Open menu' }).first().click(); await p.waitForTimeout(900) }
for (const h of [760, 600]) {
  VIEW = { width: 1280, height: h }
  await scene(`L-desktop-${h}-session.png`, { count: 7, starred: ['unflaky', 'triage', 'docs', 'release', 'security'], url: '/?sid=' + SLOT })
  await scene(`L-desktop-${h}-session-nomates.png`, { count: 7, starred: [], url: '/?sid=' + SLOT })
}
for (const h of [844, 667]) {
  VIEW = { width: 390, height: h }
  await scene(`L-phone-${h}-menu.png`, { count: 7, starred: ['unflaky', 'triage', 'docs', 'release', 'security'], url: '/schedule', act: openMenu })
  await scene(`L-phone-${h}-menu-nomates.png`, { count: 7, starred: [], url: '/schedule', act: openMenu })
  await scene(`L-phone-${h}-session-rail.png`, { count: 7, starred: ['unflaky', 'triage', 'docs', 'release', 'security'], url: '/?sid=' + SLOT, act: openSessions })
  await scene(`L-phone-${h}-session-rail-nomates.png`, { count: 7, starred: [], url: '/?sid=' + SLOT, act: openSessions })
}
VIEW = { width: 390, height: 844 }
await scene('L-phone-844-session-rail-scrolled.png', { count: 7, starred: ['unflaky', 'triage', 'docs', 'release', 'security'], url: '/?sid=' + SLOT,
  act: async p => { await openSessions(p); await p.getByTestId('rail-mate-unflaky').scrollIntoViewIfNeeded(); await p.waitForTimeout(300) } })

await browser.close()
srv.close()
console.log('done')
