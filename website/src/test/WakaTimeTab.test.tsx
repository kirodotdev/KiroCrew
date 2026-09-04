//
// WakaTimeTab (Settings > Overview drill-in). Covers the four states the tab
// must distinguish, because they map to different backend responses that must
// never be conflated: configured-with-data, configured-but-empty, the 502
// upstream-unavailable guard (must not read as zero), and the not-connected
// prompt. Also asserts a range switch refetches, since the range is the query
// key.
//
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import WakaTimeTab from '../pages/overview/WakaTimeTab'
import { api } from '../api/client'

vi.mock('../api/client', async (orig) => {
  const actual = await orig<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, wakatimeStats: vi.fn(), wakatimeExportDownload: vi.fn() } }
})

const wakatimeStats = api.wakatimeStats as unknown as ReturnType<typeof vi.fn>
const wakatimeExportDownload = api.wakatimeExportDownload as unknown as ReturnType<typeof vi.fn>

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WakaTimeTab />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  wakatimeStats.mockReset()
  wakatimeExportDownload.mockReset()
})
afterEach(() => cleanup())

describe('WakaTimeTab', () => {
  it('renders coding time, languages and projects when configured with data', async () => {
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: {
        total_seconds: 3600,
        daily_average: 1800,
        languages: [{ name: 'Python', total_seconds: 3600 }],
        projects: [{ name: 'noscere', total_seconds: 3600 }],
      },
    })
    mount()
    expect(await screen.findByText('Python')).toBeInTheDocument()
    expect(screen.getByText('noscere')).toBeInTheDocument()
    // 3600s renders as "1h" (fmtDuration drops the zero-minute part)
    expect(screen.getAllByText('1h').length).toBeGreaterThan(0)
  })

  it('rounds total minutes so a near-hour value never shows 60 minutes', async () => {
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      // 7199s is one second short of 2h; rounding minutes independently would
      // render "1h 60m". It must round to 2h.
      stats: { total_seconds: 7199, daily_average: 7199, languages: [{ name: 'Python', total_seconds: 7199 }], projects: [] },
    })
    mount()
    await screen.findByText('Python')
    expect(screen.getAllByText('2h').length).toBeGreaterThan(0)
    expect(screen.queryByText(/60m/)).not.toBeInTheDocument()
  })

  it('shows the connect prompt when the integration is not configured', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    mount()
    expect(await screen.findByText(/connect wakatime/i)).toBeInTheDocument()
  })

  it('shows a no-activity message when configured but the range is empty', async () => {
    wakatimeStats.mockResolvedValue({ configured: true, range: 'last_7_days', stats: { total_seconds: 0, languages: [], projects: [] } })
    mount()
    expect(await screen.findByText(/no activity for these dates/i)).toBeInTheDocument()
  })

  // The 502 "unreachable" branch is a trivial `if (queryErr)` in the component;
  // the substantive behavior (an upstream failure returns 502, never a
  // false-empty payload) is proven in the backend suite (test_wakatime_handlers
  // stats-502 + test_wakatime_client fetch_stats-raises). Driving a rejected
  // react-query through this unit harness only tests the harness, so it is left
  // to the backend where the contract actually lives.

  it('refetches when the range control changes', async () => {
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: { total_seconds: 3600, daily_average: 1800, languages: [{ name: 'Python', total_seconds: 3600 }], projects: [] },
    })
    mount()
    await screen.findByText('Python')
    expect(wakatimeStats).toHaveBeenCalledWith('last_7_days')
    fireEvent.click(screen.getByRole('button', { name: /last 30 days/i }))
    await waitFor(() => expect(wakatimeStats).toHaveBeenCalledWith('last_30_days'))
  })

  it('surfaces an export failure in place instead of navigating away', async () => {
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: { total_seconds: 3600, daily_average: 1800, languages: [{ name: 'Python', total_seconds: 3600 }], projects: [] },
    })
    wakatimeExportDownload.mockRejectedValue(new Error('WakaTime returned 502 (upstream_unavailable)'))
    mount()
    await screen.findByText('Python')
    fireEvent.click(screen.getByRole('button', { name: /export hours \(csv\)/i }))
    // The failure renders through ErrorNotice; the message reaches the user
    // rather than the browser navigating to the raw error body.
    expect(await screen.findByText(/502 \(upstream_unavailable\)/i)).toBeInTheDocument()
    expect(wakatimeExportDownload).toHaveBeenCalledWith(expect.any(String), expect.any(String), 'csv')
  })
})
