/**
 * Visual evidence for "the run tree reports how long each agent took" (#1652).
 *
 * WHY ISOLATED: the live surface needs a dynamic workflow actually executing so
 * the backend streams `agent_started` / `agent_finished` into the tab, which is
 * not reproducible on demand and would make the frame depend on whatever that
 * run happened to do.
 *
 * WHAT IS FAITHFUL is the whole claim: this is the real `WorkflowRunTree`
 * component folding a real event stream through its real `runModel`. Only the
 * events are synthetic, and they are the shape the backend already emits -- no
 * prop is reached around, and nothing about the reading is hand-drawn.
 *
 * The agents are chosen to show every band the formatter has in one frame, plus
 * the case that must render NOTHING:
 *   - 4.2s   sub-10s, a tenth is useful
 *   - 37s    sub-minute, the tenth is noise
 *   - 6m 38s past a minute, the minutes place carries the meaning
 *   - 2m 0s  the value that must never read as the invalid "1m 60s"
 *   - still running, which shows its spinner and no time
 *
 * The `before` scene feeds the identical stream with a `ts` that does not parse,
 * which is what every event carried before this change: the span is
 * unmeasurable, so no reading appears. That makes the two frames differ by
 * exactly the feature, and doubles as evidence that the unmeasurable guard
 * renders nothing rather than a zero.
 *
 *   ?scene=before|after &theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort
 *   node scripts/capture-workflow-run-tree-agent-timing.mjs http://127.0.0.1:6817 \
 *     ../temp-screenshots/workflow-run-tree-agent-timing
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import WorkflowRunTree from '../src/apps/workflows/WorkflowRunTree'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Start instant every span below is measured from. */
const T0 = Date.parse('2026-09-18T10:00:00.000Z')

/** `after` carries the real `ts` the backend sends; `before` carries the
 *  unparseable placeholder, which is what makes its spans unmeasurable. */
const ts = (offsetMs: number): string =>
  scene === 'before' ? 't' : new Date(T0 + offsetMs).toISOString()

let seq = 0
const ev = (type: string, offsetMs: number, data: Record<string, unknown>) =>
  ({ run_id: 'wf_capture1652', seq: seq++, ts: ts(offsetMs), type, data })

/** One agent's start and finish, `spanMs` apart. */
const ran = (agent_id: string, label: string, phase: string, startMs: number, spanMs: number) => [
  ev('agent_started', startMs, { agent_id, label, phase }),
  ev('agent_finished', startMs + spanMs, { agent_id, ok: true }),
]

const EVENTS = [
  ev('run_started', 0, { name: 'renderer-audit', budget_total: 4000 }),
  ev('phase_started', 0, { title: 'Research' }),
  ...ran('a0', 'research:prior-art', 'Research', 0, 4_200),
  ...ran('a1', 'research:call-sites', 'Research', 0, 37_400),
  ev('phase_started', 40_000, { title: 'Implement' }),
  ...ran('a2', 'implement:model', 'Implement', 40_000, 398_000),
  ...ran('a3', 'implement:render', 'Implement', 40_000, 119_600),
  ev('phase_started', 460_000, { title: 'Verify' }),
  ev('agent_started', 460_000, { agent_id: 'a4', label: 'verify:mutations', phase: 'Verify' }),
  ev('budget_update', 460_000, { spent: 1240 }),
]

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <div
          data-capture-root
          data-scene={scene}
          className="bg-bg text-text relative p-4"
          style={{ width: 620 }}
        >
          <div className="text-[10px] uppercase tracking-wider text-accent/70 pb-1.5 font-mono">
            workflow run tree — per-agent time
          </div>
          <WorkflowRunTree events={EVENTS} status="running" />
        </div>
      </Provider>
    </QueryClientProvider>
  </MemoryRouter>,
)
