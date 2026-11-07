/**
 * Screenshot harness: a crew whose workers wait on PR CI inside their own loop
 * reads Running, not Waiting, on the Sessions row and the Crew panel.
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures. Scene `before` sends what main's gateway sends (no
 * `looping`, the idle workers grouped `waiting`); scene `after` sends `looping`
 * and the server's new `working` group. The old page ignores `looping`, so
 * `before` on this build draws what main draws.
 *
 * Usage (in website/, after `npm run build`):
 *   node scripts/capture-looping-reads-running.mjs [outDir] [before|after]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/looping-reads-running')
const SCENE = process.argv[3] === 'before' ? 'before' : 'after'
const LOOPING = SCENE === 'after'
mkdirSync(OUT, { recursive: true })

const ROOT = 'chat-root'
const T0 = '2026-11-06T20:00:00Z'
const ago = (min) => new Date(Date.now() - min * 60000).toISOString()

const crewWork = {
  open: 2, person: 0, person_workers: [], conductor_looping: false,
  items: [
    { worker: 'chat-w1', stale_at_ms: 0, ...(LOOPING ? { looping: true } : {}) },
    { worker: 'chat-w2', stale_at_ms: 0, ...(LOOPING ? { looping: true } : {}) },
  ],
}

const slot = (key, title, over) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew-worker', agent_kind: 'template', mode: '', origin: 'agent',
  memory_mode: 'persistent', created: T0, last_ts: ago(3), last_turn_ts: ago(3), folder_id: '', workspace: 'default', ...over,
})

const slots = [
  slot(ROOT, 'Ship the login fix', { agent: 'kirocrew-conductor', origin: 'user', crew_mode_root: true, crew_work: crewWork }),
  slot('chat-w1', 'Fix login redirect', { crew_root: ROOT, created_by: ROOT, last_message: 'Waiting on PR #31770 CI; monitor armed.' }),
  slot('chat-w2', 'Add the retry test', { crew_root: ROOT, created_by: ROOT, last_message: 'Waiting on PR #31857 CI; monitor armed.' }),
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

const item = (id, title, worker, pr) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'pr_checks', pr }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', reason: null, summary: 'Monitor armed on the PR, ending turn.', artifacts: {}, pr,
  last_report_at: ago(6), created_at: T0, closed_at: null, decided_at: null, orphaned: false, stale: false,
  acceptance_concrete: true, report_undelivered: null, outstanding: false, terminal: false, alive: 'idle', events: [],
  ...(LOOPING ? { looping: true } : {}),
})

const BOARD = {
  conductor: { schema: 1, slot_key: ROOT, goal: 'Ship the login fix.', round: 1, depth: 0, parent_item: null, created_at: T0 },
  conductor_alive: 'idle',
  take_over_available: false,
  items: [item('it_login001', 'Fix login redirect', 'chat-w1', 31770), item('it_retry001', 'Add the retry test', 'chat-w2', 31857)],
}

const worker = (key, title, ledger) => ({
  key, title, agent: 'kirocrew-worker', status: 'idle', group: LOOPING ? 'working' : 'waiting', last_activity: ago(3),
  ledger_item: ledger, summary: title, created_by: ROOT, dispatched_by: ROOT,
})

// One fixture per stubbed route.
const ROUTES = {
  '/api/agents/catalog': CATALOG,
  '/api/crew-board': BOARD,
  [`/api/chat/slots/${ROOT}/crew-workers`]: {
    root: ROOT, workers: [worker('chat-w1', 'Fix login redirect', 'it_login001'), worker('chat-w2', 'Add the retry test', 'it_retry001')],
  },
  [`/api/chat/slots/${ROOT}/crew-timeline`]: { root: ROOT, lanes: [], gaps: [] },
  [`/api/chat/slots/${ROOT}`]: {
    messages: [
      { role: 'user', content: 'Ship the login fix with a crew.', ts: ago(60), meta: { mid: 'r-1' } },
      { role: 'assistant', content: 'Two crew members are on it.', ts: ago(59), meta: { mid: 'r-2' } },
    ],
    has_more: false, total: 2,
  },
  '/api/sessions': { sessions: [], has_more: false },
  '/api/chat/pins': { pins: [] },
}

const extra = async (path, route) => {
  if (!(path in ROUTES)) return false
  await json(route, ROUTES[path])
  return true
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
    await page.getByTestId('ultra-status').waitFor({ timeout: 20000 })
    await page.locator('[data-testid="ultra-milestones"] > ol [data-testid="ultra-milestone"]').first().waitFor({ timeout: 20000 })
    await page.waitForTimeout(1200)
    const states = await page.getByTestId('ultra-milestone').evaluateAll(els => els.map(e => e.getAttribute('data-state')))
    const word = await page.getByTestId('crew-root-state').first().getAttribute('data-state').catch(() => '')
    console.log(SCENE, theme, 'milestones:', states.join(','), 'row word:', word)
    await page.getByTestId('ultra-crew-panel').screenshot({ path: `${OUT}/${SCENE}-panel-${theme}.png` })
    const row = page.locator(`[data-slot-key="${ROOT}"]`).first()
    const target = (await row.count()) ? row : page.locator('aside').first()
    await target.screenshot({ path: `${OUT}/${SCENE}-row-${theme}.png` })
    // The Workers tab: the looping idle workers' group.
    await page.getByTestId('ultra-crew-panel').getByRole('radio', { name: 'Workers' }).click()
    await page.waitForTimeout(800)
    await page.getByTestId('ultra-crew-panel').screenshot({ path: `${OUT}/${SCENE}-workers-${theme}.png` })
    await context.close()
  }
  // The goal-board dashboard page, rendered the way its test mounts it: the
  // template's body, the host's fields, then its script.
  const raw = readFileSync(resolve('../src/kiro_crew/dashboard_templates/builtin/goal-board/template.html'), 'utf-8')
  const page = await browser.newPage({ viewport: { width: 760, height: 420 }, deviceScaleFactor: 2 })
  const items = BOARD.items.map(i => ({ item_id: i.item_id, title: i.title, state: 'open', status: 'progress', summary: i.summary, pr: i.pr, reason: null, worker: 'idle', stale: false, ...(LOOPING ? { looping: true } : {}) }))
  await page.evaluate(({ fields, raw }) => {
    // Parsed as the page test does: take the template's script out, keep the rest.
    const doc = new DOMParser().parseFromString(raw, 'text/html')
    const script = doc.querySelector('script')
    const code = script?.textContent ?? ''
    script?.remove()
    document.head.replaceChildren(...Array.from(doc.head.childNodes))
    document.body.replaceChildren(...Array.from(doc.body.childNodes))
    window.kirocrew = { fields, agentic: [], locale: 'en', missing: [], written_at: {} }
    for (const node of document.querySelectorAll('[data-dashboard-field]')) {
      const value = fields[node.getAttribute('data-dashboard-field') ?? '']
      if (value !== undefined) node.textContent = typeof value === 'string' ? value : JSON.stringify(value)
    }
    new Function(code)()
  }, { fields: { items }, raw })
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${SCENE}-goal-board.png`, fullPage: true })
  await page.close()
} finally {
  await browser.close()
  srv.close()
}
