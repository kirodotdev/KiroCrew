/** Tests for the job form's run time limit (`timeout_secs`).
 *
 *  The limit is how long one run may go on before the cron service stops it
 *  and records it as timed out. Two properties matter. A limit the job already
 *  has is shown and survives an unrelated edit. A limit set elsewhere in
 *  seconds, which the minutes field can only show rounded, is never rewritten
 *  by saving something else.
 */

import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm, { buildBody, parseJobDefaults, DEFAULT_RUN_LIMIT_SECS } from '../components/JobForm'
import type { CronJob } from '../types'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'tl1', name: 'poller', message: 'Check the timestamp.', schedule: '', enabled: true,
    every_secs: 3600, ...overrides,
  } as CronJob
}

describe('the run time limit in the job form', () => {
  it('starts a new job at the service default', () => {
    expect(DEFAULT_RUN_LIMIT_SECS).toBe(1800)
    expect(parseJobDefaults(undefined).timeLimitMin).toBe(30)
  })

  it('shows the limit an existing job has', () => {
    expect(parseJobDefaults(makeJob({ timeout_secs: 7200 })).timeLimitMin).toBe(120)
    // A list payload from a gateway without the field reads as the default.
    expect(parseJobDefaults(makeJob()).timeLimitMin).toBe(30)
  })

  it('sends a changed limit in seconds', () => {
    const f = { ...parseJobDefaults(makeJob()), timeLimitMin: 90 }
    expect(buildBody(f, 'UTC', () => {}, true)!.timeout_secs).toBe(5400)
  })

  it('sends nothing for an unchanged limit, on create and edit alike', () => {
    expect(buildBody({ ...parseJobDefaults(undefined), name: 'n', message: 'm' }, 'UTC', () => {})!.timeout_secs).toBeUndefined()
    expect(buildBody(parseJobDefaults(makeJob({ timeout_secs: 7200 })), 'UTC', () => {}, true)!.timeout_secs).toBeUndefined()
  })

  it('leaves a limit set in seconds alone when something else is edited', () => {
    // 1000 s shows as 17 minutes; saving a rename must not store 1020 s.
    const f = { ...parseJobDefaults(makeJob({ timeout_secs: 1000 })), name: 'renamed' }
    expect(f.timeLimitMin).toBe(17)
    expect(buildBody(f, 'UTC', () => {}, true)!.timeout_secs).toBeUndefined()
  })

  it('offers the limit on a script job too: it bounds every kind of run', () => {
    const f = { ...parseJobDefaults(makeJob({ script: '~/.kiro/crew/crons/f.py:run', message: '' })), timeLimitMin: 45 }
    expect(buildBody(f, 'UTC', () => {}, true)!.timeout_secs).toBe(2700)
  })

  it.each(['vertical', 'horizontal'] as const)('edits the limit through the %s layout', async layout => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout={layout} agents={[]} onSaved={onSaved} job={makeJob({ timeout_secs: 3600 })} />)
    const field = await screen.findByLabelText('Time limit in minutes')
    expect(field).toHaveValue(60)
    fireEvent.change(field, { target: { value: '240' } })
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('tl1', expect.objectContaining({ timeout_secs: 14400 }))
  })

  it('keeps the field inside the range the service accepts', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob()} />)
    const field = await screen.findByLabelText('Time limit in minutes')
    fireEvent.change(field, { target: { value: '5000' } })
    fireEvent.blur(field)
    expect(field).toHaveValue(1440)
    fireEvent.change(field, { target: { value: '0' } })
    fireEvent.blur(field)
    expect(field).toHaveValue(1)
  })

  it('reads the whole number the field holds, exponent and fraction included', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob()} />)
    const field = await screen.findByLabelText('Time limit in minutes')
    // A number input accepts exponent notation: '1e2' is 100 minutes, not 1.
    fireEvent.change(field, { target: { value: '1e2' } })
    fireEvent.blur(field)
    expect(field).toHaveValue(100)
    fireEvent.change(field, { target: { value: '90.4' } })
    fireEvent.blur(field)
    expect(field).toHaveValue(90)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('tl1', expect.objectContaining({ timeout_secs: 5400 }))
  })

  it('lets the field be cleared and retyped', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ timeout_secs: 3600 })} />)
    const field = await screen.findByLabelText('Time limit in minutes')
    await userEvent.clear(field)
    // An empty field is a draft, not a 1-minute limit.
    expect(field).toHaveValue(null)
    await userEvent.type(field, '45')
    expect(field).toHaveValue(45)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('tl1', expect.objectContaining({ timeout_secs: 2700 }))
  })

  it('restores the saved limit when the field is left empty', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ timeout_secs: 3600 })} />)
    const field = await screen.findByLabelText('Time limit in minutes')
    await userEvent.clear(field)
    fireEvent.blur(field)
    expect(field).toHaveValue(60)
  })

  it('does not send a stale limit after another tab changed it', async () => {
    // This tab opened the job at 60 minutes; another tab then saved 120. The job
    // re-fetches under the open form, and a rename here must not store 60 again.
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockClear()
    vi.mocked(api.updateCron).mockResolvedValue({})
    const props = { layout: 'vertical' as const, agents: [], onSaved }
    const { rerender } = renderWithProviders(<JobForm {...props} job={makeJob({ timeout_secs: 3600 })} />)
    const field = await screen.findByLabelText('Time limit in minutes')
    rerender(<JobForm {...props} job={makeJob({ timeout_secs: 7200 })} />)
    // The untouched field follows the stored value instead of showing the old one.
    expect(field).toHaveValue(120)
    const name = screen.getByLabelText('Name')
    await userEvent.clear(name)
    await userEvent.type(name, 'renamed')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    const body = vi.mocked(api.updateCron).mock.calls[0][1] as Record<string, unknown>
    expect(body.name).toBe('renamed')
    expect(body).not.toHaveProperty('timeout_secs')
  })

  it('keeps an edit the user made when the stored limit moves under it', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockClear()
    vi.mocked(api.updateCron).mockResolvedValue({})
    const props = { layout: 'vertical' as const, agents: [], onSaved }
    const { rerender } = renderWithProviders(<JobForm {...props} job={makeJob({ timeout_secs: 3600 })} />)
    const field = await screen.findByLabelText('Time limit in minutes')
    fireEvent.change(field, { target: { value: '90' } })
    rerender(<JobForm {...props} job={makeJob({ timeout_secs: 7200 })} />)
    expect(field).toHaveValue(90)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('tl1', expect.objectContaining({ timeout_secs: 5400 }))
  })
})
