/**
 * Screenshot harness for the Ultra Mode Board's Status milestones: needs you,
 * working, waiting, stalled and done each read as their own row.
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures, opens a conductor chat, and answers
 * GET /api/crew-board with one item per state. Each frame checks the Status view
 * is on screen before it is written.
 *
 * Usage (in website/, after `npm run build`):
 *   node scripts/capture-board-milestone-states.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/board-milestone-states')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const ROOT = 'chat-root'
const T0 = '2026-11-01T20:00:00Z'
const ago = (min) => new Date(Date.now() - min * 60000).toISOString()

const slots = [
  { key: ROOT, title: 'Launch party', messages: 2, running: false, agent: 'kirocrew-conductor', agent_kind: 'template', crew_mode_root: true, mode: '', origin: 'user', memory_mode: 'persistent', created: T0, last_ts: ago(1), folder_id: '', workspace: 'default' },
]

const CATALOG = {
  agents: [
    { name: 'kirocrew', selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' },
    { name: 'kirocrew-conductor', selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' },
  ],
  default_agent: 'kirocrew',
  crew_mode_agent: 'kirocrew-conductor',
  conductor_agents: ['kirocrew-conductor'],
}

const item = (id, title, over) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'file', path: `/tmp/${id}.txt` }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', reason: null, summary: '', artifacts: {}, pr: null,
  last_report_at: ago(20), created_at: T0, closed_at: null, decided_at: null, orphaned: false, stale: false,
  acceptance_concrete: true, report_undelivered: null, outstanding: false, terminal: false, alive: 'idle', events: [], ...over,
})

const BOARD = {
  conductor: { schema: 1, slot_key: ROOT, goal: 'Plan the launch party.', round: 1, depth: 0, parent_item: null, created_at: T0 },
  conductor_alive: 'idle',
  take_over_available: false,
  items: [
    item('it_menu0001', 'Drinks menu', { status: 'done', state: 'accepted', terminal: true, closed_at: ago(30), summary: 'Menu saved.' }),
    item('it_shop0001', 'Supplies order', { status: 'blocked', reason: 'needs_human', summary: 'Sign in to the shop so I can place the order.' }),
    item('it_poem0001', 'Opening poem', { alive: 'running', summary: 'Writing the second verse.', last_report_at: ago(2) }),
    item('it_venu0001', 'Venue booking', { status: 'question', outstanding: true, summary: 'Which of the two halls should I book?' }),
    item('it_list0001', 'Guest list', { summary: 'First 40 names done.', last_report_at: ago(90) }),
    item('it_deco0001', 'Decorations', { alive: 'closed', summary: 'Half the shopping list. More soon.', last_report_at: ago(140) }),
  ],
}

const extra = async (path, route) => {
  if (path === '/api/agents/catalog') { await json(route, CATALOG); return true }
  if (path === '/api/crew-board') { await json(route, BOARD); return true }
  if (path === `/api/chat/slots/${ROOT}/crew-workers`) { await json(route, { root: ROOT, workers: [] }); return true }
  if (path === `/api/chat/slots/${ROOT}/crew-timeline`) { await json(route, { root: ROOT, lanes: [], gaps: [] }); return true }
  if (path === `/api/chat/slots/${ROOT}`) {
    await json(route, { messages: [
      { role: 'user', content: 'Plan the launch party with a crew.', ts: ago(60), meta: { mid: 'r-1' } },
      { role: 'assistant', content: 'Six crew members are on it.', ts: ago(59), meta: { mid: 'r-2' } },
    ], has_more: false, total: 2 })
    return true
  }
  if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
  if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
  return false
}

const { srv, base } = await serveDist()
const { LD_LIBRARY_PATH: _mise, ...env } = process.env
const browser = await chromium.launch({ env })
try {
  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, localStorageEntries: { 'mc-theme-mode': theme, 'mc-active-slot-chat': ROOT, 'mc-lang': 'en', 'mc-privacy-notice-v1': '1', 'mc-preview-crew-mode': '1' }, extra })
    await page.goto(`${base}/chat?sid=${ROOT}`, { waitUntil: 'domcontentloaded' })
    const status = page.getByTestId('ultra-status')
    await status.waitFor({ timeout: 20000 })
    await page.locator('[data-testid="ultra-milestones"] > ol [data-testid="ultra-milestone"]').first().waitFor({ timeout: 20000 })
    await page.waitForTimeout(800)
    const states = await page.getByTestId('ultra-milestone').evaluateAll(els => els.map(e => e.getAttribute('data-state')))
    console.log(theme, 'states:', states.join(','))
    await page.getByTestId('ultra-crew-panel').screenshot({ path: `${OUT}/${PREFIX}-${theme}.png` })
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
