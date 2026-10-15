import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, within, waitFor, fireEvent } from '@testing-library/react'
import { act } from 'react'
import { createTestStore, renderWithProviders } from './helpers'
import { triggerRefresh } from '../store/dashboardSlice'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

// The Schedule list tells a crewmate's own schedule (`member_id` set) from a
// custom-agent job. Its Type cell reads "Crewmate" with the crewmate's face and
// name, linking to the Crewmates page, and a "by crewmate" filter narrows the
// list to one crewmate's schedules. The crewmate comes from the members roster:
// a job's `member_id` is the crewmate's durable id, which is the roster row's
// `slug`. A crewmate job whose crewmate is gone shows its raw id, unlinked.

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Nightly report',
  schedule: 'every 1d',
  message: 'send report',
  enabled: true,
  ...overrides,
} as CronJob)

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    cronFolders: vi.fn().mockResolvedValue([]),
    deleteCron: vi.fn(),
    batchDeleteCron: vi.fn(),
    createCron: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: 'kirocrew', default_template: 'kirocrew' }),
    members: vi.fn(),
  },
}))

const ROSTER = [
  // `slug` is the persisted member_id once the crewmate has one.
  { name: 'radar', slug: 'member-radar-1', slot_key: '', running: false },
  { name: 'scout', slug: 'scout', slot_key: '', running: false },
]

const JOBS = [
  mkJob({ id: 'tpl', name: 'Template job', agent: 'kiro-review' }),
  mkJob({ id: 'radar-1', name: 'Radar triage', member_id: 'member-radar-1' }),
  mkJob({ id: 'scout-1', name: 'Scout sweep', member_id: 'scout' }),
  mkJob({ id: 'gone-1', name: 'Orphan job', member_id: 'member-gone' }),
]

async function renderList() {
  const { api } = await import('../api/client')
  vi.mocked(api).crons.mockResolvedValue({ jobs: JOBS })
  renderWithProviders(<SchedulePage />)
  return screen.findByText('Radar triage')
}

const typeCell = (jobName: string) => {
  const row = screen.getByText(jobName).closest('tr')!
  return row.querySelectorAll('td')[3] as HTMLElement
}

describe('Schedule list mate schedules', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    const { api } = await import('../api/client')
    vi.mocked(api).members.mockResolvedValue({ members: ROSTER })
  })

  it('shows a mate job as Mate with a link to its Mates page', async () => {
    await renderList()
    const cell = typeCell('Radar triage')
    expect(within(cell).getByText('Mate')).toBeInTheDocument()
    const link = await within(cell).findByRole('link', { name: /radar/ })
    expect(link).toHaveAttribute('href', '/members?member=radar')
    expect(cell.getAttribute('title')).toContain('radar')
  })

  it('keeps a template job on Agent and links nothing', async () => {
    await renderList()
    const cell = typeCell('Template job')
    expect(within(cell).getByText('kiro-review')).toBeInTheDocument()
    expect(within(cell).queryByText('Mate')).toBeNull()
    expect(within(cell).queryByRole('link')).toBeNull()
  })

  it('drops a mate filter whose mate no longer owns a listed job', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: JOBS })
    const store = createTestStore()
    renderWithProviders(<SchedulePage />, { store })
    await screen.findByText('Radar triage')
    fireEvent.click(await screen.findByLabelText('Filter by mate'))
    fireEvent.click(await screen.findByRole('option', { name: /radar/ }))
    await waitFor(() => expect(screen.queryByText('Scout sweep')).toBeNull())

    // Radar's last schedule is deleted elsewhere; the list reloads.
    vi.mocked(api).crons.mockResolvedValue({ jobs: JOBS.filter(j => j.id !== 'radar-1') })
    act(() => { store.dispatch(triggerRefresh()) })

    expect(await screen.findByText('Scout sweep')).toBeInTheDocument()
    expect(screen.getByText('Template job')).toBeInTheDocument()
    expect(screen.getByLabelText('Filter by mate')).toHaveTextContent('All mates')
  })

  it('finds a mate job by the display name its row shows', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).members.mockResolvedValue({ members: [
      { ...ROSTER[0], display_name: 'Radar Bot' },
      ROSTER[1],
    ] })
    await renderList()
    await waitFor(() => expect(within(typeCell('Radar triage')).getByRole('link')).toHaveTextContent('Radar Bot'))
    fireEvent.change(screen.getByPlaceholderText('Filter jobs…'), { target: { value: 'Radar Bot' } })
    await waitFor(() => expect(screen.queryByText('Scout sweep')).toBeNull())
    expect(screen.getByText('Radar triage')).toBeInTheDocument()
    expect(typeCell('Radar triage').getAttribute('title')).toContain('Radar Bot')
  })

  it('names nobody when two roster rows share the stored slug', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).members.mockResolvedValue({ members: [
      ...ROSTER,
      { name: 'Radar Legacy', slug: 'member-radar-1', slot_key: '', running: false },
    ] })
    await renderList()
    await waitFor(() => expect(within(typeCell('Scout sweep')).getByRole('link')).toBeInTheDocument())
    const cell = typeCell('Radar triage')
    expect(within(cell).getByText('member-radar-1')).toBeInTheDocument()
    expect(within(cell).queryByRole('link')).toBeNull()
  })

  it('shows the raw id, unlinked, for a mate that is gone', async () => {
    await renderList()
    const cell = typeCell('Orphan job')
    expect(within(cell).getByText('Mate')).toBeInTheDocument()
    expect(within(cell).getByText('member-gone')).toBeInTheDocument()
    expect(within(cell).queryByRole('link')).toBeNull()
  })

  it('reports a failed mate roster, keeps raw ids, and recovers on retry', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).members.mockRejectedValue(new Error('Members request failed (HTTP 500)'))
    await renderList()

    const notice = await screen.findByTestId('schedule-crewmate-roster-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Could not load your mates.')
    expect(notice).toHaveTextContent('Members request failed (HTTP 500)')
    for (const job of JOBS.filter(j => j.member_id)) {
      const cell = typeCell(job.name)
      expect(within(cell).getByText('Mate')).toBeInTheDocument()
      expect(within(cell).getByText(job.member_id!)).toBeInTheDocument()
      expect(within(cell).queryByRole('link')).toBeNull()
    }
    expect(screen.queryByLabelText('Filter by mate')).toBeNull()

    vi.mocked(api).members.mockResolvedValue({ members: ROSTER })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await within(typeCell('Radar triage')).findByRole('link', { name: /radar/ })).toHaveAttribute('href', '/members?member=radar')
    expect(await screen.findByLabelText('Filter by mate')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('schedule-crewmate-roster-error')).toBeNull())
  })

  it('the mate filter narrows the list to one mate', async () => {
    await renderList()
    fireEvent.click(await screen.findByLabelText('Filter by mate'))
    fireEvent.click(await screen.findByRole('option', { name: /radar/ }))
    await waitFor(() => expect(screen.queryByText('Scout sweep')).toBeNull())
    expect(screen.getByText('Radar triage')).toBeInTheDocument()
    expect(screen.queryByText('Template job')).toBeNull()
    expect(screen.queryByText('Orphan job')).toBeNull()
  })

  it('offers no mate filter when no job belongs to a mate', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [JOBS[0]] })
    renderWithProviders(<SchedulePage />)
    await screen.findByText('Template job')
    expect(screen.queryByLabelText('Filter by mate')).toBeNull()
  })
})
