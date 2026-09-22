/**
 * Isolated capture entry for DagView's two approval states.
 *
 * WHY ISOLATED: the state lives inside a running AIDLC project whose executor
 * has parked one task at its approval gate — nothing a capture run can start.
 * This mounts the REAL DagView against the real stylesheet, theme tokens and
 * live i18n catalog, with the props ProjectDetailPage passes in that state:
 *
 *   - "Implement" is `in_progress` and has an `approvalMap` entry: the executor
 *     marks a task in_progress before it enters the approval gate, so this is
 *     the node that is waiting for a decision. It owns the warn halo and the
 *     Approve / Deny buttons, and its status word is the one that names that.
 *   - "Deploy" is `pending` with `requires_approval`: a gate the run has not
 *     reached yet. It is drawn like any other pending node and named as a gate,
 *     so the reader can tell it apart from the decision to make now.
 *   - "Plan" (passed) and "Announce" (pending, no gate) are the ordinary
 *     neighbours the two approval states are read against.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import DagView from '../src/pages/aidlc/DagView'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const NODES = [
  { id: '1', title: 'Plan', status: 'passed' },
  { id: '2', title: 'Implement', status: 'in_progress', requires_approval: true },
  { id: '3', title: 'Deploy', status: 'pending', requires_approval: true },
  { id: '4', title: 'Announce', status: 'pending' },
]
const EDGES = [{ from: '1', to: '2' }, { from: '2', to: '3' }, { from: '3', to: '4' }]

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div data-capture-root style={{ width: 520, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
          <div data-capture-section="dag-view">
            <DagView nodes={NODES} edges={EDGES} onNodeClick={() => {}} approvalMap={{ 2: 'appr-1' }} onApprove={() => {}} />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
