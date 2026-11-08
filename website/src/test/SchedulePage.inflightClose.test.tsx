import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation, useNavigationStakeNow } from '../components/NavigationLeaveGuard'
import { ApiError } from '../api/apiError'
import type { CronJob } from '../types'

// The Schedule page's job dialog while a save is in flight (#10307). A POST
// cannot be un-sent, so dismissal is never refused: the dialog says the save
// may still go through, a late answer refreshes the list without touching the
// dialog open by then, a late failure is reported on the page, and a page leave
// is refused while the dialog holds typed work.

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
    createCron: vi.fn(),
    updateCron: vi.fn(),
    models: vi.fn().mockResolvedValue([]),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

/** A create whose answer the test releases by hand. */
function deferCreate(api: { createCron: ReturnType<typeof vi.fn> }) {
  let settle!: { resolve: (v: unknown) => void; reject: (e: unknown) => void }
  api.createCron.mockImplementationOnce(() => new Promise((resolve, reject) => { settle = { resolve, reject } }))
  return () => settle
}

/** Open a blank create, type a job and press Create. */
async function startCreate(name: string) {
  fireEvent.click(await screen.findByRole('button', { name: /Add Job/ }))
  const dialog = await screen.findByRole('dialog')
  fireEvent.change(within(dialog).getByLabelText('Name', { selector: 'input' }), { target: { value: name } })
  fireEvent.change(within(dialog).getByLabelText('Message', { selector: 'textarea' }), { target: { value: 'do it' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
  return dialog
}

/** Reads the published stake (what arms browser Back) and the leave guard's answer. */
function LeaveProbe() {
  const mayLeave = useMayLeaveForNavigation()
  const stakeNow = useNavigationStakeNow()
  const [answer, setAnswer] = useState('')
  return <button type="button" data-testid="leave-probe" onClick={() => setAnswer(`stake=${stakeNow()} leave=${mayLeave()}`)}>{answer}</button>
}

describe('SchedulePage job dialog while a save is in flight', () => {
  beforeEach(async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockReset().mockResolvedValue({ jobs: [mkJob()] })
    vi.mocked(api).createCron.mockReset()
  })

  it('offers Close with the may-still-be-created line, and a late success leaves the dialog open now alone', async () => {
    const { api } = await import('../api/client')
    const settle = deferCreate(vi.mocked(api))
    renderWithProviders(<SchedulePage />)
    const dialog = await startCreate('Morning digest')

    await within(dialog).findByText("Closing won't cancel this — the schedule may still be created.")
    expect(within(dialog).queryByRole('button', { name: 'Cancel' })).toBeNull()
    // By its text: the dialog's corner X is also named Close.
    fireEvent.click(within(dialog).getByText('Close', { selector: 'button' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    // Another dialog is open when the first create answers.
    fireEvent.click(await screen.findByText('Nightly report'))
    await screen.findByRole('dialog', { name: 'Nightly report' })
    const reads = vi.mocked(api).crons.mock.calls.length
    await act(async () => { settle().resolve({}) })

    await waitFor(() => expect(vi.mocked(api).crons.mock.calls.length).toBeGreaterThan(reads))
    expect(screen.getByRole('dialog', { name: 'Nightly report' })).toBeInTheDocument()
  })

  it('reports each create that fails after its dialog closed, saying which and whether it was decided', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).createCron.mockRejectedValueOnce(new ApiError(400, 'Name taken'))
    const refused = deferCreate(vi.mocked(api))
    const unanswered = deferCreate(vi.mocked(api))
    renderWithProviders(<SchedulePage />)

    // A failure while its dialog is open stays in the dialog.
    const open = await startCreate('Inline')
    await within(open).findByText('Name taken')
    expect(screen.queryByTestId('schedule-late-save-error')).toBeNull()
    fireEvent.click(within(open).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    for (const name of ['Morning digest', 'Evening digest']) {
      const dialog = await startCreate(name)
      fireEvent.click(await within(dialog).findByText('Close', { selector: 'button' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    }
    await act(async () => { refused().reject(new ApiError(400, 'Bad schedule')) })
    await act(async () => { unanswered().reject(new TypeError('Failed to fetch')) })

    const notices = await screen.findAllByTestId('schedule-late-save-error')
    expect(notices).toHaveLength(2)
    expect(notices[0]).toHaveTextContent('“Morning digest” wasn\'t saved.')
    expect(notices[0]).toHaveTextContent('Bad schedule')
    expect(notices[1]).toHaveTextContent('“Evening digest” may or may not have been saved — the server didn\'t confirm it.')
    expect(notices[1]).toHaveTextContent('Failed to fetch')

    fireEvent.click(within(notices[0]).getByRole('button', { name: /Dismiss/ }))
    await waitFor(() => expect(screen.getAllByTestId('schedule-late-save-error')).toHaveLength(1))
  })

  it('refuses a page leave only while the dialog holds typed work', async () => {
    renderWithProviders(<NavigationLeaveGuardProvider><SchedulePage /><LeaveProbe /></NavigationLeaveGuardProvider>)
    fireEvent.click(await screen.findByRole('button', { name: /Add Job/ }))
    const dialog = await screen.findByRole('dialog')

    fireEvent.click(screen.getByTestId('leave-probe'))
    expect(screen.getByTestId('leave-probe')).toHaveTextContent('stake=false leave=true')

    fireEvent.change(within(dialog).getByLabelText('Name', { selector: 'input' }), { target: { value: 'Draft' } })
    fireEvent.click(screen.getByTestId('leave-probe'))
    expect(screen.getByTestId('leave-probe')).toHaveTextContent('stake=true leave=false')
  })

  it('keeps an edit on its form while the save is in flight, so its failure is shown there', async () => {
    const { api } = await import('../api/client')
    let fail!: (e: unknown) => void
    vi.mocked(api).updateCron.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))
    const dialog = await screen.findByRole('dialog', { name: 'Nightly report' })
    // The tab row may collapse to a menu at this width; open it if it did.
    const pickTab = async (current: string, next: string) => {
      const toggle = within(dialog).queryByRole('button', { name: current, expanded: false })
      if (toggle) fireEvent.click(toggle)
      fireEvent.click(await within(dialog).findByRole('radio', { name: next }))
    }
    // No form on Logs, so nothing there could report a save.
    await pickTab('Details', 'Logs')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    await pickTab('Logs', 'Details')

    fireEvent.change(within(dialog).getByLabelText('Name', { selector: 'input' }), { target: { value: 'Nightly digest' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    await within(dialog).findByText("Closing won't cancel this — your changes may still be saved.")

    await pickTab('Details', 'Logs')
    expect(within(dialog).getByLabelText('Name', { selector: 'input' })).toBeInTheDocument()
    await act(async () => { fail(new ApiError(500, 'Gateway error')) })
    await within(dialog).findByText('Failed to save')
  })

  it("puts the server's reason on the page when an edit fails after its dialog closed", async () => {
    const { api } = await import('../api/client')
    let fail!: (e: unknown) => void
    vi.mocked(api).updateCron.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
    renderWithProviders(<SchedulePage />)
    fireEvent.click(await screen.findByText('Nightly report'))
    const dialog = await screen.findByRole('dialog', { name: 'Nightly report' })
    fireEvent.change(within(dialog).getByLabelText('Name', { selector: 'input' }), { target: { value: 'Nightly digest' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    fireEvent.click(await within(dialog).findByText('Close', { selector: 'button' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    await act(async () => { fail(new ApiError(400, 'Bad schedule')) })

    const notice = await screen.findByTestId('schedule-late-save-error')
    expect(notice).toHaveTextContent('“Nightly digest” wasn\'t saved.')
    expect(notice).toHaveTextContent('Bad schedule')
  })
})
