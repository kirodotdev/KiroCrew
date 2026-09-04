/**
 * CrewEditor's own wiring of the roster failure (#7656, same class as #5990).
 *
 * The dialog calls `useAgents(0)` — a constant trigger, so the roster fetch runs
 * once per mount and never retries on its own. A failed fetch used to render as
 * a legitimately empty picker with no way out for the life of the dialog. This
 * file drives the REAL dialog with the real `useAgents` against a rejecting
 * `/api/agents` (the sibling `IssueRadarCrewEditor` suite stubs the hook, so it
 * cannot see this wiring), and pins two compositions:
 *
 *  - the error line + retry render when the fetch failed, and retry re-fetches;
 *  - the stale-agent preservation in `agentOptions` SURVIVES the error state: a
 *    crew's current agent stays selectable even while the roster is missing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'

const mockApi = vi.hoisted(() => ({
  createCrew: vi.fn(),
  updateCrew: vi.fn(),
  suggestCrewNames: vi.fn(),
  labels: vi.fn(),
}))
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: mockApi }))

/* The transport under the REAL `useAgents`: rejecting `kirocrewAgents` is the
   defect this suite exists for, so the hook must not be stubbed here. */
vi.mock('../api/client', () => ({
  api: {
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
    kirocrewAgents: vi.fn(),
  },
}))

const MODELS = [
  { name: 'auto', description: '' },
  { name: 'test-model', description: 'served' },
]
vi.mock('../hooks/useAvailableModels', () => ({
  useAvailableModels: () => MODELS,
}))

/* Same plain-DOM SimpleSelect stand-in as the sibling CrewEditor suites, for
   the same hard harness limit: Radix Select commits its discrete events with
   `flushSync`, which throws inside Testing Library's `act()`. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options,
    value,
    onChange,
    clearLabel,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    clearLabel?: string
    'aria-label'?: string
  }) => {
    const listId = `stub-select-${ariaLabel ?? 'x'}`
    return (
      <div>
        <button type="button" role="combobox" aria-label={ariaLabel} aria-expanded={false} aria-controls={listId}>
          {options.includes(value) ? value : clearLabel ?? value}
        </button>
        <div id={listId}>
          {options.map(o => (
            <button key={o} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>
              {o}
            </button>
          ))}
        </div>
      </div>
    )
  },
}))

const ACTIVE = { owner: 'kirodotdev', repo: 'KiroCrew' } // brand-ok: the repository name
vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ({ active: ACTIVE }),
}))

import CrewEditor from '../apps/issue-radar/components/CrewEditor'
import type { Crew } from '../apps/issue-radar/api'

const K = 'apps.issueRadar.views.crews.editor'

const ROSTER = [
  { name: 'kirocrew', source: 'kiro', description: 'The default agent' },
  { name: 'kirocrew-crew', source: 'kiro', description: 'Issue worker' },
]

/** A stored crew whose agent is ABSENT from the roster, so the edit-mode case
 *  doubles as the stale-agent-preservation one. */
const CREW: Crew = {
  schema: 1,
  id: 'c_1a2b3c4d',
  name: 'Whirlpool',
  avatar_seed: 'Whirlpool',
  avatar_variant: null,
  agent: 'oncall',
  model: '',
  extra_prompt: '',
  labels: [],
  auto_resolve_conflicts: false,
  auto_merge: false,
  unattended: false,
  max_open: 2,
  worktree_root: '',
  slot_key: 'crew-c_1a2b3c4d',
  enabled: true,
  paused_reason: '',
  created_at: '2026-08-01T00:00:00Z',
  retired_at: null,
}

function renderEditor(crew?: Crew | null) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <CrewEditor open onClose={vi.fn()} crew={crew} />
    </QueryClientProvider>,
  )
}

const agentField = () => within(screen.getByTestId('crew-editor-agent'))

describe('CrewEditor roster failure wiring (#7656)', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    mockApi.suggestCrewNames.mockResolvedValue({ suggestions: ['Sombrero'] })
    mockApi.labels.mockResolvedValue({ owner: ACTIVE.owner, repo: ACTIVE.repo, labels: [], from_cache: false })
    const { api } = await import('../api/client')
    vi.mocked(api).syncKirocrewAgents.mockResolvedValue({})
    vi.mocked(api).kirocrewAgents.mockRejectedValue(new Error('gateway restarting'))
  })

  it('shows the error line and a retry when the roster fetch failed', async () => {
    renderEditor()
    await screen.findByTestId('crew-editor')

    await waitFor(() => {
      expect(agentField().getByText(i18nT(`${K}.agent_roster_failed`))).toBeInTheDocument()
    })
    expect(agentField().getByText(i18nT(`${K}.agent_roster_retry`))).toBeInTheDocument()
  })

  it('retry re-fetches the roster and clears the error line', async () => {
    const { api } = await import('../api/client')
    renderEditor()
    await screen.findByTestId('crew-editor')
    await waitFor(() => {
      expect(agentField().getByText(i18nT(`${K}.agent_roster_retry`))).toBeInTheDocument()
    })

    vi.mocked(api).kirocrewAgents.mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
    fireEvent.click(agentField().getByText(i18nT(`${K}.agent_roster_retry`)))

    await waitFor(() => {
      expect(agentField().getByRole('option', { name: 'kirocrew-crew' })).toBeInTheDocument()
    })
    expect(agentField().queryByText(i18nT(`${K}.agent_roster_failed`))).not.toBeInTheDocument()
  })

  it('keeps the crew\u2019s stale agent selectable while the roster is missing', async () => {
    renderEditor(CREW)
    await screen.findByTestId('crew-editor')
    await waitFor(() => {
      expect(agentField().getByText(i18nT(`${K}.agent_roster_failed`))).toBeInTheDocument()
    })

    // The preservation composes with the error state instead of being replaced
    // by it: the current agent leads the list, selected and visible, so the
    // failure never silently re-points the crew at another agent.
    const stale = agentField().getByRole('option', { name: 'oncall' })
    expect(stale).toHaveAttribute('aria-selected', 'true')
    expect(agentField().getByRole('combobox')).toHaveTextContent('oncall')
  })
})
