/**
 * The third path that can learn a run has ended: the run's OWN snapshot.
 *
 * Expanding a row fetches `/api/workflows/runs/{id}`, which is authoritative — so
 * a header still spinning next to a tree that already reads "finished" is a
 * missed terminal frame the component is holding the answer to. It writes that
 * answer back through the same monotonic merge the connect-time reconcile uses,
 * so it can only ever advance a running row, never rewind one.
 */
import { render } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { sseWorkflowEvent } from '../store/chatSlice'
import WorkflowProgressBar from '../pages/chat/WorkflowProgressBar'

const snapshotRef: { current: { status?: string; error?: string | null; events?: unknown[]; source?: string } | null } = { current: null }

vi.mock('../apps/workflows/useRunSnapshot', () => ({
  useRunSnapshot: () => ({ snapshot: snapshotRef.current, error: null }),
}))

describe('WorkflowProgressBar snapshot write-back', () => {
  let store: ReturnType<typeof createTestStore>

  beforeEach(() => {
    store = createTestStore()
    snapshotRef.current = null
    store.dispatch(sseWorkflowEvent({
      run_id: 'wf_000025', session_key: 'dashboard:chat-1',
      type: 'run_started', data: { name: 'perf audit' },
    }))
  })

  const renderBar = () => render(
    createElement(Provider, { store },
      createElement(QueryClientProvider, { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        createElement(WorkflowProgressBar, { slot: 'chat-1' }),
      ),
    ),
  )

  const status = () => store.getState().chat.workflowRuns['wf_000025'].status

  it('advances a stuck row when its own snapshot says the run ended', () => {
    snapshotRef.current = { status: 'failed', error: 'authoring error', events: [], source: '' }
    renderBar()
    expect(status()).toBe('failed')
    expect(store.getState().chat.workflowRuns['wf_000025'].error).toBe('authoring error')
  })

  it('leaves the row alone while the snapshot agrees it is running', () => {
    snapshotRef.current = { status: 'running', events: [], source: '' }
    renderBar()
    expect(status()).toBe('running')
  })

  it('does not act on a snapshot it has not loaded yet', () => {
    snapshotRef.current = null
    renderBar()
    expect(status()).toBe('running')
  })
})
