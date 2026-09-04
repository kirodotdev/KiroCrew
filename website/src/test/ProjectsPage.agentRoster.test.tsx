/**
 * ProjectsPage's own wiring of the roster failure (#7656, same class as #5990).
 *
 * The page used to call `api.kirocrewAgents()` directly with `.catch(() => {})`
 * into local state, so the hook-level fix that gave every `useAgents` consumer a
 * readable `error` never applied here: a failed roster fetch rendered as a
 * legitimately empty agent list. Per the mutation-testing note on the schedule
 * form's sibling test, this file drives the REAL page against a failing
 * `/api/agents` rather than handing `rosterFailure` straight to the child —
 * replacing the page's `rosterFailure = rosterError ? {...} : undefined` with a
 * constant `undefined` must fail these tests.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectsPage from '../pages/ProjectsPage'

// Isolate the detail pane; the surface under test is the compose panel's
// agent picker, which must stay the REAL AgentSelector for the wiring to show.
vi.mock('../pages/ProjectDetailPage', () => ({ default: () => <div data-testid="project-detail">Detail</div> }))

vi.mock('../api/client', () => ({
  api: {
    taskRunnerStatus: vi.fn().mockResolvedValue({ running: false, available: true, runs: [] }),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
    kirocrewAgents: vi.fn(),
  },
}))

const ROSTER = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'paging', source: 'package' },
]

/** Render the page and open the compose panel's agent picker. */
async function openAgentPicker() {
  const rendered = renderWithProviders(<ProjectsPage />)
  fireEvent.click(await screen.findByLabelText('Switch agent', {}, { timeout: 5000 }))
  return rendered
}

describe('ProjectsPage roster failure wiring (#7656)', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    sessionStorage.clear()
    const { api } = await import('../api/client')
    vi.mocked(api).taskRunnerStatus.mockResolvedValue({ running: false, available: true, runs: [] })
    vi.mocked(api).syncKirocrewAgents.mockResolvedValue({})
    vi.mocked(api).kirocrewAgents.mockRejectedValue(new Error('gateway restarting'))
  })

  it('hands the picker a failure it can act on when /api/agents rejects', async () => {
    await openAgentPicker()

    await waitFor(() => {
      expect(screen.getByText("Couldn't load the agent list.")).toBeInTheDocument()
    })
    expect(screen.getByText('Retry')).toBeInTheDocument()
    // A failed load is an error, not an install with no agents to match.
    expect(screen.queryByText('No matches')).not.toBeInTheDocument()
  })

  it('recovers the roster through the page\u2019s own reload', async () => {
    const { api } = await import('../api/client')
    await openAgentPicker()
    await waitFor(() => expect(screen.getByText('Retry')).toBeInTheDocument())

    vi.mocked(api).kirocrewAgents.mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(screen.getByText('oncall')).toBeInTheDocument())
    expect(screen.queryByText("Couldn't load the agent list.")).not.toBeInTheDocument()
  })

  it('recovers every roster consumer, not just this page', async () => {
    const { api } = await import('../api/client')
    const { store } = await openAgentPicker()
    await waitFor(() => expect(screen.getByText('Retry')).toBeInTheDocument())

    const before = store.getState().dashboard.refreshTrigger
    vi.mocked(api).kirocrewAgents.mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
    fireEvent.click(screen.getByText('Retry'))

    // `useAgents` state is per-instance and the app shell holds its own copy, so
    // a retry that refreshed only this page would leave other surfaces on the
    // empty roster they failed with. The page bumps the shared trigger.
    await waitFor(() => expect(store.getState().dashboard.refreshTrigger).toBe(before + 1))
  })
})
