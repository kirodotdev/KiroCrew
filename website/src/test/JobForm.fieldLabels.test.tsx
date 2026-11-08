/** Accessible names and states in the job form's schedule and row fields.
 *
 *  Every field must have a name a screen reader announces: a placeholder is
 *  not one, since it vanishes on input and assistive tech may skip it. The
 *  weekday toggles must expose whether each day is on as state, and show it
 *  by more than the accent color.
 */

import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import type { CronJob } from '../types'

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
    id: 'fl1', name: 'poller', message: 'Check the queue.', schedule: '', enabled: true,
    every_secs: 3600, ...overrides,
  } as CronJob
}

describe('job form field names', () => {
  it('names the name, message and channel fields in the horizontal row', async () => {
    renderWithProviders(<JobForm layout="horizontal" agents={[]} onSaved={() => {}} job={makeJob({ channel: 'C123' })} />)
    expect(await screen.findByRole('textbox', { name: 'Name' })).toHaveValue('poller')
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('Check the queue.')
    expect(screen.getByRole('textbox', { name: 'Channel ID' })).toHaveValue('C123')
  })

  it.each(['vertical', 'horizontal'] as const)('names the interval amount in the %s layout', async layout => {
    renderWithProviders(<JobForm layout={layout} agents={[]} onSaved={() => {}} job={makeJob({ every_secs: 7200 })} />)
    expect(await screen.findByRole('spinbutton', { name: 'Interval amount' })).toHaveValue(2)
  })

  it('names the cron expression field', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ every_secs: undefined, cron_expr: '*/5 9-17 * * *' })} />)
    expect(await screen.findByRole('textbox', { name: 'Cron expression' })).toHaveValue('*/5 9-17 * * *')
  })
})

describe('job form weekday toggles', () => {
  function renderWeekly() {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ every_secs: undefined, cron_expr: '30 9 * * 1,3' })} />)
  }

  it('names the time of day field', async () => {
    renderWeekly()
    expect(await screen.findByLabelText('Time of day')).toHaveValue('09:30')
  })

  it('groups the days and exposes each one as a pressed or unpressed toggle', async () => {
    renderWeekly()
    const group = await screen.findByRole('group', { name: 'Days of the week' })
    const days = within(group).getAllByRole('button')
    expect(days).toHaveLength(7)
    // Monday-first grid: Mon and Wed are on, the rest are off.
    expect(days.map(d => d.getAttribute('aria-pressed'))).toEqual(['true', 'false', 'true', 'false', 'false', 'false', 'false'])
  })

  it('shows a check mark on selected days only, not just a color', async () => {
    renderWeekly()
    const days = within(await screen.findByRole('group', { name: 'Days of the week' })).getAllByRole('button')
    expect(days.map(d => d.querySelector('svg') !== null)).toEqual([true, false, true, false, false, false, false])
  })

  it('flips the pressed state and the check mark when a day is toggled', async () => {
    renderWeekly()
    const days = within(await screen.findByRole('group', { name: 'Days of the week' })).getAllByRole('button')
    await userEvent.click(days[1])
    expect(days[1]).toHaveAttribute('aria-pressed', 'true')
    expect(days[1].querySelector('svg')).not.toBeNull()
    await userEvent.click(days[0])
    expect(days[0]).toHaveAttribute('aria-pressed', 'false')
    expect(days[0].querySelector('svg')).toBeNull()
  })
})
