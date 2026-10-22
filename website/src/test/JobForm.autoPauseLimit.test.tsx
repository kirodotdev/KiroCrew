/** Tests for the edit form's auto-pause limit field.
 *
 *  The field shows the job's stored limit and sends it only when the reader
 *  changed it: an unrelated save that resent the limit would rewrite it, and a
 *  0 or high limit writes an audit row on every write.
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

const LABEL = 'Auto-pause after N failures in a row'

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'ap1', name: 'poller', message: 'Check the timestamp.', schedule: '', enabled: true,
    every_secs: 3600, auto_pause_after_failures: 5, ...overrides,
  } as CronJob
}

function renderEdit(job: CronJob, onSaved = vi.fn()) {
  vi.mocked(api.updateCron).mockResolvedValue({})
  renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={job} />)
  return onSaved
}

describe('auto-pause limit in the job edit form', () => {
  beforeEach(() => vi.mocked(api.updateCron).mockReset())

  it('shows the stored limit, including 0 for never pause', () => {
    renderEdit(makeJob({ auto_pause_after_failures: 0 }))
    const input = screen.getByLabelText(LABEL)
    expect(input).toHaveValue(0)
    expect(input).toHaveAttribute('min', '0')
    expect(input).toHaveAttribute('max', '10000')
  })

  it('sends a changed limit', async () => {
    const onSaved = renderEdit(makeJob())
    const input = screen.getByLabelText(LABEL)
    await userEvent.clear(input)
    await userEvent.type(input, '12')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('ap1', expect.objectContaining({ auto_pause_after_failures: 12 }))
  })

  it('sends nothing for the limit when it was not changed', async () => {
    const onSaved = renderEdit(makeJob({ auto_pause_after_failures: 0 }))
    await userEvent.type(screen.getByLabelText('Name'), ' renamed')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(vi.mocked(api.updateCron).mock.calls[0][1]).not.toHaveProperty('auto_pause_after_failures')
  })

  it('does not send back an untouched limit after a refresh brings a newer one', async () => {
    vi.mocked(api.updateCron).mockResolvedValue({})
    const onSaved = vi.fn()
    const job = makeJob({ auto_pause_after_failures: 5 })
    const { rerender } = renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={job} />)
    // Another tab saved 0; the crons refresh hands the same mounted form the newer job.
    rerender(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={{ ...job, auto_pause_after_failures: 0 }} />)
    await userEvent.type(screen.getByLabelText('Name'), ' renamed')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(vi.mocked(api.updateCron).mock.calls[0][1]).not.toHaveProperty('auto_pause_after_failures')
  })

  it('refuses an out-of-range limit without saving', async () => {
    const onSaved = renderEdit(makeJob())
    const input = screen.getByLabelText(LABEL)
    await userEvent.clear(input)
    await userEvent.type(input, '10001')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const hint = await screen.findByText('The auto-pause limit must be a whole number from 0 to 10000.')
    expect(hint).toBeVisible()
    // A validation hint, not a failure banner: no alert role anywhere.
    expect(hint.closest('[role="alert"]')).toBeNull()
    expect(screen.queryAllByRole('alert').some(el => el.textContent?.includes('auto-pause limit'))).toBe(false)
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input.getAttribute('aria-describedby')).toContain('jobform-auto-pause-hint')
    expect(api.updateCron).not.toHaveBeenCalled()
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('is hidden when the gateway does not report the limit', () => {
    renderEdit(makeJob({ auto_pause_after_failures: undefined }))
    expect(screen.queryByLabelText(LABEL)).not.toBeInTheDocument()
  })

  it('is hidden on the create form', () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(screen.queryByLabelText(LABEL)).not.toBeInTheDocument()
  })
})
