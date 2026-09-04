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
import { render, screen, waitFor } from '@testing-library/react'
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
      await vi.advanceTimersByTimeAsync(0)
      const initialCalls = (api.tunnelStatus as ReturnType<typeof vi.fn>).mock.calls.length
      expect(initialCalls).toBeGreaterThan(0)
      // Well past several 15s intervals — a disabled tunnel must not re-poll.
      await vi.advanceTimersByTimeAsync(15_000 * 5)
      expect((api.tunnelStatus as ReturnType<typeof vi.fn>).mock.calls.length).toBe(initialCalls)
    } finally {
      vi.useRealTimers()
    }
  })
})
