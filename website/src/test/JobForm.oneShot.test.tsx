/** A one-shot ("at") job has no cron expression and no interval. The edit form
 *  must show its stored run time and leave the schedule out of the save, so the
 *  PATCH keeps the run time instead of failing on an empty cron field (#25898). */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm, { buildBody, parseJobDefaults } from '../components/JobForm'
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

const oneShot = {
  id: 'os1', name: 'reminder', message: 'Ping me.', enabled: true,
  schedule: 'at 09:00 AM PST, Nov 2', cron_expr: null, every_secs: null,
  timezone: 'America/Los_Angeles',
} as CronJob

describe('one-shot job in the edit form', () => {
  it('reads a one-shot job as its own mode, not cron', () => {
    expect(parseJobDefaults(oneShot).schedMode).toBe('once')
  })

  it('leaves the schedule and timezone out of the body', () => {
    const setError = vi.fn()
    const body = buildBody(parseJobDefaults(oneShot), 'UTC', setError, true)
    expect(setError).not.toHaveBeenCalled()
    expect(body).not.toBeNull()
    expect(body).not.toHaveProperty('cron')
    expect(body).not.toHaveProperty('every')
    expect(body).not.toHaveProperty('timezone')
  })

  it('shows the stored run time and saves without a schedule', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} job={oneShot} />)
    expect(await screen.findByTestId('jobform-one-shot-schedule')).toHaveTextContent('at 09:00 AM PST, Nov 2')
    expect(screen.queryByPlaceholderText('0 9 * * 1-5')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    const sent = vi.mocked(api.updateCron).mock.calls[0][1] as Record<string, unknown>
    expect(sent).not.toHaveProperty('cron')
    expect(sent).not.toHaveProperty('timezone')
  })
})
