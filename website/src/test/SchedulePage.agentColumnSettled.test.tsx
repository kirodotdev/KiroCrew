/**
 * The Schedule table's Agent column resolves a pinned `member_id` through the
 * roster, and WAITS for that roster: before the catalog answers there is
 * nothing to resolve the id through, so rendering the stored value would flash
 * `crew-program-manager` on every visit where a name belongs. Same gate the
 * channel rail applies; this pins the Schedule side of it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

const job = {
  id: 'job-1', name: 'Weekly digest', schedule: 'every 7d', message: 'send digest', enabled: true,
  agent: 'crew-program-manager',
} as CronJob

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    cronFolders: vi.fn().mockResolvedValue([]),
    chatFolders: vi.fn().mockResolvedValue([]),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    models: vi.fn().mockResolvedValue([]),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: 'kirocrew' }),
    kirocrewAgents: vi.fn(),
    agentCatalog: vi.fn(),
  },
}))

const ROSTER = [
  {
    name: 'Crew Program Manager', member_id: 'crew-program-manager', display_name: 'Crew Program Manager',
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: '', source: 'user',
    selection_kind: 'member',
  },
]

describe('SchedulePage agent column waits for the roster', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [job] })
    vi.mocked(api).cronFolders.mockResolvedValue([])
    vi.mocked(api).chatFolders.mockResolvedValue([])
    vi.mocked(api).cronHistoryAll.mockResolvedValue({ runs: [] })
    vi.mocked(api).models.mockResolvedValue([])
    vi.mocked(api).defaultAgent.mockResolvedValue({ default_agent: 'kirocrew' })
    vi.mocked(api).kirocrewAgents.mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
  })

  it('never shows the stored id, then shows the display name once the catalog answers', async () => {
    const { api } = await import('../api/client')
    let release: (value: { agents: typeof ROSTER; default_agent: string }) => void = () => {}
    vi.mocked(api).agentCatalog.mockReturnValue(new Promise(resolve => { release = resolve }))

    renderWithProviders(<SchedulePage />)
    await screen.findByText('Weekly digest', {}, { timeout: 5000 })
    // Roster still in flight: the id must not be on screen.
    expect(screen.queryByText('crew-program-manager')).not.toBeInTheDocument()
    expect(screen.queryByText('Crew Program Manager')).not.toBeInTheDocument()

    release({ agents: ROSTER, default_agent: 'kirocrew' })
    await waitFor(() => expect(screen.getByText('Crew Program Manager')).toBeInTheDocument())
    expect(screen.queryByText('crew-program-manager')).not.toBeInTheDocument()
  })

  it('filters by the display name the Agent column shows, not only the stored id', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).agentCatalog.mockResolvedValue({ agents: ROSTER, default_agent: 'kirocrew' })
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [job, { ...job, id: 'job-2', name: 'Other job', agent: 'kirocrew' } as CronJob],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Crew Program Manager')).toBeInTheDocument())
    expect(screen.getByText('Other job')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('Filter jobs…'), { target: { value: 'program manager' } })
    await waitFor(() => expect(screen.queryByText('Other job')).not.toBeInTheDocument())
    expect(screen.getByText('Weekly digest')).toBeInTheDocument()
  })

  it('keeps holding the label when the catalog fetch fails, instead of printing the id', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).agentCatalog.mockRejectedValue(new Error('catalog unavailable'))

    renderWithProviders(<SchedulePage />)
    await screen.findByText('Weekly digest', {}, { timeout: 5000 })
    // The fetch has settled -- by failing. An empty list is not a roster to
    // resolve through, so the stored id still must not be on screen.
    await waitFor(() => expect(vi.mocked(api).agentCatalog).toHaveBeenCalled())
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(screen.queryByText('crew-program-manager')).not.toBeInTheDocument()
    expect(screen.queryByText('Crew Program Manager')).not.toBeInTheDocument()
  })

  it('never renders a tooltip that is only a separator and the model', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [{ ...job, model: 'sonnet' }] })
    vi.mocked(api).agentCatalog.mockRejectedValue(new Error('catalog unavailable'))

    renderWithProviders(<SchedulePage />)
    await screen.findByText('Weekly digest', {}, { timeout: 5000 })
    await waitFor(() => expect(vi.mocked(api).agentCatalog).toHaveBeenCalled())
    await new Promise(resolve => setTimeout(resolve, 50))
    // With the label withheld, the kind cell's tooltip is the model alone --
    // not " · sonnet" hanging off an empty subject.
    const cell = screen.getByTitle('sonnet')
    expect(cell).toBeInTheDocument()
    expect(document.querySelector('[title^=" · "]')).toBeNull()
  })
})
