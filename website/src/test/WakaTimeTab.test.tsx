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
import { MemoryRouter, parsePath, useLocation } from 'react-router-dom'

import WakaTimeTab from '../pages/overview/WakaTimeTab'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'

const desktop = vi.hoisted(() => ({ isElectron: false }))
vi.mock('../lib/electron', async (orig) => ({
  ...await orig<typeof import('../lib/electron')>(),
  get isElectron() { return desktop.isElectron },
}))

vi.mock('../api/client', async (orig) => {
  const actual = await orig<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      wakatimeStats: vi.fn(),
      wakatimeExportDownload: vi.fn(),
      wakatimeOauthStatus: vi.fn(),
      wakatimeOauthAuthorize: vi.fn(),
      wakatimeOauthDisconnect: vi.fn(),
    },
  }
})

const wakatimeStats = api.wakatimeStats as unknown as ReturnType<typeof vi.fn>
const wakatimeExportDownload = api.wakatimeExportDownload as unknown as ReturnType<typeof vi.fn>
const wakatimeOauthStatus = api.wakatimeOauthStatus as unknown as ReturnType<typeof vi.fn>
const wakatimeOauthAuthorize = api.wakatimeOauthAuthorize as unknown as ReturnType<typeof vi.fn>
const wakatimeOauthDisconnect = api.wakatimeOauthDisconnect as unknown as ReturnType<typeof vi.fn>

function LocationProbe() {
  const { search, state } = useLocation()
  return <>
    <output data-testid="router-search">{search}</output>
    <output data-testid="router-state">{JSON.stringify(state)}</output>
  </>
}

function mount(path = '/settings/overview?view=wakatime', state?: { wakatimeOauth?: string } | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[{ ...parsePath(path), state }]}>
        <WakaTimeTab />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  desktop.isElectron = false
  wakatimeStats.mockReset()
  wakatimeExportDownload.mockReset()
  wakatimeOauthStatus.mockReset()
  wakatimeOauthAuthorize.mockReset()
  wakatimeOauthDisconnect.mockReset()
  // Default: an unconnected, unconfigured OAuth app, so the stats-focused tests
  // render without the Connect button needing per-test setup.
  wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: false })
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

  it('headline reads the include-other total so it matches WakaTime.com and the Languages bars', async () => {
    // The classified-only total_seconds (3600 = 1h) omits the Other bucket.
    // The headline must report the include-other total (10800 = 3h), the figure
    // WakaTime.com's own headline and the Languages bars use.
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: {
        total_seconds: 3600,
        daily_average: 3600,
        total_seconds_including_other_language: 10800,
        daily_average_including_other_language: 10800,
        languages: [{ name: 'Python', total_seconds: 10800 }],
        projects: [{ name: 'noscere', total_seconds: 10800 }],
      },
    })
    mount()
    await screen.findByText('Python')
    expect(screen.getAllByText('3h').length).toBeGreaterThan(0)
    expect(screen.queryByText('1h')).not.toBeInTheDocument()
  })

  it('falls back to the classified total when the include-other field is null (free account)', async () => {
    // Free WakaTime accounts return null for the include-other variant. The
    // headline must fall back to total_seconds rather than render zero.
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: {
        total_seconds: 7200,
        daily_average: 7200,
        total_seconds_including_other_language: null,
        daily_average_including_other_language: null,
        languages: [{ name: 'Python', total_seconds: 7200 }],
        projects: [],
      },
    })
    mount()
    await screen.findByText('Python')
    expect(screen.getAllByText('2h').length).toBeGreaterThan(0)
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
    expect(screen.getByText(/Set wakatime.enabled to true in \$KIROCREW_HOME\/config.json.*then.*set the WakaTime API key under Used automatically by Kiro Crew/)).toBeInTheDocument()
    expect(screen.queryByText(/Add a secret named WAKATIME_API_KEY/)).not.toBeInTheDocument()
  })

  it('shows a no-activity message when configured but the range is empty', async () => {
    wakatimeStats.mockResolvedValue({ configured: true, range: 'last_7_days', stats: { total_seconds: 0, languages: [], projects: [] } })
    mount()
    expect(await screen.findByText(/no activity for these dates/i)).toBeInTheDocument()
  })

  it('asks to reconnect, and keeps Connect on screen, when WakaTime refuses the read', async () => {
    wakatimeStats.mockRejectedValue(new ApiError(
      502,
      'WakaTime refused the read. Reconnect WakaTime in Settings.',
      JSON.stringify({ error: 'WakaTime refused the read. Reconnect WakaTime in Settings.', code: 'reconnect_required' }),
    ))
    wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: true })
    mount()
    expect(await screen.findByText('Reconnect WakaTime')).toBeInTheDocument()
    expect(screen.queryByText('WakaTime is unreachable')).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /connect with wakatime/i })).toBeInTheDocument()
  })

  it('offers Connect beside Disconnect when a stored grant is refused', async () => {
    // A revoked grant or a failed refresh leaves the token entry, so status says
    // connected while WakaTime refuses the read; reconnecting must be one click.
    wakatimeStats.mockRejectedValue(new ApiError(
      502,
      'WakaTime refused the read. Reconnect WakaTime in Settings.',
      JSON.stringify({ error: 'WakaTime refused the read. Reconnect WakaTime in Settings.', code: 'reconnect_required' }),
    ))
    wakatimeOauthStatus.mockResolvedValue({ connected: true, client_configured: true })
    mount()
    expect(await screen.findByRole('button', { name: /^disconnect$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /connect with wakatime/i })).toBeInTheDocument()
  })

  it('reads an upstream outage as unreachable, not as a reconnect', async () => {
    wakatimeStats.mockRejectedValue(new ApiError(
      502,
      'WakaTime is unreachable or the API key was rejected',
      JSON.stringify({ error: 'WakaTime is unreachable or the API key was rejected', code: 'upstream_unavailable' }),
    ))
    mount()
    expect(await screen.findByText('WakaTime is unreachable')).toBeInTheDocument()
    expect(screen.queryByText('Reconnect WakaTime')).not.toBeInTheDocument()
  })

  it('shows a failed OAuth connect even when an API key already serves stats', async () => {
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: { total_seconds: 3600, daily_average: 1800, languages: [{ name: 'Python', total_seconds: 3600 }], projects: [] },
    })
    mount('/settings/overview?view=wakatime&wakatime_oauth=error&reason=exchange_failed')
    await screen.findByText('Python')
    expect(screen.getByText('Could not connect your WakaTime account. Please try again.')).toBeInTheDocument()
    // The marker is removed through the router, so router navigation cannot restore it.
    await waitFor(() => expect(screen.getByTestId('router-search').textContent).toBe('?view=wakatime'))
  })

  it('confirms a connect on the no-API-key card, not only on the stats view', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    wakatimeOauthStatus.mockResolvedValue({ connected: true, client_configured: true })
    mount('/settings/overview?view=wakatime&wakatime_oauth=connected')
    expect(await screen.findByText('WakaTime account connected.')).toBeInTheDocument()
  })

  it('strips the OAuth callback markers without saving the result in router state', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    mount('/settings/overview?view=wakatime&wakatime_oauth=connected&reason=unused')
    await waitFor(() => expect(screen.getByTestId('router-search').textContent).toBe('?view=wakatime'))
    expect(await screen.findByText('WakaTime account connected.')).toBeInTheDocument()
    expect(JSON.parse(screen.getByTestId('router-state').textContent!)).toBeNull()
  })

  it.each([false, true])('clears the OAuth notice after Disconnect succeeds (configured=%s)', async (configured) => {
    wakatimeStats.mockResolvedValue({ configured })
    wakatimeOauthStatus.mockResolvedValue({ connected: true, client_configured: true })
    wakatimeOauthDisconnect.mockResolvedValue({ disconnected: true })
    mount('/settings/overview?view=wakatime&wakatime_oauth=connected')
    expect(await screen.findByText('WakaTime account connected.')).toBeInTheDocument()
    const disconnect = await screen.findByRole('button', { name: /^disconnect$/i })
    wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: true })
    fireEvent.click(disconnect)
    await screen.findByRole('button', { name: /connect with wakatime/i })
    expect(wakatimeOauthDisconnect).toHaveBeenCalledOnce()
    expect(screen.queryByText('WakaTime account connected.')).not.toBeInTheDocument()
  })

  it('keeps the OAuth notice when Disconnect fails', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    wakatimeOauthStatus.mockResolvedValue({ connected: true, client_configured: true })
    wakatimeOauthDisconnect.mockRejectedValue(new Error('Disconnect failed'))
    mount('/settings/overview?view=wakatime&wakatime_oauth=connected')
    expect(await screen.findByText('WakaTime account connected.')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: /^disconnect$/i }))
    expect(await screen.findByText('Disconnect failed')).toBeInTheDocument()
    expect(screen.getByText('WakaTime account connected.')).toBeInTheDocument()
  })

  it('does not restore the OAuth notice when the stripped location remounts', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    const first = mount('/settings/overview?view=wakatime&wakatime_oauth=connected')
    await screen.findByText('WakaTime account connected.')
    await waitFor(() => expect(screen.getByTestId('router-search').textContent).toBe('?view=wakatime'))
    const search = screen.getByTestId('router-search').textContent!
    const state = JSON.parse(screen.getByTestId('router-state').textContent!)
    first.unmount()
    mount(`/settings/overview${search}`, state)
    await screen.findByText(/connect wakatime/i)
    expect(screen.queryByText('WakaTime account connected.')).not.toBeInTheDocument()
  })

  it('ignores an OAuth result left in history state when there is no URL marker', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    mount('/settings/overview?view=wakatime', { wakatimeOauth: 'connected' })
    await screen.findByText(/connect wakatime/i)
    expect(screen.queryByText('WakaTime account connected.')).not.toBeInTheDocument()
  })

  it('shows a failed Connect request through the error notice', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: true })
    wakatimeOauthAuthorize.mockRejectedValue(new Error('Start the WakaTime connection from the configured dashboard address'))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /connect with wakatime/i }))
    const msg = await screen.findByText('Start the WakaTime connection from the configured dashboard address')
    expect(msg.closest('[role="alert"]')).not.toBeNull()
  })

  it('refetches when the range control changes', async () => {
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: { total_seconds: 3600, daily_average: 1800, languages: [{ name: 'Python', total_seconds: 3600 }], projects: [] },
    })
    mount()
    await screen.findByText('Python')
    expect(wakatimeStats).toHaveBeenCalledWith('last_7_days')
    fireEvent.click(screen.getByRole('radio', { name: /last 30 days/i }))
    await waitFor(() => expect(wakatimeStats).toHaveBeenCalledWith('last_30_days'))
  })

  it('offers Connect with WakaTime and navigates to the authorize URL', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: true })
    wakatimeOauthAuthorize.mockResolvedValue({ authorize_url: 'https://wakatime.com/oauth/authorize?x=1' })
    const assign = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, assign } })
    try {
      mount()
      const btn = await screen.findByRole('button', { name: /connect with wakatime/i })
      fireEvent.click(btn)
      await waitFor(() => expect(assign).toHaveBeenCalledWith('https://wakatime.com/oauth/authorize?x=1'))
      expect(wakatimeOauthAuthorize).toHaveBeenCalledOnce()
      expect(btn).toBeEnabled()
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })

  it.each([false, true])('keeps desktop Connect in the dashboard and explains browser setup (rejected=%s)', async (rejected) => {
    desktop.isElectron = true
    if (rejected) {
      wakatimeStats.mockRejectedValue(new ApiError(502, 'Reconnect WakaTime', JSON.stringify({ code: 'reconnect_required' })))
    } else {
      wakatimeStats.mockResolvedValue({ configured: false })
    }
    wakatimeOauthStatus.mockResolvedValue({ connected: rejected, client_configured: true })
    wakatimeOauthAuthorize.mockResolvedValue({ authorize_url: 'https://wakatime.com/oauth/authorize?x=1' })
    const assign = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, assign } })
    try {
      mount()
      const btn = await screen.findByRole('button', { name: /connect with wakatime/i })
      fireEvent.click(btn)
      const notice = await screen.findByText('Open this dashboard in a web browser to connect WakaTime. Once connected there, the desktop app uses the same account.')
      // Guidance, not a failure: muted status text, never an ErrorNotice alert.
      expect(notice).toHaveAttribute('role', 'status')
      expect(notice.closest('[role="alert"]')).toBeNull()
      expect(wakatimeOauthAuthorize).not.toHaveBeenCalled()
      expect(assign).not.toHaveBeenCalled()
      expect(btn).toBeEnabled()
      expect(screen.queryByText('Opening WakaTime…')).not.toBeInTheDocument()
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original })
    }
  })

  it('shows the setup hint instead of Connect when the OAuth app is not configured', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: false })
    mount()
    await screen.findByText(/connect wakatime/i)
    expect(await screen.findByText(/Set wakatime.oauth_client_id in config.json/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /connect with wakatime/i })).not.toBeInTheDocument()
  })

  it('links to the configured address when Connect starts from another one', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: true })
    const refuse = (dashboard_url: string) =>
      wakatimeOauthAuthorize.mockRejectedValue(new ApiError(
        409,
        'Start the WakaTime connection from the configured dashboard address',
        JSON.stringify({ error: 'Start the WakaTime connection from the configured dashboard address', code: 'wrong_host', dashboard_url }),
      ))
    refuse('https://kiro.example.com/settings/overview?view=wakatime')
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /connect with wakatime/i }))
    const link = await screen.findByRole('link', { name: /open the dashboard at its main address/i })
    expect(link).toHaveAttribute('href', 'https://kiro.example.com/settings/overview?view=wakatime')
    // The raw server sentence is replaced, not shown beside the link.
    expect(screen.queryByText(/configured dashboard address/i)).not.toBeInTheDocument()
    // The next action clears the notice, so its own failure is the one shown.
    wakatimeOauthAuthorize.mockRejectedValue(new Error('gateway restarting'))
    fireEvent.click(screen.getByRole('button', { name: /connect with wakatime/i }))
    expect(await screen.findByText('gateway restarting')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /open the dashboard/i })).not.toBeInTheDocument()
    cleanup()

    // An address that is not http(s) never becomes a link.
    refuse('javascript:alert(1)')
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /connect with wakatime/i }))
    expect(await screen.findByText(/configured dashboard address/i)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /open the dashboard/i })).not.toBeInTheDocument()
  })

  it('keeps a failed Connect visible when the status then reports no OAuth app', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    let resolveStatus: (v: { connected: boolean; client_configured: boolean }) => void = () => {}
    wakatimeOauthStatus.mockReturnValue(new Promise((resolve) => (resolveStatus = resolve)))
    wakatimeOauthAuthorize.mockRejectedValue(new Error('WakaTime OAuth is not configured'))
    mount()
    // Connect is clickable while the status read is still pending.
    fireEvent.click(await screen.findByRole('button', { name: /connect with wakatime/i }))
    await waitFor(() => expect(wakatimeOauthAuthorize).toHaveBeenCalled())
    resolveStatus({ connected: false, client_configured: false })
    expect(await screen.findByText(/Set wakatime.oauth_client_id in config.json/)).toBeInTheDocument()
    expect(screen.getByText('WakaTime OAuth is not configured')).toBeInTheDocument()
  })

  it('reads the OAuth status again when the panel reopens, so a finished setup shows Connect', async () => {
    wakatimeStats.mockResolvedValue({ configured: false })
    // The dashboard's query client never marks data stale on its own.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const open = () =>
      render(
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={['/settings/overview?view=wakatime']}>
            <WakaTimeTab />
          </MemoryRouter>
        </QueryClientProvider>,
      )
    const first = open()
    await screen.findByText(/Set wakatime.oauth_client_id in config.json/)
    first.unmount()
    wakatimeOauthStatus.mockResolvedValue({ connected: false, client_configured: true })
    open()
    expect(await screen.findByRole('button', { name: /connect with wakatime/i })).toBeInTheDocument()
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

  it.each([false, true])('shows Disconnect when connected and clears tokens on click (desktop=%s)', async (isElectron) => {
    desktop.isElectron = isElectron
    wakatimeStats.mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: { total_seconds: 3600, daily_average: 1800, languages: [{ name: 'Python', total_seconds: 3600 }], projects: [] },
    })
    wakatimeOauthStatus.mockResolvedValue({ connected: true, client_configured: true })
    wakatimeOauthDisconnect.mockResolvedValue({ disconnected: true })
    mount()
    await screen.findByText('Python')
    const btn = await screen.findByRole('button', { name: /^disconnect$/i })
    fireEvent.click(btn)
    await waitFor(() => expect(wakatimeOauthDisconnect).toHaveBeenCalled())
  })

  it('labels only the running action while a disconnect is in flight', async () => {
    wakatimeStats.mockRejectedValue(new ApiError(502, 'Reconnect WakaTime', JSON.stringify({ code: 'reconnect_required' })))
    wakatimeOauthStatus.mockResolvedValue({ connected: true, client_configured: true })
    let finish: (v: { disconnected: boolean }) => void = () => {}
    wakatimeOauthDisconnect.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    mount()
    const connectBtn = await screen.findByRole('button', { name: /connect with wakatime/i })
    fireEvent.click(await screen.findByRole('button', { name: /^disconnect$/i }))
    expect(await screen.findByRole('button', { name: 'Disconnecting…' })).toBeDisabled()
    expect(connectBtn).toHaveTextContent('Connect with WakaTime')
    expect(connectBtn).toBeDisabled()
    expect(screen.queryByText('Opening WakaTime…')).not.toBeInTheDocument()
    finish({ disconnected: true })
    await waitFor(() => expect(screen.queryByText('Disconnecting…')).not.toBeInTheDocument())
  })
})
