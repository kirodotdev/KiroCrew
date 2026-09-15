/**
 * Screenshot harness + geometry guard for the EXPANDED WORKFLOW BAR's height.
 *
 * WorkflowProgressBar sits in the band between the virtualized transcript and
 * the composer. Expanding a run renders the full phase tree + result block + a
 * "View source" panel — routinely taller than the viewport. The band is not a
 * shrinkable flex item, so before the fix that growth pushed the composer down
 * until the chat column's `overflow-hidden` clipped it out of view entirely:
 * the user could no longer type.
 *
 * This ASSERTS as well as photographs. jsdom (where the unit test lives) has no
 * layout engine, so the real proof has to happen in a real browser: the harness
 * measures the composer's bounding box against the viewport and exits non-zero
 * if the input box is not fully on screen while a run is expanded.
 *
 * BEFORE / AFTER
 *   Each arm needs its own `npm run build`, because the fix is compiled into the
 *   bundle. Capture the pre-fix arm by checking the component out at a ref that
 *   predates the fix (or `git stash` the working change), rebuilding, and running
 *   this with a `before` label; then restore, rebuild, and run it with `after`.
 *   The pre-fix arm reports `composerVisible: false` with the input box sitting
 *   several hundred px BELOW the viewport bottom.
 *
 * Nothing in CI runs this file — treat it as a manual guard. The CI-enforced
 * half of the invariant is src/test/WorkflowProgressBar.heightBound.test.tsx.
 *
 * Usage: node scripts/capture-workflow-bar-height.mjs <before|after> [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const ARM = process.argv[2] === 'before' ? 'before' : 'after'
const OUT = process.argv[3] || '../temp-screenshots/workflow-bar-height'
const SLOT = 'chat-1'
const RUN_ID = 'wf_000025'
const SESSION_KEY = `dashboard:${SLOT}`
const VIEWPORT = { width: 1500, height: 950 }

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Kiro Crew performance investigation',
  running: true,
  last_message: 'Started workflow run wf_000025…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '/home/user/workspace/KiroCrew',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 2,
  queue: [],
  project: '/home/user/workspace/KiroCrew',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 900, content: 'Run a perf investigation as a workflow.' },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 60,
      content: 'Launched the investigation as a dynamic workflow — five investigators, five critics, then a ranking pass.',
    },
  ],
}

/** Five phases × five agents — the real shape of the run in the bug report. */
const AREAS = [
  'Gateway request path',
  'Startup / boot path',
  'Memory footprint',
  'History / search / persistence',
  'Frontend runtime + bundle',
]

/** A snapshot whose expanded body is comfortably taller than the viewport. */
function buildEvents() {
  const events = []
  let seq = 0
  const push = (type, data) => {
    events.push({ run_id: RUN_ID, seq: ++seq, ts: new Date().toISOString(), type, data })
  }
  push('run_started', { name: 'kirocrew-perf-investigation' })
  for (const [phase, agents] of [
    ['investigate', AREAS.map((a, i) => [`invest:a${i}`, `invest:a${i}:${a}`])],
    ['critique', AREAS.map((a, i) => [`critic:a${i}`, `critic:a${i}:${a}`])],
    ['synthesize', [['synth:a0', 'synthesize:ranked-defect-table']]],
  ]) {
    push('phase_started', { title: phase })
    for (const [id, label] of agents) {
      push('agent_started', { agent_id: id, label, phase })
      push('agent_finished', { agent_id: id, ok: true })
    }
  }
  for (const line of [
    'Authoring workflow from your request…',
    'Drafting the workflow script (attempt 1/3)…',
    'Script validated: kirocrew-perf-investigation',
    'Workflow authored — starting execution.',
    'Starting Kiro Crew performance investigation',
  ]) push('log', { message: line })
  return events
}

const SOURCE = Array.from({ length: 295 }, (_, i) => `# line ${i + 1} of the authored workflow script`).join('\n')

const snapshot = {
  run_id: RUN_ID,
  name: 'kirocrew-perf-investigation',
  status: 'running',
  events: buildEvents(),
  source: SOURCE,
  error: null,
  result: {
    status: 'ok',
    ranked_defects: [
      {
        proposed_fix: 'Lift the WS counts cache onto DashboardState; have the SSE loop and /api/status read from it.',
        effort: 'S',
        impact: 'High',
        impact_x_cost_rationale:
          'Highest-frequency gateway path (every 5s × every open tab) doing blocking file I/O and a lock-held DB scan on the loop.',
      },
    ],
  },
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })

  const extra = async (path, route) => {
    if (path === `/api/workflows/runs/${RUN_ID}`) { await json(route, snapshot); return true }
    if (path === '/api/workflows/runs') { await json(route, { runs: [] }); return true }
    if (path === '/api/tips/status') { await json(route, { enabled: false, cadence_hours: 24 }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  const page = await context.newPage()
  logPageProblems(page)
  let wsServer = null
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  // AFTER the shared stub so this wins: the stub swallows /api/ws to stop a
  // retry storm, but this harness needs the socket to push the run events.
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
  await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  if (!wsServer) throw new Error('websocket route never bound — the bar cannot appear')
  // The bar is fed ONLY by workflow_run_event frames (chat.workflowRuns), so the
  // run has to arrive over the socket exactly as the gateway broadcasts it.
  for (const type of ['run_started', 'phase_started', 'log']) {
    const src = snapshot.events.find(e => e.type === type)
    wsServer.send(JSON.stringify({
      type: 'workflow_run_event',
      data: { ...src, session_key: SESSION_KEY },
    }))
  }
  await page.waitForTimeout(1200)

  /**
   * The band. Pre-fix code carries no test id, so it is selected structurally —
   * and NOT with a bare `.animate-slide-up`, because the page holds more than
   * one band with that class (the queue/subagent strips animate in too, and the
   * first match is not this bar).
   */
  const BAND_CSS = '.animate-slide-up.rounded-md.bg-accent\\/10'

  async function measure() {
    return page.evaluate(sel => {
      const band = document.querySelector(sel)
      const boxEl =
        document.querySelector('[data-testid="input-wrapper"]')
        || document.querySelector('textarea')
      const b = band?.getBoundingClientRect()
      const c = boxEl?.getBoundingClientRect()
      return {
        found: !!band && !!boxEl,
        bandTop: b ? Math.round(b.top) : null,
        bandHeight: b ? Math.round(b.height) : null,
        bandBottom: b ? Math.round(b.bottom) : null,
        bandScrolls: band ? band.scrollHeight > band.clientHeight + 1 : null,
        bandContentHeight: band ? band.scrollHeight : null,
        bandClass: band ? band.className : null,
        composerTop: c ? Math.round(c.top) : null,
        composerBottom: c ? Math.round(c.bottom) : null,
        viewportH: window.innerHeight,
        // The whole point: is the input box still on screen?
        composerVisible: !!c && c.top >= 0 && c.bottom <= window.innerHeight + 1,
      }
    }, BAND_CSS)
  }

  const collapsed = await measure()
  if (!collapsed.found) throw new Error('workflow bar or composer never rendered')
  await page.screenshot({ path: `${OUT}/${ARM}-01-collapsed.png` })

  // Expand the run row. The click MUST be scoped to the band: the sidebar
  // renders its own row for the same run (WorkflowSidebarRow), it comes first in
  // DOM order, and clicking that one expands the sidebar while leaving this arm
  // silently measuring a still-collapsed bar — which reads as a passing "before".
  //
  // The run row is the band's FIRST button. Do not identify it as "the button
  // with aria-expanded=false": the expanded body brings its own disclosures (the
  // View source panel, each phase header), so that predicate never empties and
  // is useless as a did-it-open check.
  const runRow = page.locator(BAND_CSS).locator('button').first()
  await runRow.click()
  await page.waitForTimeout(1500)
  if ((await runRow.getAttribute('aria-expanded')) !== 'true') {
    throw new Error('the run row did not expand — nothing to measure')
  }

  // Reproduce the REPORTED state: the bug report's screenshot has the source
  // panel open, and that 295-line editor is a large part of what makes the body
  // outgrow the viewport. Collapsed source would understate the defect.
  const viewSource = page.locator(BAND_CSS).getByRole('button', { name: /view source/i }).first()
  if (await viewSource.count()) {
    await viewSource.click()
    await page.waitForTimeout(800)
  }
  const expanded = await measure()
  await page.screenshot({ path: `${OUT}/${ARM}-02-expanded.png` })

  const report = { arm: ARM, viewport: VIEWPORT, collapsed, expanded }
  writeFileSync(`${OUT}/${ARM}-report.json`, JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))

  await browser.close()
  srv.close()

  const ok = collapsed.composerVisible && expanded.composerVisible
  console.log(
    ok
      ? `PASS (${ARM}): composer stays fully on screen with the run expanded`
      : `FAIL (${ARM}): composer left the viewport — bottom ${expanded.composerBottom} vs viewport ${expanded.viewportH}`,
  )
  if (!ok) process.exitCode = 1
}

main().catch(e => {
  console.error(e)
  process.exit(2)
})
