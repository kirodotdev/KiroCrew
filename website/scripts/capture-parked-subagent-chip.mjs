/**
 * Screenshot harness + assertions for a sub-agent PARKED ON A SPAWN APPROVAL
 * (#7318).
 *
 * Both surfaces that publish a running count used to fold status 'pending' into
 * it, so a run that had launched no process at all was reported as running:
 *
 *   - SubagentProgressBar (the wave chip above the composer) counted the parked
 *     run behind the spinning loader and rendered its row as a bare task label
 *     with a ticking elapsed timer -- identical to an agent doing work;
 *   - SubagentRunCard (the inline launch card in scrollback) printed
 *     "2 agents running" for a wave with one member executing and one waiting on
 *     the user.
 *
 * This ASSERTS as well as photographs, because a PNG cannot fail: it drives the
 * REAL built SPA (website/dist), pushes the same WS frames the gateway sends, and
 * exits non-zero unless the chip reads 1 running + 1 awaiting, the parked row
 * names the approval, and the launch card says "1 agent running" beside an
 * awaiting chip. A build serving a stale bundle therefore reds instead of quietly
 * photographing the old copy.
 *
 * To photograph the BEFORE state for an A/B pair, check the two components out at
 * the commit that precedes the fix, rebuild, and run this with a different outDir:
 *
 *   git checkout <base> -- website/src/pages/chat/SubagentProgressBar.tsx \
 *                          website/src/pages/chat/SubagentRunCard.tsx
 *   npm run build && node scripts/capture-parked-subagent-chip.mjs ../temp-screenshots/before
 *   git checkout HEAD -- website/src/pages/chat/SubagentProgressBar.tsx \
 *                        website/src/pages/chat/SubagentRunCard.tsx
 *
 * The BEFORE run reports `running: '2'` with no awaiting count and exits 1 -- that
 * failure IS the regression being fixed, so pass --expect-before to invert the
 * verdict when capturing it deliberately.
 *
 * Usage: node scripts/capture-parked-subagent-chip.mjs [outDir] [--expect-before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2).filter(a => a !== '--expect-before')
const EXPECT_BEFORE = process.argv.includes('--expect-before')
const OUT = args[0] || '../temp-screenshots/parked-subagent-chip'
const SLOT = 'chat-parked'
const PROJECT = '/home/user/workspace/uploader'

/** Hex-shaped ids: extractSpawnRunLaunch only recognises the real id pattern. */
const RUNNING_ID = 'a1b2c3d4'
const PARKED_ID = 'e5f6a7b8'
const PARKED_TASK = 'draft the migration note for the pinned schema'

const LAUNCH_OUTPUT = [
  'Spawned 2 subagent(s). Results will arrive as completion events:',
  `  ${RUNNING_ID} (kirocrew): audit the retry ladder in the uploader`,
  `  ${PARKED_ID} (kirocrew): ${PARKED_TASK}`,
].join('\n')

const slots = [{
  key: SLOT,
  title: 'Audit the uploader retry ladder',
  running: true,
  last_message: 'Spawned 2 subagent(s).',
  messages: 3,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 3,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: Date.now() / 1000 - 600,
      content: 'Audit the uploader retry ladder and draft the migration note.',
    },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 60,
      content: 'Splitting this into two sub-agents.',
    },
    {
      // The wrench prefix is load-bearing: transcriptRenderers.tsx uses
      // `content.startsWith('\u{1F527}')` as its cheap reject before parsing the
      // launch, so a tool row without it never reaches SubagentRunCard.
      role: 'tool',
      ts: Date.now() / 1000 - 55,
      content: '\u{1F527} spawn_run',
      cls: '',
      meta: { tool_call_id: 'tc_spawn_7318', input: '{}', output: LAUNCH_OUTPUT },
    },
  ],
}

/** What `/api/spawn` reports: BOTH runs are registered and not done. The parked
 *  one is in `_agents` and counted by the manager -- that is the whole point. */
const spawnList = {
  agents: [
    { id: RUNNING_ID, done: false, parent: `dashboard:${SLOT}` },
    { id: PARKED_ID, done: false, parent: `dashboard:${SLOT}` },
  ],
}

async function main() {
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The chip is 11-12px type; 1x renders it soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (path === '/api/spawn') { await json(route, spawnList); return true }
    if (path === '/api/tips/status') { await json(route, { enabled: false }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  // AFTER the shared stub so this wins: the stub swallows /api/ws to stop a retry
  // storm, but this harness needs the socket to push the run frames.
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
  await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  if (!wsServer) throw new Error('websocket route never bound')

  const send = async (type, data, settle = 350) => {
    wsServer.send(JSON.stringify({ type, data }))
    await page.waitForTimeout(settle)
  }

  // 1. The member that actually started, with a tool so its row looks alive.
  await send('subagent_spawn', { slot: SLOT, id: RUNNING_ID, task: 'audit the retry ladder in the uploader', agent: 'kirocrew' })
  await send('subagent_tool', { slot: SLOT, id: RUNNING_ID, tool: 'Reading: src/uploader/retry.py', tool_count: 4 })
  // 2. The member parked on an unanswered spawn approval. Verbatim the frame the
  //    gateway broadcasts: an `approval` whose id is `spawn:<agent_id>`, which
  //    useWebSocket routes into sseSubagentPending.
  await send('approval', {
    id: `spawn:${PARKED_ID}`,
    slot: SLOT,
    tool: `spawn_run(${PARKED_TASK})`,
    source: 'agent',
    ts: Date.now() / 1000,
  }, 900)

  const text = async (testId) => {
    const el = page.getByTestId(testId).first()
    return (await el.count()) ? (await el.textContent() || '').trim() : null
  }

  const observed = {
    running: await text('subagent-running-count'),
    awaiting: await text('subagent-awaiting-count'),
    cardAwaiting: await text('subagent-card-awaiting'),
    parkedRow: await page.getByText('Waiting for your approval to start').first().count() > 0,
    cardRunning: await page.getByText('1 agent running').first().count() > 0,
    cardClaimsTwoRunning: await page.getByText('2 agents running').first().count() > 0,
  }

  const chip = page.getByTestId('subagent-histogram').locator('xpath=ancestor::div[contains(@class,"animate-slide-up")][1]')
  await page.screenshot({ path: `${OUT}/full.png` })
  if (await chip.count()) await chip.first().screenshot({ path: `${OUT}/wave-chip.png` })
  const card = page.getByTestId('subagent-run-card').first()
  if (await card.count()) await card.screenshot({ path: `${OUT}/launch-card.png` })

  await browser.close()
  srv.close()

  console.log(JSON.stringify(observed, null, 2))
  console.log(`screenshots -> ${OUT}`)

  const fixed = observed.running === '1'
    && observed.awaiting === '1'
    && observed.parkedRow
    && observed.cardRunning
    && observed.cardAwaiting === '1'
    && !observed.cardClaimsTwoRunning
  if (EXPECT_BEFORE) {
    // The pre-fix tree must exhibit the defect, not merely "not be fixed": a
    // blank page would otherwise pass as a convincing BEFORE frame.
    const broken = observed.running === '2' && observed.awaiting === null
      && observed.cardClaimsTwoRunning && observed.cardAwaiting === null
    if (!broken) { console.error('BEFORE frame did not reproduce the defect'); process.exit(1) }
    console.log('BEFORE: reproduced -- 2 running claimed, no awaiting count')
    return
  }
  if (!fixed) { console.error('AFTER frame does not show the fix'); process.exit(1) }
  console.log('AFTER: 1 running + 1 awaiting, parked row names the approval')
}

main().catch(e => { console.error(e); process.exit(1) })
