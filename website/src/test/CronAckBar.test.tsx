import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import CronAckBar from '../pages/chat/CronAckBar'

vi.mock('../api/client', () => ({
  api: {
    ackCron: vi.fn().mockResolvedValue({ ok: true }),
    deleteNotification: vi.fn().mockResolvedValue({ ok: true }),
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

  it('shows acknowledged after the route saves it', async () => {
    renderWithProviders(<CronAckBar notification={baseNotif} onDone={() => {}} />)
    fireEvent.click(screen.getByText(/Acknowledge/))
    expect(await screen.findByText(/Acknowledged/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it.each([
    ['the route saves nothing without saying why', () => Promise.resolve({ ok: false })],
    ['the request fails', () => Promise.reject(new Error('503'))],
  ])('keeps the button and shows an error when %s', async (_case, reply) => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackCron).mockImplementationOnce(reply)
    renderWithProviders(<CronAckBar notification={baseNotif} onDone={() => {}} />)
    fireEvent.click(screen.getByText(/Acknowledge/))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn't save this acknowledgement/)
    expect(screen.queryByText(/Acknowledged/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Acknowledge/ })).toBeInTheDocument()
  })

  it('says the job is gone and offers Delete when the route cannot find it', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackCron).mockResolvedValueOnce({ ok: false, code: 'job_not_found' })
    const onDone = vi.fn()
    renderWithProviders(<CronAckBar notification={baseNotif} onDone={onDone} />)
    fireEvent.click(screen.getByText(/Acknowledge/))
    expect(await screen.findByRole('alert')).toHaveTextContent(/This job no longer exists/)
    expect(screen.queryByText(/Acknowledged/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Acknowledge$/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Delete/ }))
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce())
  })

  it.each([
    [false, { ok: false, code: 'job_not_found' }],
    [true, { ok: false, code: 'job_not_found' }],
    // The run's merge kept the job (another writer re-scheduled it): its ack is saved.
    [false, { ok: true }],
  ])('offers Done on a one-shot reminder (read: %s), which dismisses it once the store answers %o', async (acked, reply) => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackCron).mockResolvedValueOnce(reply)
    const onDone = vi.fn()
    renderWithProviders(<CronAckBar notification={{ ...baseNotif, one_shot: true, acked }} onDone={onDone} />)
    expect(screen.queryByRole('button', { name: /Acknowledge/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/Acknowledged/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Done/ }))
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce())
    expect(api.ackCron).toHaveBeenCalledWith('job-123', 'Job output here', baseNotif.ts)
    expect(api.deleteNotification).toHaveBeenCalledWith(baseNotif.ts)
  })

  it.each([
    ['the store saves nothing without saying why', () => Promise.resolve({ ok: false })],
    ['the ack request fails', () => Promise.reject(new Error('503'))],
  ])('keeps a one-shot note and deletes nothing when %s', async (_case, reply) => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackCron).mockImplementationOnce(reply)
    const onDone = vi.fn()
    renderWithProviders(<CronAckBar notification={{ ...baseNotif, one_shot: true }} onDone={onDone} />)
    fireEvent.click(screen.getByRole('button', { name: /Done/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn't save this acknowledgement/)
    expect(api.deleteNotification).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Done/ })).toBeInTheDocument()
  })

  it('keeps a one-shot note open and says so when Done fails', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.deleteNotification).mockRejectedValueOnce(new Error('offline'))
    const onDone = vi.fn()
    renderWithProviders(<CronAckBar notification={{ ...baseNotif, one_shot: true }} onDone={onDone} />)
    fireEvent.click(screen.getByRole('button', { name: /Done/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn't delete this notification/)
    expect(onDone).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Done/ })).toBeInTheDocument()
  })

  it('keeps the panel open and says so when Delete fails', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.deleteNotification).mockRejectedValueOnce(new Error('offline'))
    const onDone = vi.fn()
    renderWithProviders(<CronAckBar notification={{ ...baseNotif, acked: true }} onDone={onDone} />)
    fireEvent.click(screen.getByRole('button', { name: /Delete/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Couldn't delete this notification/)
    expect(onDone).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Delete/ })).toBeInTheDocument()
  })
})
