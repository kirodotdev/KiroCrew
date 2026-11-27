/**
 * Gateway-free capture of the nav rails' single scroll region (issue #34398):
 * on the desktop rail everything above the bottom block (menu row, primary
 * rows, starred Bots, Apps list) scrolls as one, and Capabilities and Settings
 * stay pinned; the phone chat drawer's rail scrolls everything above Settings
 * and Search the same way.
 *
 * Usage: node capture/shoot-nav-rail-single-scroll.mjs <out-dir>   (after `npm run build`)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { stubDashboardApi, json } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2]
mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

const mate = (name, display_name, i) => ({
  name, slug: name, display_name, slot_key: '', running: false,
  last_active_ts: now - 600 * (i + 1), last_message: '', kiro_agent: 'kirocrew',
  workspace: 'default', memory_store: 'default', memory_version: 2, memory_owner: name,
  model: '', avatar: {}, source: 'kirocrew', starred: true, star_order: i + 1, dashboard_created: true,
  has_dm_message: true, has_goal: true, projections: { asOfSeq: -1, values: {} },
})
const MATES = ['Unflaky', 'Issue Triage', 'Docs Keeper', 'Release Captain', 'Security Watch', 'Perf Guard']
  .map((n, i) => mate(n.toLowerCase().replace(/ /g, '-'), n, i))
const APPS = ['Ledger Lens', 'Task Runner', 'Code Review', 'Notebook', 'Meetings', 'Radar', 'Fleet', 'Sage'].map((label, i) => {
  const name = 'demo-app-' + i
  return { name, version: '1.0.0', displayName: label, enabled: true, origin: 'local',
    manifest: { name, version: '1.0.0', displayName: label, description: 'x', ui: { entry: 'index.mjs', pages: [{ route: '/apps/' + name, label }] } } }
})
const crew = m => ({ name: m.name, display_name: m.display_name, scope: 'global', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', reasoning_effort: '', description: '', triggers: '', source: 'kirocrew', session_color: '', avatar: {}, starred: true, paused: false, last_active_ts: 0 })
const SLOT = 'chat-1-1'
const SLOTS = [{ key: SLOT, title: 'Fix the flaky shard', running: false, last_message: 'ok', messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: 'default', modified: now, source_links: [], source_links_total: 0 }]

const EXTRA = async (path, route) => {
  if (path === '/api/members') return json(route, { members: MATES }), true
  if (path === '/api/teams') return json(route, { teams: [] }), true
  if (path.endsWith('/panel')) return json(route, { error: 'none' }, 404), true
  if (path.startsWith('/api/members/')) return json(route, {}), true
  if (path === '/api/agents' || path === '/api/agents/catalog') {
    return json(route, { agents: [crew({ name: 'kirocrew', display_name: '' }), ...MATES.map(crew)], default_agent: 'kirocrew' }), true
  }
  if (path === '/api/crons') return json(route, { jobs: [] }), true
  if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
  if (path === '/api/apps') return json(route, APPS), true
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

/** Scroll every scrolling box inside the rail to its end, and report how many there were. */
const scrollToEnd = (p, selector) => p.evaluate(sel => {
  const rail = document.querySelector(sel)
  const boxes = [rail, ...rail.querySelectorAll('div')].filter(d => getComputedStyle(d).overflowY === 'auto' && d.scrollHeight > d.clientHeight)
  for (const b of boxes) b.scrollTop = b.scrollHeight
  return boxes.length
}, selector)

async function scene(file, { width = 1280, height = 600, collapsed = false, theme = 'dark', session = false, bottom = false } = {}) {
  const phone = width < 600
  const context = await browser.newContext({ viewport: { width, height }, hasTouch: phone, isMobile: phone })
  const page = await context.newPage()
  await stubDashboardApi(page, { extra: EXTRA, slots: SLOTS, theme, localStorageEntries: {
    'mc-nav': collapsed ? '1' : '0', 'mc-dock-mates-expanded': '1', 'mc-apps-expanded': '1',
  } })
  let rail = 'nav[aria-label="Main navigation"]'
  if (session) {
    await page.goto(base + '/?sid=' + SLOT)
    await page.getByRole('button', { name: 'Toggle sessions' }).first().click()
    rail = '[data-testid=mobile-nav-rail]'
    await page.locator(rail).waitFor({ timeout: 20000 })
  } else {
    await page.goto(base + '/schedule')
    if (phone) await page.getByRole('button', { name: 'Open menu' }).first().click()
    await page.locator('[data-testid^=dock-mate-]').first().waitFor({ state: 'attached', timeout: 20000 })
  }
  await page.waitForTimeout(1500)
  if (bottom) console.log(file, 'scrolling boxes:', await scrollToEnd(page, rail))
  await page.mouse.move(width - 5, height - 5)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${file}` })
  await context.close()
}

await scene('desktop-top.png')
await scene('desktop-bottom.png', { bottom: true })
await scene('desktop-collapsed-bottom.png', { collapsed: true, bottom: true })
await scene('desktop-light-top.png', { theme: 'light' })
await scene('desktop-tall.png', { height: 1100 })
await scene('phone-menu-top.png', { width: 390, height: 667 })
await scene('phone-menu-bottom.png', { width: 390, height: 667, bottom: true })
for (const h of [667, 844]) {
  await scene(`phone-session-${h}-top.png`, { width: 390, height: h, session: true })
  await scene(`phone-session-${h}-bottom.png`, { width: 390, height: h, session: true, bottom: true })
}

await browser.close()
srv.close()
console.log('done')
