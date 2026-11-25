/**
 * Test: tunnelDisplay() — pure state→label/color/tooltip mapping for the
 * Overview "Tunnel" stat tile (tunnel for mobile dashboard access).
 *
 * The backend GET /api/tunnel/status returns one of six states. This maps
 * each to a user-facing label, a semantic color class, and a tooltip.
 * Pins the contract so the Overview tile stays consistent with the other
 * tiles' color conventions (accent=ok, warn=transient, danger=error).
 *
 * Fork adaptation: tunnelDisplay() returns null when the status is
 * null/unfetched OR the state is 'disabled'. The tile then renders nothing,
 * so the public edition (permanently 'disabled') shows zero pixels and there
 * is no em-dash flash before the first poll resolves.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { tunnelDisplay, TunnelStatus as TunnelStatusTile } from '../components/TunnelStatus'
import type { TunnelStatus } from '../api/client'

vi.mock('../api/client', () => ({
  api: { tunnelStatus: vi.fn() },
}))

const base: TunnelStatus = { state: 'disabled', url: '', error: '', uptime: 0, reconnect_attempt: 0 }

describe('tunnelDisplay', () => {
  it('returns null when status is null (still loading — no em-dash flash)', () => {
    expect(tunnelDisplay(null)).toBeNull()
  })

  it('connected → accent color with url + uptime tooltip', () => {
    const d = tunnelDisplay({ ...base, state: 'connected', url: 'https://x-kirocrew.example.tunnels.dev', uptime: 3720 })
    expect(d).not.toBeNull()
    expect(d!.value).toBe('Connected')
    expect(d!.colorClass).toBe('text-accent')
    expect(d!.tooltip).toContain('https://x-kirocrew.example.tunnels.dev')
    expect(d!.tooltip).toContain('up 1h 2m')
  })

  it('connected with no uptime → tooltip is just the url', () => {
    const d = tunnelDisplay({ ...base, state: 'connected', url: 'https://x.example.tunnels.dev', uptime: 0 })
    expect(d!.tooltip).toBe('https://x.example.tunnels.dev')
  })

  it('connected with a reported error → warn label, url in tooltip', () => {
    const d = tunnelDisplay({ ...base, state: 'connected', url: 'https://x.example.tunnels.dev', uptime: 120, error: 'auth-expired' })
    expect(d!.value).toBe('Connected with error')
    expect(d!.colorClass).toBe('text-warn')
    expect(d!.tooltip).toContain('https://x.example.tunnels.dev')
  })

  it('connected with an empty error → stays plain green Connected', () => {
    const d = tunnelDisplay({ ...base, state: 'connected', url: 'https://x.example.tunnels.dev', error: '' })
    expect(d!.value).toBe('Connected')
    expect(d!.colorClass).toBe('text-accent')
    expect(d!.tooltip).toBe('https://x.example.tunnels.dev')
  })

  it('reconnecting with an error → unchanged: warn, attempt + error in tooltip', () => {
    const d = tunnelDisplay({ ...base, state: 'reconnecting', reconnect_attempt: 2, error: 'net-down' })
    expect(d!.value).toBe('Reconnecting…')
    expect(d!.colorClass).toBe('text-warn')
    expect(d!.tooltip).toContain('net-down')
  })

  it('starting → warn color', () => {
    const d = tunnelDisplay({ ...base, state: 'starting' })
    expect(d!.value).toBe('Connecting…')
    expect(d!.colorClass).toBe('text-warn')
  })

  it('reconnecting → warn color with attempt count in tooltip', () => {
    const d = tunnelDisplay({ ...base, state: 'reconnecting', reconnect_attempt: 3 })
    expect(d!.value).toBe('Reconnecting…')
    expect(d!.colorClass).toBe('text-warn')
    expect(d!.tooltip).toContain('3')
  })

  it('error → danger color with error message in tooltip', () => {
    const d = tunnelDisplay({ ...base, state: 'error', error: 'token-expired' })
    expect(d!.value).toBe('Error')
    expect(d!.colorClass).toBe('text-danger')
    expect(d!.tooltip).toBe('token-expired')
  })

  it('stopped → muted', () => {
    expect(tunnelDisplay({ ...base, state: 'stopped' })!.value).toBe('Stopped')
  })

  it('disabled → null (public edition renders nothing)', () => {
    expect(tunnelDisplay(base)).toBeNull()
  })

  it.each(['reconnecting', 'starting', 'stopped'] as const)(
    'a default-only personal Crew %s → null (quiet until it can host)',
    (state) => {
      expect(tunnelDisplay({ ...base, state, error: 'not signed in', reason: 'personal_default' })).toBeNull()
    },
  )

  it('a default-only personal Crew in a final error → still shown as Error', () => {
    const d = tunnelDisplay({ ...base, state: 'error', error: 'upgrade Crew', reason: 'personal_default' })
    expect(d!.value).toBe('Error')
    expect(d!.colorClass).toBe('text-danger')
  })

  it('a default-only personal Crew that connected → shown as Connected', () => {
    const d = tunnelDisplay({ ...base, state: 'connected', url: 'https://x.example.tunnels.dev', reason: 'personal_default' })
    expect(d!.value).toBe('Connected')
  })
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

describe('<TunnelStatus /> tile', () => {
  beforeEach(() => { vi.clearAllMocks(); qc.clear() })

  it('renders the "Connected" label when the tunnel is up', async () => {
    const { api } = await import('../api/client')
    ;(api.tunnelStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      state: 'connected', url: 'https://x-kirocrew.example.tunnels.dev', error: '', uptime: 120, reconnect_attempt: 0,
    })
    render(<TunnelStatusTile />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('Connected')).toBeInTheDocument())
    expect(screen.getByText('Tunnel')).toBeInTheDocument()
  })

  it('renders a connected tunnel\'s reported error through ErrorNotice', async () => {
    const { api } = await import('../api/client')
    ;(api.tunnelStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      state: 'connected', url: 'https://x.example.tunnels.dev', error: 'auth-expired', uptime: 120, reconnect_attempt: 0,
    })
    render(<TunnelStatusTile />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('Connected with error')).toBeInTheDocument())
    expect(screen.getByTestId('tunnel-status-error')).toHaveTextContent('auth-expired')
  })

  it.each([
    ['error', 'Error'],
    ['reconnecting', 'Reconnecting…'],
  ] as const)('renders a %s tunnel\'s reported error through ErrorNotice', async (state, label) => {
    const { api } = await import('../api/client')
    ;(api.tunnelStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      state, url: '', error: 'net-down', uptime: 0, reconnect_attempt: 1,
    })
    render(<TunnelStatusTile />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText(label)).toBeInTheDocument())
    expect(screen.getByTestId('tunnel-status-error')).toHaveTextContent('net-down')
  })

  it.each([
    // Quiet: a personal Crew the relay hosts only by default that cannot host yet.
    ['reconnecting', 'personal_default', false],
    ['starting', 'personal_default', false],
    ['stopped', 'personal_default', false],
    // Loud: a final failure (a relay that refuses this version) still says to upgrade.
    ['error', 'personal_default', true],
    // Loud: someone chose (or a team key or workload): today's red alert.
    ['reconnecting', '', true],
  ] as const)('%s with reason %j → error notice shown: %s', async (state, reason, shown) => {
    const { api } = await import('../api/client')
    ;(api.tunnelStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      state, url: '', error: 'not signed in', uptime: 0, reconnect_attempt: 1, reason,
    })
    const { container } = render(<TunnelStatusTile />, { wrapper: Wrapper })
    await waitFor(() => expect(api.tunnelStatus).toHaveBeenCalled())
    if (shown) {
      await waitFor(() => expect(screen.getByTestId('tunnel-status-error')).toHaveAttribute('role', 'alert'))
    } else {
      // The status has arrived, not merely the pre-fetch empty render.
      await waitFor(() => expect(qc.getQueryData(['tunnel-status'])).toBeTruthy())
      await act(async () => { await Promise.resolve() })
      expect(screen.queryByTestId('tunnel-status-error')).not.toBeInTheDocument()
      expect(container).toBeEmptyDOMElement()
    }
  })

  it('shows no error notice for a connected tunnel without an error', async () => {
    const { api } = await import('../api/client')
    ;(api.tunnelStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      state: 'connected', url: 'https://x.example.tunnels.dev', error: '', uptime: 120, reconnect_attempt: 0,
    })
    render(<TunnelStatusTile />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('Connected')).toBeInTheDocument())
    expect(screen.queryByTestId('tunnel-status-error')).not.toBeInTheDocument()
  })

  it('renders nothing when the tunnel is disabled', async () => {
    const { api } = await import('../api/client')
    ;(api.tunnelStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      state: 'disabled', url: '', error: '', uptime: 0, reconnect_attempt: 0,
    })
    const { container } = render(<TunnelStatusTile />, { wrapper: Wrapper })
    await waitFor(() => expect(api.tunnelStatus).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByText('Tunnel')).not.toBeInTheDocument()
  })

  it('stops polling once the tunnel reports disabled (no perpetual no-op loop)', async () => {
    vi.useFakeTimers()
    try {
      const { api } = await import('../api/client')
      ;(api.tunnelStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
        state: 'disabled', url: '', error: '', uptime: 0, reconnect_attempt: 0,
      })
      render(<TunnelStatusTile />, { wrapper: Wrapper })
      // Drain the initial fetch.
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const initialCalls = (api.tunnelStatus as ReturnType<typeof vi.fn>).mock.calls.length
      expect(initialCalls).toBeGreaterThan(0)
      // Well past several 15s intervals — a disabled tunnel must not re-poll.
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000 * 5) })
      expect((api.tunnelStatus as ReturnType<typeof vi.fn>).mock.calls.length).toBe(initialCalls)
    } finally {
      vi.useRealTimers()
    }
  })
})
