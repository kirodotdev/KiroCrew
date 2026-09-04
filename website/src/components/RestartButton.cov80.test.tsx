import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import RestartButton from './RestartButton'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, restartSessions: vi.fn() } }
})

const restartSessions = vi.mocked(api.restartSessions)

describe('RestartButton', () => {
  beforeEach(() => {
    restartSessions.mockReset()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => vi.useRealTimers())

  it('reports success and clears the notice after the timeout', async () => {
    restartSessions.mockResolvedValue(undefined as never)
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button'))
    const ok = await screen.findByText(/sessions restarted/i)
    expect(ok.className).toContain('text-ok')
    expect(restartSessions).toHaveBeenCalledTimes(1)
    // Button is re-enabled once the call settles.
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled())

    act(() => { vi.advanceTimersByTime(5000) })
    await waitFor(() => expect(screen.queryByText(/sessions restarted/i)).not.toBeInTheDocument())
  })

  it('surfaces the Error message on failure in the danger tint', async () => {
    restartSessions.mockRejectedValue(new Error('zzq-restart-broke'))
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button'))
    const err = await screen.findByText('zzq-restart-broke')
    expect(err.className).toContain('text-danger')
  })

  it('falls back to the generic failure text for a non-Error rejection', async () => {
    restartSessions.mockRejectedValue('zzq-not-an-error')
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button'))
    const err = await screen.findByText(/restart failed/i)
    expect(err.className).toContain('text-danger')
  })

  it('disables itself while the restart is in flight', async () => {
    let release: (() => void) | undefined
    restartSessions.mockImplementation(
      () => new Promise<void>(resolve => { release = resolve }) as never,
    )
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button'))
    const button = await screen.findByRole('button')
    await waitFor(() => expect(button).toBeDisabled())
    expect(button.className).toContain('cursor-wait')
    expect(screen.getByText(/restarting/i)).toBeInTheDocument()

    await act(async () => { release?.() })
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled())
  })

  it('reports a FAILED MCP reconcile instead of claiming the config was applied', async () => {
    // The HTTP call succeeded and the sessions did restart, but the reconcile
    // before it failed — so the config on disk may not match the sources.
    // Claiming "config applied" here is the lie this button exists to avoid.
    restartSessions.mockResolvedValue({
      ok: true,
      sessions_reset: 2,
      mcp_synced: 0,
      mcp_sync_ok: false,
    } as never)
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button'))
    const msg = await screen.findByText(/mcp sync failed/i)
    expect(msg.className).toContain('text-danger')
    expect(screen.queryByText(/config applied/i)).not.toBeInTheDocument()
  })

  it('still reports plain success when the reconcile is ok', async () => {
    restartSessions.mockResolvedValue({
      ok: true,
      sessions_reset: 2,
      mcp_synced: 3,
      mcp_sync_ok: true,
    } as never)
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button'))
    const ok = await screen.findByText(/config applied/i)
    expect(ok.className).toContain('text-ok')
  })
})
