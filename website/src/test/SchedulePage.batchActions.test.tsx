import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor, within, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import { triggerCronRefresh } from '../store/dashboardSlice'
import { batchTargets } from '../components/CronBatchActionsMenu'
import type { CronJob } from '../types'

/**
 * The batch bar applies a row-menu action to every checked job, calling the
 * per-job endpoint once per job and skipping jobs the action would not change.
 */

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Job',
  schedule: 'every 1d',
  message: 'm',
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
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

const JOBS = [
  mkJob({ id: 'a', name: 'Alpha job' }),
  mkJob({ id: 'b', name: 'Beta job', enabled: false, strict_schedule: true }),
  mkJob({ id: 'c', name: 'Gamma job', is_running: true }),
]

const check = (name: string) =>
  fireEvent.click(within(screen.getByText(name).closest('tr') as HTMLElement).getByRole('checkbox'))

async function renderChecked(names: string[]) {
  const { api } = await import('../api/client')
  vi.mocked(api).crons.mockResolvedValue({ jobs: JOBS })
  renderWithProviders(<SchedulePage />)
  await waitFor(() => expect(screen.getByText('Alpha job')).toBeInTheDocument())
  names.forEach(check)
  return api
}

function openMenu() {
  fireEvent.keyDown(screen.getByLabelText('Actions for the checked jobs'), { key: 'Enter' })
}

const menuItem = (label: string) =>
  screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith(label)) as HTMLElement

describe('batchTargets', () => {
  const running = new Set<string>()
  it('keeps only the jobs each action changes', () => {
    const ids = (a: Parameters<typeof batchTargets>[0]) => batchTargets(a, JOBS, running).map(j => j.id)
    expect(ids('pause')).toEqual(['a', 'c'])
    expect(ids('resume')).toEqual(['b'])
    expect(ids('strict_on')).toEqual(['a', 'c'])
    expect(ids('strict_off')).toEqual(['b'])
    expect(ids('run')).toEqual(['a'])
  })

  it('skips a job whose run was just started from this page', () => {
    expect(batchTargets('run', JOBS, new Set(['a']))).toEqual([])
  })
})

describe('SchedulePage batch actions', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('pauses only the checked jobs that are not paused, and reloads once', async () => {
    const api = await renderChecked(['Alpha job', 'Beta job'])
    const loadsBefore = vi.mocked(api).crons.mock.calls.length
    openMenu()
    fireEvent.click(menuItem('Pause'))

    await waitFor(() => expect(api.toggleCron).toHaveBeenCalledTimes(1))
    expect(api.toggleCron).toHaveBeenCalledWith('a', false)
    await waitFor(() => expect(vi.mocked(api).crons.mock.calls.length).toBe(loadsBefore + 1))
    await waitFor(() => expect(screen.queryByText(/selected/)).not.toBeInTheDocument())
  })

  it('turns strict schedule off through the per-job update', async () => {
    const api = await renderChecked(['Alpha job', 'Beta job'])
    openMenu()
    fireEvent.click(menuItem('Allow small random delay'))
    await waitFor(() => expect(api.updateCron).toHaveBeenCalledWith('b', { strict_schedule: false }))
    expect(api.updateCron).toHaveBeenCalledTimes(1)
  })

  it('names the effect of each timing item and counts jobs, not bare numbers', async () => {
    await renderChecked(['Alpha job', 'Beta job'])
    openMenu()
    expect(menuItem('Run at exact time')).toHaveTextContent('Run at exact timeStart exactly on schedulechanges 1 job')
    expect(menuItem('Allow small random delay')).toHaveTextContent('Allow small random delayStart a little late to spread the loadchanges 1 job')
    expect(menuItem('Pause')).toHaveTextContent('Pausechanges 1 job')
    expect(screen.getByRole('menu').textContent).not.toMatch(/jitter|strict/i)
  })

  it('asks before Run now and runs only enabled, idle jobs', async () => {
    const api = await renderChecked(['Alpha job', 'Beta job', 'Gamma job'])
    openMenu()
    fireEvent.click(menuItem('Run Now'))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Run 1 job now?')).toBeInTheDocument()
    const skipped = within(dialog).getByTestId('schedule-run-skipped')
    expect(within(skipped).getAllByRole('listitem').map(li => li.textContent))
      .toEqual(['Beta job (paused)', 'Gamma job (already running)'])
    expect(api.runCron).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Run Now' }))

    await waitFor(() => expect(api.runCron).toHaveBeenCalledTimes(1))
    expect(api.runCron).toHaveBeenCalledWith('a')
  })

  it('lists at most five skipped jobs in the Run now confirm, then the rest as a count', async () => {
    const { api } = await import('../api/client')
    const paused = Array.from({ length: 7 }, (_, i) => mkJob({ id: `p${i}`, name: `Paused ${i}`, enabled: false }))
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob({ id: 'a', name: 'Alpha job' }), ...paused] })
    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Alpha job')).toBeInTheDocument())
    check('Alpha job'); paused.forEach(j => check(j.name))
    openMenu()
    fireEvent.click(menuItem('Run Now'))
    const skipped = within(await screen.findByRole('dialog')).getByTestId('schedule-run-skipped')
    expect(within(skipped).getAllByRole('listitem')).toHaveLength(5)
    expect(skipped).toHaveTextContent('+2 more')
  })

  it('skips a job paused while the Run now dialog was open', async () => {
    const { api } = await import('../api/client')
    const twoIdle = [mkJob({ id: 'a', name: 'Alpha job' }), mkJob({ id: 'd', name: 'Delta job' })]
    vi.mocked(api).crons.mockResolvedValue({ jobs: twoIdle })
    const { store } = renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Alpha job')).toBeInTheDocument())
    check('Alpha job'); check('Delta job')
    openMenu()
    fireEvent.click(menuItem('Run Now'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Run 2 jobs now?')).toBeInTheDocument()

    vi.mocked(api).crons.mockResolvedValue({ jobs: [twoIdle[0], { ...twoIdle[1], enabled: false }] })
    const loads = vi.mocked(api).crons.mock.calls.length
    act(() => { store.dispatch(triggerCronRefresh()) })
    await waitFor(() => expect(vi.mocked(api).crons.mock.calls.length).toBeGreaterThan(loads))

    fireEvent.click(within(dialog).getByRole('button', { name: 'Run Now' }))
    await waitFor(() => expect(api.runCron).toHaveBeenCalledTimes(1))
    expect(api.runCron).toHaveBeenCalledWith('a')
  })

  it('keeps the batch bar to Clear and one Actions menu', async () => {
    await renderChecked(['Alpha job'])
    const bar = screen.getByText('1 selected').parentElement as HTMLElement
    expect(within(bar).getAllByRole('button').map(b => b.getAttribute('aria-label') ?? b.textContent))
      .toEqual(['Clear', 'Actions for the checked jobs'])
  })

  it('moves and deletes from the Actions menu', async () => {
    await renderChecked(['Alpha job'])
    openMenu()
    expect(screen.getByText('Move to folder')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('menuitem', { name: /^Delete 1 selected$/ }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('Delete 1 scheduled job?')
  })

  it('keeps failed jobs checked and names the count and the reason', async () => {
    const api = await renderChecked(['Alpha job', 'Gamma job'])
    vi.mocked(api).toggleCron.mockImplementation(async (id: string) => {
      if (id === 'c') throw new Error('job is already running')
      return {}
    })
    openMenu()
    fireEvent.click(menuItem('Pause'))

    expect(await screen.findByText('Pause: 1 of 2 jobs failed (job is already running). Failed jobs stay selected.')).toBeInTheDocument()
    const rowCheckbox = (name: string) =>
      within(screen.getByText(name).closest('tr') as HTMLElement).getByRole('checkbox')
    expect(rowCheckbox('Gamma job')).toBeChecked()
    expect(rowCheckbox('Alpha job')).not.toBeChecked()
  })

  it('counts a refusal reported in the response body as a failure and names its reason', async () => {
    const api = await renderChecked(['Beta job'])
    vi.mocked(api).toggleCron.mockResolvedValue({ error: 'not your job' })
    openMenu()
    fireEvent.click(menuItem('Resume'))
    expect(await screen.findByText('Resume: 1 of 1 job failed (not your job). Failed jobs stay selected.')).toBeInTheDocument()
  })

  it('omits the reason when the failure carries none', async () => {
    const api = await renderChecked(['Alpha job', 'Gamma job'])
    vi.mocked(api).toggleCron.mockImplementation(async (id: string) => {
      if (id === 'c') throw 'bare rejection'  // not an Error: no message to show
      return {}
    })
    openMenu()
    fireEvent.click(menuItem('Pause'))
    expect(await screen.findByText('Pause: 1 of 2 jobs failed. Failed jobs stay selected.')).toBeInTheDocument()
  })

  it('disables an action no checked job would change', async () => {
    await renderChecked(['Beta job'])
    openMenu()
    expect(menuItem('Pause')).toHaveAttribute('data-disabled')
    expect(menuItem('Resume')).not.toHaveAttribute('data-disabled')
  })
})
