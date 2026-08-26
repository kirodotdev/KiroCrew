/**
 * Browser guard for the STUCK WORKFLOW ROW above the composer.
 *
 * `workflow_run_event` is a one-shot broadcast with no replay, so a tab that was
 * closed, asleep, or disconnected when a run ended keeps its row spinning at
 * `running` forever: the spinner is driven purely by stored status, and the
 * terminal-linger cleanup only arms for a row that HAS reached a terminal status.
 * The fix consults `/api/workflows/runs` — the authority — and merges monotonically.
 *
 * Both of the paths that need a real browser are exercised here without pushing a
 * single WebSocket frame, which is the point: the run enters the chat purely
 * because the authority reported it, and it leaves purely because the authority
 * said it ended.
 *
 *   1. SEED — the first read reports the run `running`, so the row appears even
 *      though this tab never saw a `run_started` frame (the reload / late-join
 *      case; nothing else seeds that slice).
 *   2. HEAL — a later read reports the run `finished`, and the slow heal tick
 *      must stop the spinner with no reconnect and no frame involved. Fake timers
 *      in jsdom cannot prove that this fires in a real browser.
 *
 * The `stuck` arm answers 503 (workflows service unavailable) to every read after
 * the seed: the authority cannot be read, so the merge must change nothing and the
 * row must keep spinning. That is both the fail-closed requirement and a faithful
 * photograph of the pre-fix behaviour.
 *
 * Nothing in CI runs this file — the CI-enforced halves are
 * src/test/workflowRunReconcile.test.ts and
 * src/test/useWebSocket.workflowRunSync.test.ts.
 *
 * Usage: node scripts/capture-workflow-stuck-row.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/workflow-stuck-row'
const SLOT = 'chat-wf'
const RUN_ID = 'wf_000025'
const RUN_NAME = 'Kiro Crew perf investigation'
/** Longer than the 15s heal interval, with room for the round-trip. */
const HEAL_WAIT_MS = 20_000

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Run a perf investigation',
  running: false,
  last_message: 'Launched the workflow.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 900, content: 'Run a perf investigation over the repo.' },
    { role: 'assistant', ts: Date.now() / 1000 - 880, content: 'Launched the workflow — three hours of CPU have accumulated.' },
  ],
}

/** The compact row the backend returns, in each of the two states that matter. */
const row = status => ({
  run_id: RUN_ID,
  name: RUN_NAME,
  status,
  error: null,
  session_key: `dashboard:${SLOT}`,
  phase: 'synthesize',
  last_log: 'Starting Kiro Crew performance investigation',
})

/**
 * The band, selected structurally: this component carries no test id on main, and
 * a BARE `.animate-slide-up` matches the wrong band (the queue / sub-agent strips
 * animate in too, and the first match is not this bar). The spinner is scoped
 * INSIDE it for the same reason — the page holds other spinners at all times.
 */
const BAND_CSS = '.animate-slide-up.rounded-md.bg-accent\\/10'
const SPINNER = `${BAND_CSS} .animate-spin`

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const results = {}

  /** @param {'stuck'|'healed'} arm */
  async function run(arm) {
    const page = await context.newPage()
    logPageProblems(page)
    let reads = 0

    const extra = async (path, route) => {
      if (path === '/api/workflows/runs') {
        reads += 1
        console.log(`  [${arm}] authority read #${reads}`)
        // Read 1 seeds the row as live. After that the two arms diverge: one
        // cannot be read at all, the other reports the run over.
        if (reads === 1) await json(route, { runs: [row('running')] })
        else if (arm === 'stuck') await json(route, { error: 'workflows not available' }, 503)
        else await json(route, { runs: [row('finished')] })
        return true
      }
      if (path.startsWith('/api/workflows/runs/')) { await json(route, { ...row('running'), events: [], source: '' }); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    }

    await stubDashboardApi(page, { slots, extra })
    // AFTER the shared stub so this wins: the stub swallows /api/ws to stop a
    // retry storm, but the socket must actually OPEN — the reconcile rides that
    // handshake. Nothing is ever pushed over it: every state change in this
    // harness comes from the authority, which is the whole point.
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3000)

    const bar = page.locator(BAND_CSS)
    const spinning = async () => await page.locator(SPINNER).count() > 0

    // The row exists ONLY because the authority reported it — this is the seed
    // half of the fix, and a pre-fix build shows nothing here at all.
    if (!(await spinning())) {
      const dbg = await page.evaluate(name => {
        const hit = [...document.querySelectorAll('*')].reverse().find(
          el => el.children.length === 0 && (el.textContent || '').includes(name),
        )
        const trail = []
        for (let el = hit; el && trail.length < 8; el = el.parentElement) {
          trail.push(`${el.tagName.toLowerCase()}${el.getAttribute('data-testid') ? `#${el.getAttribute('data-testid')}` : ''}.${(el.className || '').toString().slice(0, 40)}`)
        }
        return {
          bands: document.querySelectorAll('.animate-slide-up.rounded-md').length,
          composer: !!document.querySelector('[data-testid="input-wrapper"]'),
          activeSlotLs: localStorage.getItem('mc-active-slot'),
          renderedBy: trail,
        }
      }, RUN_NAME)
      throw new Error(`[${arm}] the authority's running row never rendered — ${JSON.stringify(dbg, null, 1)}`)
    }
    await shoot(page, bar, `${arm}-before`)

    await page.waitForTimeout(HEAL_WAIT_MS)

    results[arm] = {
      reads,
      spinning: await spinning(),
      rowPresent: await page.locator(BAND_CSS).count() > 0,
    }
    await shoot(page, bar, `${arm}-after`)
    console.log(arm, JSON.stringify(results[arm]))
    await page.close()
  }

  /** Crop the band, or the region above the composer once the row is gone. */
  async function shoot(page, bar, name) {
    const box = await bar.first().boundingBox().catch(() => null)
    const composer = await page.getByTestId('input-wrapper').first().boundingBox().catch(() => null)
    const clip = box
      ? { x: Math.max(0, box.x - 12), y: Math.max(0, box.y - 12), width: Math.min(1480, box.width + 24), height: box.height + 24 }
      : composer
        ? { x: Math.max(0, composer.x - 12), y: Math.max(0, composer.y - 120), width: Math.min(1480, composer.width + 24), height: 200 }
        : { x: 250, y: 700, width: 1000, height: 220 }
    await page.screenshot({ path: `${OUT}/${name}.png`, clip })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  let failure = null
  try {
    await run('stuck')
    await run('healed')

    if (results.stuck.reads < 2) failure = 'FAIL: the heal tick never re-read the authority'
    // Unreadable authority: nothing may change, so the row keeps spinning.
    else if (!results.stuck.spinning) failure = 'FAIL: an unreadable authority cleared the row (must fail closed)'
    // Authority says finished: the spinner must be gone — the row shows ✓ and is
    // then dropped by the linger cleanup.
    else if (results.healed.spinning) failure = 'FAIL: row still spinning after the authority reported the run finished'
    else console.log('PASS: seeded from the authority; healed arm stops spinning, stuck arm keeps spinning')
  } finally {
    await browser.close()
    srv.close()
  }
  if (failure) { console.error(failure); process.exitCode = 1 }
}

main().catch(err => { console.error(err); process.exit(1) })
