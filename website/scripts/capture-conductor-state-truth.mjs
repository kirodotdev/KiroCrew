/**
 * Screenshot harness for the conductor-state change: a Crew Mode root's row (and
 * its board lane) says Running / Waiting / Stalled / Needs you from what its crew
 * and its ledger actually do, not "Waiting" while a worker works.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). `DIST` selects the build, so
 * the same script frames the base build (before) and this branch (after).
 *
 * Four Crew Mode roots, one per state, each with an armed patrol loop (the default
 * work-ledger patrol) and an `[OPTIONS:]` courtesy line on its newest reply:
 *   working  -- its worker is mid-turn
 *   waiting  -- its worker parked a `progress` report a minute ago (ledger moving)
 *   stalled  -- its worker's report is 40 minutes old (ledger quiet)
 *   needs    -- an item names a person (`reason: needs_human`)
 *
 * Frames (2x): <prefix>-rows-<theme>.png (list view), <prefix>-board-<theme>.png
 * (state lanes).
 *
 * Usage (in website/): DIST=<dist> node scripts/capture-conductor-state-truth.mjs <outDir> <prefix>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/conductor-state-truth')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now()
const T0 = new Date(NOW - 5 * 60_000).toISOString()
const row = (key, title, extra = {}) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew', agent_kind: 'template', mode: '', origin: 'user',
  memory_mode: 'persistent', created: T0, last_ts: T0, folder_id: '', workspace: 'default', ...extra,
})
// stale_at_ms: when the ledger flags it (15 min after its last report).
const item = (worker, agoMs) => ({ worker, stale_at_ms: NOW - agoMs + 15 * 60_000 })
const root = (key, title, crewWork) => row(key, title, {
  agent: 'kirocrew-conductor', crew_mode_root: true, has_options: true,
  last_message: 'Patrol armed. Next check in 15 min.', crew_work: crewWork,
})
const worker = (key, rootKey, title, running) => row(key, title, {
  agent: 'kirocrew-worker', crew_root: rootKey, created_by: rootKey, running,
})
const ROOTS = {
  'chat-working': { title: 'Ship the sidebar fix', worker: 'chat-w1', running: true, work: { open: 1, person: 0, items: [item('chat-w1', 60_000)] } },
  'chat-waiting': { title: 'Rebase the docs PR', worker: 'chat-w2', running: false, work: { open: 1, person: 0, items: [item('chat-w2', 60_000)] } },
  'chat-stalled': { title: 'Bump the lockfile', worker: 'chat-w3', running: false, work: { open: 1, person: 0, items: [item('chat-w3', 40 * 60_000)] } },
  'chat-needs': { title: 'Rotate the deploy key', worker: 'chat-w4', running: false, work: { open: 1, person: 1, items: [] } },
}
const slots = []
for (const [key, r] of Object.entries(ROOTS)) {
  slots.push(root(key, r.title, r.work))
  slots.push(worker(r.worker, key, `${r.title} (worker)`, r.running))
}
const loop = slotKey => ({
  id: `loop-${slotKey}`, slot_key: slotKey, message: 'Patrol the work ledger.', active: true,
  cycle_count: 3, max_cycles: 96, idle_secs: 900, created_ts: NOW / 1000 - 3600, last_fire_ts: NOW / 1000 - 300,
})
const CATALOG = {
  agents: ['kirocrew', 'kirocrew-conductor', 'kirocrew-worker'].map(name => ({ name, selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' })),
  default_agent: 'kirocrew', crew_mode_agent: 'kirocrew-conductor', conductor_agents: ['kirocrew-conductor'],
}
const workersFor = key => {
  const r = ROOTS[key]
  return {
    root: key,
    workers: [{
      key: r.worker, title: `${r.title} (worker)`, agent: 'kirocrew-worker',
      status: r.running ? 'working' : 'idle', group: r.running ? 'working' : 'waiting',
      last_activity: T0, created_by: key, dispatched_by: key,
    }],
  }
}
const LANES = ['needs_approval', 'waiting', 'working', 'idle']
const laneColumns = LANES.map((key, i) => ({
  id: `lane-${key}`, name: '', tag_ids: [], mode: 'any', order: i, include_untagged: false, source: 'state', state_key: key,
}))

const extra = board => async (path, route) => {
  if (path === '/api/agents/catalog') { await json(route, CATALOG); return true }
  const m = path.match(/^\/api\/chat\/slots\/([^/]+)\/(crew-workers|crew-timeline)$/)
  if (m && ROOTS[m[1]]) {
    await json(route, m[2] === 'crew-workers' ? workersFor(m[1]) : { root: m[1], lanes: [], gaps: [] })
    return true
  }
  if (path.startsWith('/api/chat/slots/') && !path.slice('/api/chat/slots/'.length).includes('/')) {
    await json(route, { messages: [], has_more: false, total: 0 }); return true
  }
  if (path === '/api/autonudge') { await json(route, { loops: Object.keys(ROOTS).map(loop) }); return true }
  if (path === '/api/monitors') { await json(route, { enabled: true, monitors: [] }); return true }
  if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
  if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
  if (board && path === '/api/chat/tags') { await json(route, []); return true }
  if (board && path === '/api/chat/tag-columns') { await json(route, laneColumns); return true }
  return false
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const { LD_LIBRARY_PATH: _mise, ...env } = process.env
  const browser = await chromium.launch({ env })
  const report = {}
  for (const theme of ['light', 'dark']) {
    for (const board of [false, true]) {
      const context = await browser.newContext({ viewport: { width: board ? 1500 : 1280, height: 800 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        slots, theme, extra: extra(board),
        localStorageEntries: {
          'mc-lang': 'en', 'mc-preview-crew-mode': '1',
          ...(board ? { 'mc-chat-config': JSON.stringify({ tagColumnsEnabled: true }), 'mc-sidebar-width': '900' } : {}),
        },
      })
      await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
      await page.waitForSelector(board ? '[data-testid="column-strip"]' : '[data-crew-root="true"]', { timeout: 20_000 })
      await page.waitForTimeout(1500)
      if (board) {
        const lanes = {}
        for (const lane of LANES) {
          lanes[lane] = await page.evaluate(cid => {
            const col = document.querySelector(`[data-testid="column-${cid}"]`)
            return col ? Array.from(col.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key')) : null
          }, `lane-${lane}`)
        }
        report[`board-${theme}`] = lanes
        await page.screenshot({ path: `${OUT}/${PREFIX}-board-${theme}.png`, clip: { x: 0, y: 0, width: 960, height: 560 } })
      } else {
        const words = {}
        for (const key of Object.keys(ROOTS)) {
          const card = page.locator(`[data-slot-key="${key}"]`).first()
          words[key] = (await card.innerText()).replace(/\s+/g, ' ').trim()
        }
        report[`rows-${theme}`] = words
        const first = await page.locator('[data-crew-root="true"]').first().boundingBox()
        const width = first ? first.width + 24 : 340
        await page.screenshot({ path: `${OUT}/${PREFIX}-rows-${theme}.png`, clip: { x: 0, y: 0, width: Math.max(width, 340), height: 520 } })
      }
      await context.close()
    }
  }
  console.log(JSON.stringify(report, null, 2))
  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(2) })
