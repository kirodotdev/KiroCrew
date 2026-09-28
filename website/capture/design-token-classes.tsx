/**
 * Isolated capture entry for the surfaces whose colors moved from raw
 * Tailwind palette classes / hex literals to theme tokens when the
 * `shadcn/no-raw-colors` lint went blocking.
 *
 * WHY ISOLATED: the states live inside a running cron log, a pending tool
 * approval and an in-flight AIDLC project — none exists in a capture run.
 * This mounts the REAL components against the real stylesheet, theme tokens
 * and live i18n catalog, with the props their pages pass in those states.
 *
 * Surfaces, top to bottom:
 *   - LogEntry: a `manual` trigger pill (was purple-100/700, now accent-subtle/accent)
 *     next to a `scheduled` one for contrast.
 *   - CollapsibleToolGroup: the pulsing approval dot (was amber-400, now warn)
 *     and the running dot (was green-400, now ok).
 *   - DagView: every node status the legend names, a `fix` and a `checkpoint`
 *     typed node, a selected node, a pending-edit dot and a running node awaiting
 *     approval (warn ring + Approve / Deny buttons) — the page-wide
 *     palette (orange running / gray pending / hex greens) is now tokens, so
 *     running follows the theme accent like every other running state.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import LogEntry from '../src/components/LogEntry'
import CollapsibleToolGroup from '../src/pages/chat/CollapsibleToolGroup'
import DagView from '../src/pages/aidlc/DagView'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const NODES = [
  { id: '1', title: 'Plan', status: 'passed' },
  { id: '2', title: 'Implement', status: 'in_progress' },
  { id: '3', title: 'Review', status: 'blocked', requires_approval: true },
  { id: '4', title: 'Fix lint', status: 'pending', task_type: 'fix' },
  { id: '5', title: 'Checkpoint', status: 'paused', task_type: 'checkpoint' },
  { id: '6', title: 'Deploy', status: 'failed' },
]
const EDGES = [
  { from: '1', to: '2' }, { from: '2', to: '3' }, { from: '3', to: '4' },
  { from: '4', to: '5' }, { from: '5', to: '6' },
]

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width: 720, margin: '16px auto', background: 'var(--bg)', padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div data-capture-section="log-entry" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <LogEntry jobId="job-1" entry={{ run_id: 'r1', status: 'success', started_at: 1758326400, duration_ms: 4200, trigger: 'manual', summary: 'Ran on request' }} />
            <LogEntry jobId="job-1" entry={{ run_id: 'r2', status: 'success', started_at: 1758322800, duration_ms: 3900, trigger: 'scheduled', summary: 'Ran on schedule' }} />
          </div>
          <div data-capture-section="tool-group" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <CollapsibleToolGroup count={2} isRunning hasPermission pendingPermCount={1}>{null}</CollapsibleToolGroup>
            <CollapsibleToolGroup count={2} isRunning>{null}</CollapsibleToolGroup>
          </div>
          <div data-capture-section="dag-view">
            {/* Node 2 is running AND awaiting approval: the pulsing warn ring plus the
                Approve / Deny buttons render only in that combination. */}
            <DagView nodes={NODES} edges={EDGES} onNodeClick={() => {}} selectedId="2" pendingEditIds={new Set(['1'])} approvalMap={{ 2: 'pending' }} onApprove={() => {}} />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
