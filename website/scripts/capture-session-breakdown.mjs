/**
 * Screenshot + video harness for the Session Breakdown tree at the top of the
 * chat side-panel Context tab.
 *
 * Runs the REAL built SPA (website/dist) through the shared transcript harness:
 * a static server over dist, /api/** answered from fixtures, and /api/ws bound
 * so the app's socket does not hang. No gateway, no kiro-cli — only the network
 * is stubbed, so the tree, each node's own context-trace fetch, the occupancy
 * gauge and the expand animation run exactly as they do in production.
 *
 * The scene the tree exists to show:
 *   - Session Breakdown header (one node per sub-agent, spawn order)
 *   - three sub-agents pushed over the mocked /api/ws as snapshot+done frames:
 *       gpt-review    (completed) -> its OWN trace is HISTORY-dominant (~58%)
 *       opus-review   (completed) -> HISTORY-dominant (~62%), 2 turns
 *       verify-repro  (failed)    -> TOOL_OUTPUT-dominant (~26%)
 *   - the parent (slot chat-1) trace is SKILL-dominant (loaded_skill ~47%)
 *   - one node expanded so its per-turn composition is visible and visibly
 *     DIFFERENT from the parent's skill-heavy mix.
 *
 * Each node fetches GET /api/telemetry/context-trace?slot=<childSession>; this
 * harness registers its OWN route for that path BEFORE load() so a distinct
 * ContextTrace is returned per `slot` query param (the newest-registered route
 * wins over the harness catch-all).
 *
 * Usage:  node scripts/capture-session-breakdown.mjs [outDir]
 * Output: <outDir> || $KIROCREW_SCRATCH || os.tmpdir()/session-tree-cap/
 *   session-tree.png, session-tree.mp4
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const SLOT = 'chat-1'
const PROJECT = '/home/user/workspace/KiroCrew'
// Output dir: first CLI arg, else $KIROCREW_SCRATCH, else the OS temp dir —
// never a hardcoded personal path.
const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || tmpdir(), 'session-tree-cap')
mkdirSync(OUT, { recursive: true })

// ---------------------------------------------------------------------------
// ContextTrace fixtures — one per session key. Shapes match the ContextTrace
// interface in ContextBreakdownPanel.tsx EXACTLY (slot, turns[], totals,
// injected_chars, user_chars, estimated_other_chars, peak_context_used,
// context_window, window_days). Block sizes are CHARS; the composition is what
// the tree renders, so the per-key mixes are deliberately, visibly different.
// ---------------------------------------------------------------------------

/** A per-turn injection record. */
const turn = (blocks, phase = 'per_turn', model = 'auto') => {
  const total = Object.values(blocks).reduce((a, b) => a + b, 0)
  return {
    ts: '2026-08-27T03:00:00Z',
    phase,
    blocks,
    total_chars: total,
    // context_used is in TOKENS (~4 chars/token); only relative size matters here.
    context_used: Math.round(total / 4),
    context_window: 200_000,
    model,
  }
}

const sum = obj => Object.values(obj).reduce((a, b) => a + b, 0)

/** Build a ContextTrace from a list of per-turn block maps. `peakFrac` sets how
 *  full the window got (drives the occupancy gauge). */
function makeTrace(slot, turnBlocks, peakFrac) {
  const turns = turnBlocks.map(b => turn(b))
  const totals = {}
  for (const b of turnBlocks) for (const [k, v] of Object.entries(b)) totals[k] = (totals[k] ?? 0) + v
  const injected = sum(totals)
  const userChars = totals.your_message ?? 0
  const window = 200_000
  return {
    slot,
    turns,
    totals,
    injected_chars: injected,
    user_chars: userChars,
    // The model context Kiro Crew did NOT inject (kiro-cli prompt + tool catalog).
    estimated_other_chars: Math.round(injected * 0.35),
    peak_context_used: Math.round(window * peakFrac),
    context_window: window,
    window_days: 14,
  }
}

// Parent (chat-1): SKILL-dominant. loaded_skill ~47%, then memory/history/lessons/
// agent_instructions/tool_output/your_message. Gauge ~22%.
const PARENT = makeTrace(SLOT, [
  { loaded_skill: 23500, memory: 7000, history: 7000, lessons: 4500, agent_instructions: 4000, tool_output: 2500, your_message: 1500 },
  { loaded_skill: 23000, memory: 7000, history: 8000, lessons: 4500, agent_instructions: 4000, tool_output: 3000, your_message: 500 },
], 0.22)

// gpt-review: HISTORY-dominant (~58%).
const GPT = makeTrace('subagent:gpt', [
  { history: 58000, loaded_skill: 14000, memory: 12000, tool_output: 9000, your_message: 7000 },
], 0.51)

// opus-review: HISTORY-dominant (~62%), 2 turns.
const OPUS = makeTrace('subagent:opus', [
  { history: 30000, loaded_skill: 6000, memory: 5000, tool_output: 4000, your_message: 3000 },
  { history: 32000, loaded_skill: 7000, memory: 5000, tool_output: 4000, your_message: 2000 },
], 0.58)

// verify-repro: TOOL_OUTPUT-dominant (~26%), a flatter mix (a short failed run).
const VERIFY = makeTrace('subagent:verify', [
  { tool_output: 26000, history: 22000, loaded_skill: 20000, memory: 18000, your_message: 14000 },
], 0.33)

const TRACE_BY_SLOT = {
  [SLOT]: PARENT,
  'subagent:gpt': GPT,
  'subagent:opus': OPUS,
  'subagent:verify': VERIFY,
}

// ---------------------------------------------------------------------------
// Fixtures for the boot path: one slot for chat-1, and a detail body.
// ---------------------------------------------------------------------------
const slots = [{
  key: SLOT,
  title: 'Babysit PR #5966 to green and review it',
  running: false,
  last_message: 'Babysit PR #5966 to green and review it',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const now = () => Date.now() / 1000
const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now() - 600, content: 'Babysit PR #5966 to green and review it.' },
    { role: 'assistant', ts: now() - 300, content: 'Spawned three review sub-agents; watching their context windows.' },
  ],
}

// Distinct started times => stable spawn order gpt < opus < verify.
const T0 = Math.floor(now()) - 200
const SUBAGENTS = [
  {
    id: 'sa-gpt', child_session: 'subagent:gpt', agent: 'gpt-review', model: 'gpt-5.6-sol',
    task: 'read specs', tool_count: 12, started: T0, elapsed: 84000, outcome: 'completed',
  },
  {
    id: 'sa-opus', child_session: 'subagent:opus', agent: 'opus-review', model: 'claude-opus-5',
    task: 'review the diff', tool_count: 9, started: T0 + 5, elapsed: 96000, outcome: 'completed',
  },
  {
    id: 'sa-verify', child_session: 'subagent:verify', agent: 'verify-repro', model: 'gpt-5.6-sol',
    task: 'reproduce the failure', tool_count: 4, started: T0 + 10, elapsed: 14200, outcome: 'failed',
  },
]

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
    viewport: { width: 640, height: 1040 },
    deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: { width: 640, height: 1040 } },
  })
  const { page, ws } = h

  // Per-node context-trace route. Registered BEFORE load() so it takes priority
  // over the harness's `**/api/**` catch-all (newest route wins). The tree
  // fetches /api/telemetry/context-trace?slot=<childSession> per node, and the
  // panel itself fetches it for the parent slot.
  await page.route('**/api/telemetry/context-trace**', route => {
    const slot = new URL(route.request().url()).searchParams.get('slot') || ''
    const body = TRACE_BY_SLOT[slot] ?? TRACE_BY_SLOT[SLOT]
    return json(route, body)
  })

  // Cold boot with the chat slot selected.
  await h.load('dark')

  // Seed Developer Mode + the panel strip (a Context tab, focused) + the
  // side-panel open flag, then reload so the SPA boots straight into the
  // Context view. Registered AFTER load() so this init script runs LAST on the
  // next navigation — it therefore wins over the harness's localStorage.clear().
  await page.addInitScript(([slot]) => {
    localStorage.setItem('mc-dev-mode', '1')
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem(
      'mc-panel-tabs:' + slot,
      JSON.stringify({ activeId: 'context', tabs: [{ id: 'context', kind: 'context', title: 'Context' }] }),
    )
  }, [SLOT])
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)

  // Push the three sub-agents over the mocked socket: a running snapshot then a
  // terminal done frame each, carrying child_session so each node fetches its
  // own trace. The reducers map started->startedAt, child_session->childSession,
  // outcome->status.
  const socket = ws()
  if (!socket) throw new Error('websocket route not bound — cannot push subagent frames')
  const send = obj => socket.send(JSON.stringify(obj))
  for (const s of SUBAGENTS) {
    send({
      type: 'subagent_snapshot',
      data: {
        id: s.id, slot: SLOT, child_session: s.child_session, task: s.task,
        agent: s.agent, model: s.model, streaming: '', last_tool: '',
        started: s.started, tool_count: s.tool_count,
      },
    })
    send({
      type: 'subagent_done',
      data: {
        slot: SLOT, id: s.id, child_session: s.child_session, elapsed: s.elapsed,
        outcome: s.outcome, agent: s.agent, model: s.model,
      },
    })
  }

  // Let the tree render + each node's trace query resolve.
  await page.waitForSelector('text=Session Breakdown', { timeout: 20000 })
  await page.waitForTimeout(1500)

  // ---- Expand a node (opus-review — history-dominant) so its per-turn
  // composition shows, contrasting the parent's skill-heavy panel below. ----
  const expandTarget = 'opus-review'
  const row = page.locator('[role="button"]', { hasText: expandTarget }).first()
  await row.waitFor({ timeout: 10000 })

  // Small settle before the recorded expand so the video opens on the collapsed
  // tree, then animates the disclosure.
  await page.waitForTimeout(1200)
  await row.click()
  // node_caption ("… · N turns · its own window") only renders once expanded.
  await page.waitForSelector('text=its own window', { timeout: 10000 })
  await page.waitForTimeout(1600)

  // ---- ASSERT rendered text before saving the PNG. ----
  // innerText reflects CSS text-transform, so the header and node caption come
  // back UPPERCASED ("SESSION BREAKDOWN", "ITS OWN WINDOW"). Compare
  // case-insensitively; the agent names are not transformed but fold safely too.
  const body = await page.locator('body').innerText()
  const hay = body.toLowerCase()
  const required = [
    'session breakdown',
    'gpt-review',
    'opus-review',
    'verify-repro',
    'its own window',        // expanded node caption
  ]
  const missing = required.filter(t => !hay.includes(t))
  // A status word must be present (running / done / ended).
  const hasStatus = /\b(done|ended|running)\b/i.test(body)
  if (missing.length || !hasStatus) {
    throw new Error(
      `assert failed: missing=${JSON.stringify(missing)} hasStatus=${hasStatus}\n` +
      `--- body (first 2500) ---\n${body.slice(0, 2500)}`,
    )
  }
  console.log('ASSERT OK: tree + 3 agent names + status + expanded caption all present')

  // ---- Screenshot ----
  await page.screenshot({ path: join(OUT, 'session-tree.png') })
  console.log('wrote', join(OUT, 'session-tree.png'))

  // A short tail after the expand so the recorded clip is 6-12s and ends on the
  // expanded state.
  await page.waitForTimeout(1500)

  const webm = await h.close()
  console.log('video webm:', webm)

  // ---- webm -> mp4 ----
  if (webm) {
    const mp4 = join(OUT, 'session-tree.mp4')
    const r = spawnSync('ffmpeg', [
      '-y', '-i', webm,
      '-movflags', '+faststart',
      '-pix_fmt', 'yuv420p',
      // even dimensions required by yuv420p
      '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
      mp4,
    ], { encoding: 'utf8' })
    if (r.status !== 0) {
      console.error('ffmpeg failed:', r.stderr?.slice(-1500))
      process.exit(1)
    }
    console.log('wrote', mp4)
  } else {
    console.error('no webm produced — video recording failed')
    process.exit(1)
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
