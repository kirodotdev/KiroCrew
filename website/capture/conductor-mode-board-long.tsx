/**
 * Capture page: the conductor mode Board's Status view on a LONG run, the shape a
 * real run reaches after a day: 94 milestones, 87 done, 7 running, titles of 150+
 * characters, a goal and a "Done when" longer than one line. `?w=` sets the panel
 * width (360, 560, 900), `?theme=light` the light palette, `?goal=2` serves the
 * goal rewritten with only its Plan and Changed lines moved. The panel is the
 * real component; only the reads are fixtures, and every text is made up.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import ConductorModeCrewPanel from '../src/pages/chat/command-center/ConductorModeCrewPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')
const width = Number(params.get('w') || 560)

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const item = (id: string, title: string, over: Record<string, unknown>) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'pr_checks' }, state: 'accepted', verdict: 'pass', decision: '', round: 1,
  fails: 0, status: 'done', summary: 'Merged; the benchmark row moved from 4.1 s to 2.6 s on the same host.', artifacts: {}, pr: null,
  last_report_at: ago(600), created_at: ago(1400), closed_at: ago(590), decided_at: null, orphaned: false, stale: false,
  acceptance_concrete: true, outstanding: false, terminal: true, alive: 'closed', events: [], reason: null, awaits_person: false, ...over,
})
const open = { state: 'open', verdict: null, terminal: false, closed_at: null, status: 'progress' }
const DONE_TITLES = [
  'Speed work: benchmark harness plus a Linux baseline (the CLI against the app, four metrics times three conditions)',
  'Speed work: measured root-cause profile of the first reply, split into start, tool load and first token',
  'Speed fix: the tool list is read once per start instead of once per server, with the same answer',
]
const done = Array.from({ length: 87 }, (_, n) => item(`d${n}`, `${DONE_TITLES[n % 3]} (part ${n + 1})`, { created_at: ago(1400 - n) }))
const RUNNING: [string, string, number][] = [
  ['Speed work: small machines release the embedding model sooner with no side effects (channel early load, operator value kept); RFC first, then one PR', 'PR 37173 10:24Z: the red checks are not from this docs diff.', 2],
  ['Speed fix: custom agents at sixty-four servers get faster with the same answer (pure glob memo, one scope read, no rewrite of an identical environment file)', 'New head dcc4fb9d3b14c95d8df302e5c0c75647c37b29eb (ls-remote), on main d15505809e1d.', 4],
  ['Speed fix (second backend): no fixed 500 ms drain after a new session, less work before the adapter starts; plus the old adapter background response', '10:23Z Research section is written: each own server needs about 0.63 s to start (imports), and core needs 0.24 s more for its tools.', 9],
  ['Speed work (remote box): smoke of the cost PR (session-stable blocks in the agent prompt) with the internal set and the GPU agents; evidence only', '10:28Z.', 15],
  ['Owner task 1: read the team channel, survey agent-harness benchmarks, pick the best fit for the app with a run plan; evidence and a short report', 'Step 1 done: read the channel from 2026-10-01 (53 messages, 11-05 to 11-13).', 31],
  ['Owner task 2: how the engine loads MCP servers and how its skill search works, from the engine source, compared with the app; research only', '', 47],
  ['Owner task 3: design doc for turning off the app\'s own sub-agents and using the engine\'s sub-agents, in the sample report\'s shape, uploaded to the shared space', '', 58],
]
const running = RUNNING.map(([title, summary, min], n) => item(`r${n}`, title, { ...open, summary, alive: 'running', last_report_at: ago(min), created_at: ago(200 - n) }))
const GOAL_1 = 'Make the main session reply as fast as the CLI or faster on the same engine, with the same security bar the CLI holds today. '
  + 'Done when: side-by-side benchmarks (no config; heavy memory; 60+ MCP servers; one-word and two-sentence warm replies) show the main session at or ahead of the CLI on Linux, macOS and Windows. '
  + 'Plan: it_a profile; it_b fix the start path; it_c fix the tool load. Changed: 2026-11-12 09:00 first plan.'
const GOAL_2 = GOAL_1.replace(/Plan: .*$/, 'Plan: owner tasks 1 to 3 (survey, engine loading, sub-agent design) next to the open fixes. Changed: 2026-11-14 10:20 the owner added three research tasks; the start-path fix merged.')
const conductor = { schema: 1, slot_key: 'root', goal: params.get('goal') === '2' ? GOAL_2 : GOAL_1, round: 4, depth: 0, parent_item: null, created_at: ago(1500), goal_version: 6, round_started_at: ago(30) }
Object.assign(api, {
  crewWorkers: async () => ({ root: 'root', workers: [] }),
  crewBoard: async () => ({ conductor, conductor_alive: 'idle', items: [...done, ...running], take_over_available: false, run: { settled: '', open: 7, accepted: 87, not_done: 0, at_limit: false } }),
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div style={{ width, height: 1500 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
        <ConductorModeCrewPanel root="root" viewing="root" active />
      </div>
    </QueryClientProvider>
  </Provider>,
)
