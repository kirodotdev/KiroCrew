import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import CronAckBar from '../pages/chat/CronAckBar'

vi.mock('../api/client', () => ({
  api: {
    ackCron: vi.fn().mockResolvedValue({}),
  },
}))

const baseNotif = {
  kind: 'cron',
  title: 'Cron result',
  body: 'Job output here',
  ts: '2025-01-01T00:00:00Z',
  job_id: 'job-123',
}

describe('CronAckBar', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('shows acknowledge button when not acked', () => {
    renderWithProviders(<CronAckBar notification={baseNotif} onDone={() => {}} />)
    expect(screen.getByText(/Acknowledge/)).toBeInTheDocument()
  })

  it('shows acknowledged state when already acked', () => {
    renderWithProviders(<CronAckBar notification={{ ...baseNotif, acked: true }} onDone={() => {}} />)
    expect(screen.getByText(/Acknowledged/)).toBeInTheDocument()
  })

  it('calls api.ackCron on acknowledge click', async () => {
    const { api } = await import('../api/client')
    renderWithProviders(<CronAckBar notification={baseNotif} onDone={() => {}} />)
    fireEvent.click(screen.getByText(/Acknowledge/))
    await waitFor(() => {
      expect(api.ackCron).toHaveBeenCalledWith('job-123', 'Job output here', baseNotif.ts)
    })
  })
})
