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
    // The button asks before it relaunches; these cases are about what
    // happens after the operator has said yes.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('asks first, saying what stays and what stops, and does nothing when declined', () => {
    // The reassurance has to land at the moment of the click, not only in a
    // hover title: a reader who reads "Restart" as breaking something never
    // presses it, and a saved template change never reaches a running chat.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<RestartButton />)
    fireEvent.click(screen.getByRole('button'))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(confirm.mock.calls[0][0]).toMatch(/Your chats and their history stay; a reply in progress stops/)
    expect(restartSessions).not.toHaveBeenCalled()
  })

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

  it('surfaces the Error message on failure through ErrorNotice, and keeps it until dismissed', async () => {
    restartSessions.mockRejectedValue(new Error('zzq-restart-broke'))
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button', { name: /apply & restart/i }))
    const notice = await screen.findByTestId('restart-button-error')
    expect(notice).toHaveAttribute('role', 'alert')
    // The page's sentence leads; the server's reason follows it.
    expect(notice).toHaveTextContent('Could not restart — zzq-restart-broke')
    // No agent hand-off: it would navigate away from the page the button sits in.
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()
    // A failure does not time out; the reader dismisses it.
    act(() => { vi.advanceTimersByTime(6000) })
    expect(screen.getByTestId('restart-button-error')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('restart-button-error')).not.toBeInTheDocument()
  })

  it('falls back to the generic failure text for a non-Error rejection', async () => {
    restartSessions.mockRejectedValue('zzq-not-an-error')
    render(<RestartButton />)

    fireEvent.click(screen.getByRole('button'))
    const notice = await screen.findByTestId('restart-button-error')
    expect(notice).toHaveTextContent(/restart failed/i)
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
    const notice = await screen.findByTestId('restart-button-error')
    expect(notice).toHaveTextContent(/mcp sync failed/i)
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
