/**
 * Isolated capture entry for the Workflows graph mode (issue #11796, from #1652).
 *
 * WHY ISOLATED: the frame needs a run whose script has already been previewed by the
 * gateway and whose event stream is at a chosen point mid-run. Only `fetch` is stubbed;
 * everything else is real — the REAL WorkflowsRuns panel, the REAL graph and tree, the
 * REAL stylesheet and theme tokens, and the exact payload shapes
 * `GET /api/workflows/runs/{id}` emits, `plan` included.
 *
 * Scenes (?scene=):
 *   mid      the loop in "Ship" has not run yet: its marker and its uncertain work are
 *            dashed, and "Ship" itself is a stage nothing has entered.
 *   loop     the same run after the loop produced three agents the plan drew as one
 *            node. The prediction is gone and reality is in its place.
 *   noplan   a task-plan run: no Python entrypoint, so no plan is readable and the
 *            graph says so instead of drawing an empty one.
 *   gated    a stage the script only reaches under an `if`, so the plan cannot promise
 *            it runs at all.
 *   surprise the run enters a stage the plan never named.
 *   truncated the previewer hit its own ceiling, so the plan shown is partial.
 *   empty    a readable plan that predicted nothing, and nothing has run yet.
 *   detailfail the run-detail read fails, which is also how a graph-mode plan fetch
 *            fails, so the frame shows the error surface and its agent hand-off.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import WorkflowsRuns, { type RunDetail } from '../src/apps/workflows/WorkflowsRuns'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'mid'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const SCRIPT = `META = {"name": "release notes"}

async def workflow(ctx):
    with ctx.phase("Research"):
        findings = await ctx.parallel([
            lambda: ctx.agent("read the spec", label="spec"),
            lambda: ctx.agent("read the diff", label="diff"),
        ])
    with ctx.phase("Write"):
        draft = await ctx.agent("draft the notes", label="draft")
    with ctx.phase("Ship"):
        for item in findings:
            await ctx.agent("file it", label="file")
`

/** What the gateway's previewer returns for SCRIPT. */
const PLAN = {
  phases: [
    {
      title: 'Research',
      certain: true,
      nodes: [
        { kind: 'agent' as const, label: 'spec', certain: true },
        { kind: 'agent' as const, label: 'diff', certain: true },
      ],
    },
    {
      title: 'Write',
      certain: true,
      nodes: [{ kind: 'agent' as const, label: 'draft', certain: true }],
    },
    {
      title: 'Ship',
      certain: true,
      nodes: [
        { kind: 'unknown' as const, label: 'for', certain: false },
        { kind: 'agent' as const, label: 'file', certain: false },
      ],
    },
  ],
  truncated: false,
  titleLimit: 120,
}

/** Same plan with a phase the script only reaches under an `if`. */
const GATED_PLAN = {
  ...PLAN,
  phases: [
    ...PLAN.phases,
    {
      title: 'Announce',
      certain: false,
      nodes: [{ kind: 'agent' as const, label: 'post', certain: false }],
    },
  ],
}

/** Same plan, reported partial because the previewer hit its own ceiling. */
const TRUNCATED_PLAN = { ...PLAN, truncated: true }

/** A readable plan that predicted nothing at all. */
const EMPTY_PLAN = { phases: [], truncated: false, titleLimit: 120 }

function ev(type: string, data: Record<string, unknown>, seq: number, ts: string) {
  return { run_id: 'wf_notes', seq, ts, type, data }
}

const T = (s: number) => `2026-09-18T10:0${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}.000Z`

/** Research done, Write in flight, Ship not entered. */
const MID = [
  ev('run_started', { name: 'release notes', budget_total: 40000 }, 0, T(0)),
  ev('phase_started', { title: 'Research' }, 1, T(0)),
  ev('agent_started', { agent_id: 'a0', label: 'spec', phase: 'Research' }, 2, T(0)),
  ev('agent_started', { agent_id: 'a1', label: 'diff', phase: 'Research' }, 3, T(0)),
  ev('agent_finished', { agent_id: 'a0', ok: true }, 4, T(7)),
  ev('agent_finished', { agent_id: 'a1', ok: true }, 5, T(11)),
  ev('phase_started', { title: 'Write' }, 6, T(11)),
  ev('agent_started', { agent_id: 'a2', label: 'draft', phase: 'Write' }, 7, T(11)),
  ev('budget_update', { spent: 12400 }, 8, T(12)),
]

/** The loop ran: three agents where the plan could only draw one node. */
const LOOP = [
  ...MID.filter(e => e.type !== 'budget_update'),
  ev('agent_finished', { agent_id: 'a2', ok: true }, 9, T(24)),
  ev('phase_started', { title: 'Ship' }, 10, T(24)),
  ev('agent_started', { agent_id: 'a3', label: 'file: spec gap', phase: 'Ship' }, 11, T(24)),
  ev('agent_started', { agent_id: 'a4', label: 'file: diff gap', phase: 'Ship' }, 12, T(24)),
  ev('agent_started', { agent_id: 'a5', label: 'file: release note', phase: 'Ship' }, 13, T(24)),
  ev('agent_finished', { agent_id: 'a3', ok: true }, 14, T(31)),
  ev('agent_finished', { agent_id: 'a4', ok: false }, 15, T(33)),
  ev('budget_update', { spent: 28900 }, 16, T(33)),
]

/** The run does a phase the plan never named -- a genuine surprise, marked as one. */
const SURPRISE = [
  ...MID,
  ev('phase_started', { title: 'Hotfix' }, 20, T(40)),
  ev('agent_started', { agent_id: 'h0', label: 'patch the build', phase: 'Hotfix' }, 21, T(40)),
  ev('agent_finished', { agent_id: 'h0', ok: true }, 22, T(46)),
]

const TASK_PLAN = [
  ev('run_started', { name: 'nightly triage' }, 0, T(0)),
  ev('phase_started', { title: 'Triage' }, 1, T(0)),
  ev('agent_started', { agent_id: 'a0', label: 'sort the queue', phase: 'Triage' }, 2, T(0)),
  ev('agent_finished', { agent_id: 'a0', ok: true }, 3, T(9)),
]

const BASE = {
  run_id: 'wf_notes',
  name: 'release notes',
  status: 'running' as const,
  result: null,
  error: null,
  author: 'dashboard:chat-1',
  session_key: 'dashboard:chat-1',
  source_format: 'python' as const,
  driver: 'workflow' as const,
  capabilities: [],
}

const DETAIL: Record<string, RunDetail> = {
  mid: { ...BASE, event_count: MID.length, source: SCRIPT, plan: PLAN, events: MID },
  loop: { ...BASE, event_count: LOOP.length, source: SCRIPT, plan: PLAN, events: LOOP },
  gated: { ...BASE, event_count: MID.length, source: SCRIPT, plan: GATED_PLAN, events: MID },
  surprise: {
    ...BASE,
    event_count: SURPRISE.length,
    source: SCRIPT,
    plan: PLAN,
    events: SURPRISE,
  },
  truncated: {
    ...BASE,
    event_count: MID.length,
    source: SCRIPT,
    plan: TRUNCATED_PLAN,
    events: MID,
  },
  empty: { ...BASE, event_count: 0, source: SCRIPT, plan: EMPTY_PLAN, events: [] },
  noplan: {
    ...BASE,
    run_id: 'wf_notes',
    name: 'nightly triage',
    source_format: 'task-plan',
    driver: 'taskrunner',
    event_count: TASK_PLAN.length,
    source: 'agents:\n  triage:\n    prompt: sort the queue\n',
    events: TASK_PLAN,
  },
}

const detail = DETAIL[scene] || DETAIL.mid

// Stub only the two reads this panel makes. Every shape below is what the gateway
// actually returns, so the frame cannot show a payload the product cannot produce.
window.fetch = (async (input: RequestInfo | URL) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url || input)
  // One scene exists to photograph the FAILURE surface: the list still answers, so a row
  // is selectable, and the detail read fails the way a gateway failure reads. Graph mode
  // asks for the plan on this same endpoint, so this is also the plan-fetch failure.
  if (scene === 'detailfail' && url.includes('/runs/')) {
    return new Response(JSON.stringify({ error: 'workflow run detail unavailable' }), {
      status: 500,
      headers: { 'content-type': 'application/json' },
    })
  }
  const body = url.includes('/runs/')
    ? detail
    : { runs: [{ ...detail, events: undefined, result: undefined }] }
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}) as typeof window.fetch

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={client}>
    <div className="p-4" style={{ background: 'var(--bg)', minHeight: '100vh' }}>
      <WorkflowsRuns embedded />
    </div>
  </QueryClientProvider>,
)
