/** Tests for the edit form's alert threshold field.
 *
 *  The field shows the job's stored threshold and sends it only when the reader
 *  changed it, like the auto-pause limit beside it.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import type { CronJob } from '../types'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    chatFolders: vi.fn().mockResolvedValue([]),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

const LABEL = 'Send the failure message after N failures in a row'

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'af1', name: 'watcher', message: 'Check the mount.', schedule: '', enabled: true,
    every_secs: 600, auto_pause_after_failures: 5, alert_after_failures: 1, ...overrides,
  } as CronJob
}

function renderEdit(job: CronJob, onSaved = vi.fn()) {
  vi.mocked(api.updateCron).mockResolvedValue({})
  renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={job} />)
  return onSaved
}

describe('alert threshold in the job edit form', () => {
  beforeEach(() => vi.mocked(api.updateCron).mockReset())

  it('shows the stored threshold with a floor of 1', () => {
    renderEdit(makeJob({ alert_after_failures: 3 }))
    const input = screen.getByLabelText(LABEL)
    expect(input).toHaveValue(3)
    expect(input).toHaveAttribute('min', '1')
    expect(input).toHaveAttribute('max', '10000')
  })

  it('sends a changed threshold', async () => {
    const onSaved = renderEdit(makeJob())
    const input = screen.getByLabelText(LABEL)
    await userEvent.clear(input)
    await userEvent.type(input, '2')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('af1', expect.objectContaining({ alert_after_failures: 2 }))
  })

  it('sends nothing for the threshold when it was not changed', async () => {
    const onSaved = renderEdit(makeJob({ alert_after_failures: 2 }))
    await userEvent.type(screen.getByLabelText('Name'), ' renamed')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(vi.mocked(api.updateCron).mock.calls[0][1]).not.toHaveProperty('alert_after_failures')
  })

  it('does not send back an untouched threshold after a refresh brings a newer one', async () => {
    vi.mocked(api.updateCron).mockResolvedValue({})
    const onSaved = vi.fn()
    const job = makeJob({ alert_after_failures: 1 })
    const { rerender } = renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={job} />)
    rerender(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={{ ...job, alert_after_failures: 3 }} />)
    await userEvent.type(screen.getByLabelText('Name'), ' renamed')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(vi.mocked(api.updateCron).mock.calls[0][1]).not.toHaveProperty('alert_after_failures')
  })

  it('refuses 0 without saving', async () => {
    const onSaved = renderEdit(makeJob())
    const input = screen.getByLabelText(LABEL)
    await userEvent.clear(input)
    await userEvent.type(input, '0')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const hint = await screen.findByText('The alert threshold must be a whole number from 1 to 10000.')
    expect(hint).toBeVisible()
    expect(hint.closest('[role="alert"]')).toBeNull()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input.getAttribute('aria-describedby')).toContain('jobform-alert-after-hint')
    expect(api.updateCron).not.toHaveBeenCalled()
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('is hidden when the gateway does not report the threshold', () => {
    renderEdit(makeJob({ alert_after_failures: undefined }))
    expect(screen.queryByLabelText(LABEL)).not.toBeInTheDocument()
  })

  it('is hidden on the create form', () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(screen.queryByLabelText(LABEL)).not.toBeInTheDocument()
  })
})
