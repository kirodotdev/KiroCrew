import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

// Covers the arm -> confirm -> delete -> revert state machine added in
//. This logic is on a destructive, irreversible action and
// had two real bugs (premature button re-enable before await load(),
// and confirmDeleteId not resetting on a failed delete) caught by code review
// during review -- these tests lock in both fixes.

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
    deleteCron: vi.fn(),
    batchDeleteCron: vi.fn(),
    createCron: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    syncKirocrewAgents: vi.fn().mockResolvedValue({}),
  },
}))

describe('SchedulePage delete button state machine', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('arms on first click, deletes on second click', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).deleteCron.mockResolvedValue({})

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    const deleteBtn = screen.getByRole('button', { name: 'Delete' })
    expect(api.deleteCron).not.toHaveBeenCalled()

    // First click arms the row -- button swaps label, no API call yet.
    fireEvent.click(deleteBtn)
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()
    expect(api.deleteCron).not.toHaveBeenCalled()

    // After delete, refresh the list to empty so the row disappears.
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    // Second click (now "Confirm") actually deletes.
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(api.deleteCron).toHaveBeenCalledWith('job-1'))
  })

  it('reverts confirm state back to Delete if the delete call fails', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).deleteCron.mockRejectedValue(new Error('boom'))

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const confirmBtn = await screen.findByRole('button', { name: 'Confirm' })
    fireEvent.click(confirmBtn)

    await waitFor(() => expect(api.deleteCron).toHaveBeenCalled())
    // Bug fix under test: even on failure, the button must revert out of
    // "Confirm" (previously it stayed stuck with no way to re-arm).
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument())
    expect(screen.getByText(/boom/)).toBeInTheDocument()
  })

  it('auto-reverts the armed state after the timeout if not confirmed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [mkJob()] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()

    await vi.advanceTimersByTimeAsync(3100)

    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(api.deleteCron).not.toHaveBeenCalled()
    vi.useRealTimers()
  })

  it('arming a different row disarms the previously armed row', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ id: 'job-1', name: 'Nightly report' }), mkJob({ id: 'job-2', name: 'Weekly digest' })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    const deleteButtons = screen.getAllByRole('button', { name: 'Delete' })
    fireEvent.click(deleteButtons[0])
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeInTheDocument()

    // Arming row 2 must disarm row 1 -- only one row confirmable at a time.
    const remainingDeleteButtons = screen.getAllByRole('button', { name: 'Delete' })
    fireEvent.click(remainingDeleteButtons[remainingDeleteButtons.length - 1])

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Confirm' })).toHaveLength(1)
    })
    expect(api.deleteCron).not.toHaveBeenCalled()
  })
})

describe('SchedulePage batch select + bulk delete', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const twoJobs = [
    mkJob({ id: 'job-1', name: 'Nightly report' }),
    mkJob({ id: 'job-2', name: 'Weekly digest' }),
  ]

  it('selecting rows shows the batch bar and deleting requires typing delete', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: twoJobs })
    vi.mocked(api).batchDeleteCron.mockResolvedValue({ ok: true, deleted: ['job-1', 'job-2'], failed: [] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    // Select both rows via their per-row checkboxes.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Nightly report' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Weekly digest' }))
    expect(screen.getByText('2 selected')).toBeInTheDocument()

    // Open the confirm modal — the delete button is disarmed until "delete" is typed.
    fireEvent.click(screen.getByRole('button', { name: /Delete 2 selected/ }))
    const modal = await screen.findByRole('dialog')
    expect(modal).toBeInTheDocument()
    const armBtn = screen.getByRole('button', { name: 'Delete 2' })
    expect(armBtn).toBeDisabled()
    expect(api.batchDeleteCron).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/Type/, { selector: 'input' }), { target: { value: 'delete' } })
    expect(armBtn).not.toBeDisabled()

    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })
    fireEvent.click(armBtn)
    await waitFor(() => expect(api.batchDeleteCron).toHaveBeenCalledWith(['job-1', 'job-2']))
    // Modal closes and selection clears on full success.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps failed ids selected and surfaces the failure count', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: twoJobs })
    vi.mocked(api).batchDeleteCron.mockResolvedValue({ ok: false, deleted: ['job-1'], failed: ['job-2'] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Nightly report')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Nightly report' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Weekly digest' }))
    fireEvent.click(screen.getByRole('button', { name: /Delete 2 selected/ }))
    await screen.findByRole('dialog')
    fireEvent.change(screen.getByLabelText(/Type/, { selector: 'input' }), { target: { value: 'delete' } })

    // job-1 deletes; job-2 fails and stays behind after reload.
    vi.mocked(api).crons.mockResolvedValue({ jobs: [twoJobs[1]] })
    fireEvent.click(screen.getByRole('button', { name: 'Delete 2' }))

    await waitFor(() => expect(api.batchDeleteCron).toHaveBeenCalledWith(['job-1', 'job-2']))
    // Modal stays open with the error; the failed job remains selected for retry.
    expect(await screen.findByText('1 of 2 jobs could not be deleted')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Select Weekly digest' })).toBeChecked())
  })
})

describe('SchedulePage empty-state preset cards', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the 4 pre-canned schedule cards when there are no jobs', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)

    await waitFor(() => expect(screen.getByText('Dependency Guardian')).toBeInTheDocument())
    expect(screen.getByText('Nightly Build Watch')).toBeInTheDocument()
    expect(screen.getByText('Error Digest')).toBeInTheDocument()
    expect(screen.getByText('Standup Brief')).toBeInTheDocument()
  })

  it('clicking a preset card opens the create panel with the prompt + name prefilled', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Error Digest')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Error Digest'))

    const nameInput = (await screen.findByLabelText('Name')) as HTMLInputElement
    expect(nameInput.value).toBe('Error Digest')
    const msgInput = screen.getByLabelText('Message') as HTMLTextAreaElement
    expect(msgInput.value).toContain('production errors')
  })

  it('weekly preset (Dependency Guardian) creates a Monday cron', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({ jobs: [] })
    vi.mocked(api).createCron.mockResolvedValue({})

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Dependency Guardian')).toBeInTheDocument())

    // Open the prefilled create panel, then save via the normal create path.
    fireEvent.click(screen.getByText('Dependency Guardian'))
    const createBtn = await screen.findByRole('button', { name: 'Create' })
    fireEvent.click(createBtn)

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const body = vi.mocked(api).createCron.mock.calls[0][0] as Record<string, unknown>
    // Pins JobForm's grid weekday convention (Mon=1): the Dependency Guardian
    // preset (weekDays: [1], 06:00) must map to a Monday cron. If JobForm's
    // day-numbering changes, this fails loudly instead of silently shifting.
    expect(body.cron).toBe('0 6 * * 1')
  })
})
