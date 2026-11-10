import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobLogsView from '../components/JobLogsView'
import ExecutionsView from '../components/ExecutionsView'

// Both cron history pagers hide while the next page loads, then disable Next on
// the last page and Prev on the first. Each has exactly two pages here.
const { crons, cronHistory, cronHistoryAll } = vi.hoisted(() => ({ crons: vi.fn(), cronHistory: vi.fn(), cronHistoryAll: vi.fn() }))
vi.mock('../api/client', () => ({ api: { crons, cronHistory, cronHistoryAll, cronRunDetail: vi.fn() } }))

const run = (n: number) => ({ job_id: 'a', job_name: 'Digest', run_id: `run-${n}`, status: 'success' as const, started_at: 1_790_000_000, duration_ms: 1200, trigger: 'manual' as const })

describe('cron history pager focus', () => {
  it('keeps keyboard focus on the job log pager when Next or Prev goes disabled', async () => {
    cronHistory.mockImplementation(async (_jobId: string, offset: number) => ({ runs: [run(offset)], total: 20 }))
    renderWithProviders(<JobLogsView jobId="a" />)
    expect(await screen.findByText('1 / 2')).toBeInTheDocument()
    // The pager is hidden while each page loads, so read the buttons fresh.
    const next = () => screen.getByRole('button', { name: /Next/ })
    const prev = () => screen.getByRole('button', { name: /Prev/ })
    act(() => { next().focus() })
    fireEvent.click(next())
    expect(await screen.findByText('2 / 2')).toBeInTheDocument()
    expect(next()).toBeDisabled()
    await waitFor(() => expect(document.activeElement).toBe(prev()))
    fireEvent.click(prev())
    expect(await screen.findByText('1 / 2')).toBeInTheDocument()
    expect(prev()).toBeDisabled()
    await waitFor(() => expect(document.activeElement).toBe(next()))
  })

  it('keeps keyboard focus on the executions pager when Next or Prev goes disabled', async () => {
    crons.mockResolvedValue({ jobs: [{ id: 'a', name: 'Digest' }] })
    // A page of 20 plus one more row says "there is a next page"; offset 20 has one row.
    cronHistoryAll.mockImplementation(async ({ offset }: { offset: number }) => ({
      runs: offset === 0 ? Array.from({ length: 21 }, (_, i) => run(i)) : [run(20)],
    }))
    renderWithProviders(<ExecutionsView />)
    expect(await screen.findByText('Page 1')).toBeInTheDocument()
    const next = () => screen.getByRole('button', { name: 'Next' })
    const prev = () => screen.getByRole('button', { name: 'Prev' })
    act(() => { next().focus() })
    fireEvent.click(next())
    expect(await screen.findByText('Page 2')).toBeInTheDocument()
    expect(next()).toBeDisabled()
    await waitFor(() => expect(document.activeElement).toBe(prev()))
    fireEvent.click(prev())
    expect(await screen.findByText('Page 1')).toBeInTheDocument()
    expect(prev()).toBeDisabled()
    await waitFor(() => expect(document.activeElement).toBe(next()))
  })
})
