import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobLogsView from '../components/JobLogsView'
import { api } from '../api/client'

// A script job that polls every few minutes ends nearly every run in Skip(), so
// its Logs tab buries the runs that acted under pages of idle rows. "Hide idle
// runs" asks the server to leave them out before paging, says how many it hid,
// and is remembered for the next job opened.

const run = (id: string, summary: string) => ({ run_id: id, status: 'success', started_at: 1_700_000_000, trigger: 'scheduled', summary })
// job-busy: 12 runs that acted and 11 idle ones — three pages unfiltered, two filtered.
const busyActed = Array.from({ length: 12 }, (_, i) => run(`a${i}`, `acted ${i}`))
const busyRuns = [busyActed[0], ...Array.from({ length: 11 }, (_, i) => run(`b${i + 1}`, `idle ${i + 1}`)), ...busyActed.slice(1)]

vi.mock('../api/client', () => ({
  api: {
    cronHistory: vi.fn(async (job: string, offset = 0, limit = 10, hideIdle?: boolean) => {
      if (job === 'job-busy') {
        const runs = hideIdle ? busyActed : busyRuns
        return { runs: runs.slice(offset, offset + limit), total: runs.length, idle_hidden: hideIdle ? 11 : 0 }
      }
      if (job === 'job-shrink') {
        // The filtered total drops from 11 to 10 between the page 1 and page 2 reads.
        return offset === 0
          ? { runs: Array.from({ length: 10 }, (_, i) => run(`s${i}`, `acted ${i}`)), total: 11, idle_hidden: 5 }
          : { runs: [], total: 10, idle_hidden: 6 }
      }
      return hideIdle
        ? { runs: [run('r2', 'acted')], total: 1, idle_hidden: 98 }
        : { runs: [run('r1', 'idle tick'), run('r2', 'acted')], total: 2, idle_hidden: 0 }
    }),
  },
}))

describe('JobLogsView hide idle runs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('hides idle runs on request, shows the count and remembers the choice', async () => {
    const view = renderWithProviders(<JobLogsView jobId="job-1" canHideIdle />)
    expect(await screen.findByText('idle tick')).toBeInTheDocument()
    const toggle = screen.getByRole('checkbox', { name: 'Hide idle runs' })
    expect(toggle).not.toBeChecked()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-1', 0, 10, false)

    fireEvent.click(toggle)
    expect(await screen.findByText('98 idle runs hidden')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('idle tick')).not.toBeInTheDocument())
    expect(screen.getByText('acted')).toBeInTheDocument()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-1', 0, 10, true)

    view.unmount()
    renderWithProviders(<JobLogsView jobId="job-2" canHideIdle />)
    expect(await screen.findByRole('checkbox', { name: 'Hide idle runs' })).toBeChecked()
    await waitFor(() => expect(api.cronHistory).toHaveBeenLastCalledWith('job-2', 0, 10, true))
  })

  it('offers no toggle and hides nothing for a job that cannot Skip', async () => {
    localStorage.setItem('kc-cron-logs-hide-idle', '1')
    renderWithProviders(<JobLogsView jobId="job-1" />)
    expect(await screen.findByText('idle tick')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-1', 0, 10, false)
  })

  it('returns to page 1 when another tab turns the filter on', async () => {
    renderWithProviders(<JobLogsView jobId="job-busy" canHideIdle />)
    expect(await screen.findByText('1 / 3')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    expect(await screen.findByText('idle 11')).toBeInTheDocument()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-busy', 10, 10, false)

    localStorage.setItem('kc-cron-logs-hide-idle', '1')
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'kc-cron-logs-hide-idle', newValue: '1', storageArea: localStorage })) })
    expect(await screen.findByText('acted 0')).toBeInTheDocument()
    expect(screen.getByText('1 / 2')).toBeInTheDocument()
    expect(api.cronHistory).toHaveBeenLastCalledWith('job-busy', 0, 10, true)
  })

  it('moves back to the last page when the filtered total shrinks under it', async () => {
    localStorage.setItem('kc-cron-logs-hide-idle', '1')
    renderWithProviders(<JobLogsView jobId="job-shrink" canHideIdle />)
    expect(await screen.findByText('1 / 2')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    await waitFor(() => expect(api.cronHistory).toHaveBeenCalledWith('job-shrink', 10, 10, true))
    expect(await screen.findByText('acted 0')).toBeInTheDocument()
  })
})
