import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

/* ── Mocks: must run before importing the component ── */

// Keep the REAL ApiError so the component's `instanceof ApiError` branch works;
// stub only the network methods.
const mockApi = vi.hoisted(() => ({
  connectionsMint: vi.fn(),
  connectionsMintState: vi.fn(),
  mcpOAuthRelay: vi.fn(),
  mcpProbe: vi.fn(),
}))
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { api: mockApi, ApiError: actual.ApiError }
})

// Spy the MODULE-LEVEL SINGLETON: the component invalidates ['mcp-servers']
// through this exact instance (never useQueryClient()), so the assertion has to
// watch the singleton, not a per-render client.
import { queryClient } from '../api/queryClient'

import { ApiError } from '../api/client'
import McpRowSignIn from '../pages/overview/McpRowSignIn'

const VALID_RETURN = 'http://127.0.0.1:52100/?code=abc123'

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  vi.useFakeTimers()
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/**
 * Flush pending microtasks (resolved promises) WITHOUT advancing wall-clock —
 * the substitute for real-timer `waitFor`, which cannot poll while timers are
 * faked. `advanceTimersByTimeAsync(0)` yields to the microtask queue between
 * timer callbacks.
 */
async function flush() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
}

/** Advance the 2s poll interval N times, flushing microtasks between each. */
async function tickPolls(n: number) {
  for (let i = 0; i < n; i += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
  }
}

describe('McpRowSignIn', () => {
  it('mints, polls, and renders the authorize link once the URL arrives', async () => {
    mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
    mockApi.connectionsMintState
      .mockResolvedValueOnce({ slug: 'notion', state: 'minting' })
      .mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: 'https://mcp.notion.com/authorize' })

    render(<McpRowSignIn slug="notion" serverName="notion" />)

    fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
    await flush()
    expect(mockApi.connectionsMint).toHaveBeenCalledWith('notion')
    // Preparing state while polling.
    expect(screen.getByRole('status')).toHaveTextContent(/Preparing/)

    await tickPolls(2)

    const link = screen.getByRole('link', { name: /Authorize notion/ })
    expect(link).toHaveAttribute('href', 'https://mcp.notion.com/authorize')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // The relay disclosure is present alongside the authorize link.
    expect(screen.getByRole('button', { name: /connection error after authorizing/ })).toBeInTheDocument()
  })

  it('shows an inline error with retry when the mint is rejected', async () => {
    mockApi.connectionsMint.mockRejectedValue(new Error('boom'))
    render(<McpRowSignIn slug="notion" serverName="notion" />)

    fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
    await flush()
    expect(screen.getByRole('alert')).toHaveTextContent(/Could not start the sign-in/)
    expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument()
    // Never polled after a mint failure.
    expect(mockApi.connectionsMintState).not.toHaveBeenCalled()
  })

  it('errors and offers retry when polling never yields an approval URL', async () => {
    mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
    mockApi.connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })

    render(<McpRowSignIn slug="notion" serverName="notion" />)
    fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
    await flush()

    // 30 polls (the cap) with no URL → timeout error + retry.
    await tickPolls(30)
    expect(screen.getByRole('alert')).toHaveTextContent(/did not produce an approval link/)
    expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument()
  })

  it('probes and repaints instead of a false timeout when the mint reports granted', async () => {
    const setData = vi.spyOn(queryClient, 'setQueryData')
    mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
    // A concurrent flow finished the sign-in before this attempt produced a URL.
    mockApi.connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'granted' })
    const probed = [{ name: 'notion', status: 'ok', tools: [], authChallenge: true, authGrantPresent: true }]
    mockApi.mcpProbe.mockResolvedValue(probed)

    render(<McpRowSignIn slug="notion" serverName="notion" />)
    fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
    await tickPolls(1)

    // Fresh probe (not a cache read: GET /api/mcp replays the pre-consent
    // observation) repaints the table; no timeout error, no error alert.
    expect(mockApi.mcpProbe).toHaveBeenCalled()
    expect(setData).toHaveBeenCalledWith(['mcp-servers'], probed)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // Back to idle — the repainted row (source of truth) decides what renders.
    expect(screen.getByRole('button', { name: /Sign in/ })).toBeInTheDocument()
  })

  it('errors when the mint state reports failure', async () => {
    mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
    mockApi.connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'failed', reason: 'mint_timeouterror' })

    render(<McpRowSignIn slug="notion" serverName="notion" />)
    fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
    await tickPolls(1)
    expect(screen.getByRole('alert')).toHaveTextContent(/Could not start the sign-in/)
  })

  describe('relay affordance', () => {
    async function reachAuthorize() {
      mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
      mockApi.connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: 'https://mcp.notion.com/authorize' })
      render(<McpRowSignIn slug="notion" serverName="notion" />)
      fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
      await tickPolls(1)
      screen.getByRole('link', { name: /Authorize notion/ })
      // Expand the disclosure.
      fireEvent.click(screen.getByRole('button', { name: /connection error after authorizing/ }))
    }

    it('relays a pasted return address and invalidates the singleton cache', async () => {
      const invalidate = vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined)
      mockApi.mcpOAuthRelay.mockResolvedValue({ ok: true })
      await reachAuthorize()

      const input = screen.getByLabelText('Return address')
      fireEvent.change(input, { target: { value: VALID_RETURN } })
      fireEvent.click(screen.getByRole('button', { name: /Complete connection/ }))
      await flush()

      expect(mockApi.mcpOAuthRelay).toHaveBeenCalledWith('notion', VALID_RETURN)
      // Neutral delivered state — never claims signed-in.
      expect(screen.getByRole('status')).toHaveTextContent(/Code delivered/)
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['mcp-servers'] })
    })

    it('rejects a malformed return address locally without a round-trip', async () => {
      await reachAuthorize()
      const input = screen.getByLabelText('Return address')
      fireEvent.change(input, { target: { value: 'not-a-url' } })
      fireEvent.click(screen.getByRole('button', { name: /Complete connection/ }))
      await flush()
      expect(screen.getByRole('alert')).toBeInTheDocument()
      expect(mockApi.mcpOAuthRelay).not.toHaveBeenCalled()
    })

    it('routes a superseded approval to the error phase with a real Try again control', async () => {
      // FIX 2: a 409 approval_superseded is a terminal dead-end — re-pasting can
      // never succeed. On this surface (no out-of-band completion signal) it must
      // land in the `error` phase, which has a real retry button, NOT inline copy
      // pointing at a "Sign in" control that does not exist in the authorize view.
      mockApi.mcpOAuthRelay.mockRejectedValue(
        new ApiError(409, 'conflict', JSON.stringify({ code: 'approval_superseded' })),
      )
      await reachAuthorize()

      const input = screen.getByLabelText('Return address')
      fireEvent.change(input, { target: { value: VALID_RETURN } })
      fireEvent.click(screen.getByRole('button', { name: /Complete connection/ }))
      await flush()

      expect(screen.getByRole('alert')).toHaveTextContent(/no longer active/)
      // Error phase: a real Try again button, and the paste-back input is gone.
      expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument()
      expect(screen.queryByLabelText('Return address')).not.toBeInTheDocument()
    })

    it('shows the generic relay failure INLINE for a bad pasted URL (retryable in place)', async () => {
      // A bad pasted URL is retryable in the same input — it stays inline and does
      // NOT tear down to the error phase.
      mockApi.mcpOAuthRelay.mockRejectedValue(new Error('network'))
      await reachAuthorize()

      const input = screen.getByLabelText('Return address')
      fireEvent.change(input, { target: { value: VALID_RETURN } })
      fireEvent.click(screen.getByRole('button', { name: /Complete connection/ }))
      await flush()

      expect(screen.getByRole('alert')).toHaveTextContent(/Could not complete the connection/)
      // Still inline — the input remains, no error-phase Try again.
      expect(screen.getByLabelText('Return address')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /Try again/ })).not.toBeInTheDocument()
    })

    it('routes the 60s delivery timeout to the error phase when the probe still sees no grant', async () => {
      // FIX 2: the delivery timeout is the other terminal dead-end. When the
      // fresh probe confirms the exchange genuinely never completed, it lands in
      // the error phase (Try again), not a reopened inline input.
      vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined)
      mockApi.mcpOAuthRelay.mockResolvedValue({ ok: true })
      mockApi.mcpProbe.mockResolvedValue([
        { name: 'notion', status: 'needs_auth', tools: [], authChallenge: true, authGrantPresent: false },
      ])
      await reachAuthorize()

      const input = screen.getByLabelText('Return address')
      fireEvent.change(input, { target: { value: VALID_RETURN } })
      fireEvent.click(screen.getByRole('button', { name: /Complete connection/ }))
      await flush()
      expect(screen.getByRole('status')).toHaveTextContent(/Code delivered/)

      // The server's completion never arrives; after 60s the dead-end routes out.
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
      await flush()
      expect(mockApi.mcpProbe).toHaveBeenCalled()
      expect(screen.getByRole('alert')).toHaveTextContent(/did not complete/)
      expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument()
      expect(screen.queryByLabelText('Return address')).not.toBeInTheDocument()
    })

    it('suppresses the delivery-timeout error when the fresh probe shows the grant landed', async () => {
      vi.spyOn(queryClient, 'invalidateQueries').mockResolvedValue(undefined)
      const setData = vi.spyOn(queryClient, 'setQueryData')
      mockApi.mcpOAuthRelay.mockResolvedValue({ ok: true })
      // The gateway finished the exchange after the relay; only a FRESH probe
      // can see it (GET /api/mcp replays the cached pre-consent observation).
      const probed = [{ name: 'notion', status: 'ok', tools: [], authChallenge: true, authGrantPresent: true }]
      mockApi.mcpProbe.mockResolvedValue(probed)
      await reachAuthorize()

      const input = screen.getByLabelText('Return address')
      fireEvent.change(input, { target: { value: VALID_RETURN } })
      fireEvent.click(screen.getByRole('button', { name: /Complete connection/ }))
      await flush()
      expect(screen.getByRole('status')).toHaveTextContent(/Code delivered/)

      await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
      // Probe repainted the table; NO false "did not complete" on the success path.
      expect(setData).toHaveBeenCalledWith(['mcp-servers'], probed)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      // Delivered state persists until the repainted row unmounts the component.
      expect(screen.getByRole('status')).toHaveTextContent(/Code delivered/)
    })
  })

  describe('authorize-phase polling', () => {
    it('keeps polling after the URL arrives; a local authorize grant probes and returns to idle', async () => {
      // FIX 3: a successful LOCAL authorize completes on the gateway with no
      // paste-back, so the row would stick on "Authorize {{name}}" unless the
      // component keeps watching the mint feed. Once it reports granted, a fresh
      // probe repaints the row and the component drops back to idle.
      const setData = vi.spyOn(queryClient, 'setQueryData')
      mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
      // First a URL (→ authorize), then the local authorize completes (granted).
      mockApi.connectionsMintState
        .mockResolvedValueOnce({ slug: 'notion', state: 'waiting', oauth_url: 'https://mcp.notion.com/authorize' })
        .mockResolvedValue({ slug: 'notion', state: 'granted' })
      const probed = [{ name: 'notion', status: 'ok', tools: [], authChallenge: true, authGrantPresent: true }]
      mockApi.mcpProbe.mockResolvedValue(probed)

      render(<McpRowSignIn slug="notion" serverName="notion" />)
      fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
      await tickPolls(1)
      // Authorize link is up.
      expect(screen.getByRole('link', { name: /Authorize notion/ })).toBeInTheDocument()

      // The authorize-phase poll fires; granted → probe → idle.
      await tickPolls(1)
      expect(mockApi.mcpProbe).toHaveBeenCalled()
      expect(setData).toHaveBeenCalledWith(['mcp-servers'], probed)
      expect(screen.getByRole('button', { name: /Sign in/ })).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: /Authorize notion/ })).not.toBeInTheDocument()
    })

    it('routes an authorize-phase mint failure to the error phase', async () => {
      mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
      mockApi.connectionsMintState
        .mockResolvedValueOnce({ slug: 'notion', state: 'waiting', oauth_url: 'https://mcp.notion.com/authorize' })
        .mockResolvedValue({ slug: 'notion', state: 'failed', reason: 'mint_timeouterror' })

      render(<McpRowSignIn slug="notion" serverName="notion" />)
      fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
      await tickPolls(1)
      expect(screen.getByRole('link', { name: /Authorize notion/ })).toBeInTheDocument()
      await tickPolls(1)
      expect(screen.getByRole('alert')).toHaveTextContent(/Could not start the sign-in/)
      expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument()
    })
  })

  describe('caption placement', () => {
    it('shows the row-updates caption from the authorize phase, not the idle phase', async () => {
      // FIX 5: the caption moved INTO this component and appears only once a
      // sign-in is pending to reconcile — never on the idle Sign in button.
      mockApi.connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 't1' })
      mockApi.connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: 'https://mcp.notion.com/authorize' })

      render(<McpRowSignIn slug="notion" serverName="notion" />)
      // Idle: no caption.
      expect(screen.queryByText(/refresh this row/)).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: /Sign in/ }))
      await tickPolls(1)
      // Authorize: caption present, naming the Probe MCP servers control.
      expect(screen.getByText(/Use Probe MCP servers above to refresh this row/)).toBeInTheDocument()
    })
  })
})
