import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: { planAction: vi.fn() },
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  },
}))

import { api, ApiError } from '../api/client'
import { isPlanAction, usePlanActionMutation } from '../hooks/usePlanActionMutation'

const planAction = api.planAction as unknown as ReturnType<typeof vi.fn>

let queryClient: QueryClient
const wrapper = ({ children }: { children: React.ReactNode }) =>
  React.createElement(QueryClientProvider, { client: queryClient }, children)

beforeEach(() => {
  planAction.mockReset()
  planAction.mockResolvedValue({ ok: true })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

/* The allowlist must mirror the server's plan-action contract exactly:
 * chat_orchestrator lowercases and strips the incoming action and accepts
 * only 'go', 'go all', 'cancel'. isPlanAction applies the same normalization
 * client-side, so every label it admits is a label the server will act on,
 * and everything else stays on the composer path. */

describe('isPlanAction', () => {
  it.each(['Go', 'Go All', 'Cancel'])('accepts the canonical chip label %j', (label) => {
    expect(isPlanAction(label)).toBe(true)
  })

  it.each(['go', 'GO', 'go all', 'GO ALL', 'cancel', 'CANCEL'])(
    'is case-insensitive, matching the server\'s .lower(): %j', (label) => {
      expect(isPlanAction(label)).toBe(true)
    })

  it.each([' Go ', '\tCancel\n', ' go all '])(
    'trims surrounding whitespace, matching the server\'s .strip(): %j', (label) => {
      expect(isPlanAction(label)).toBe(true)
    })

  it.each(['Approve', 'Approve it', 'Stage-1-APPROVE', 'Go  All', 'goall', 'go-all', '', ' '])(
    'rejects non-protocol labels the server would 400: %j', (label) => {
      expect(isPlanAction(label)).toBe(false)
    })
})

/* A chip click is DEBOUNCED by FollowUpBar (FOLLOWUP_CHIP_DEBOUNCE_MS), and a
 * byte-identical replacement footer re-renders the same chips WITHOUT
 * remounting them — so the pending timer outlives the row it was armed on and
 * `mutate` can run after the transcript already advanced. Neither the
 * single-flight (the acknowledgement effect freed it for the new row) nor the
 * null-source refusal (a live row is on screen) stops that, so the row the user
 * clicked is captured at click time and rejected here when it no longer
 * matches. Every test uses its OWN slot key: the latches are module-level and
 * a mock reset does not clear them. */
describe('usePlanActionMutation stale-click guard', () => {
  it('refuses a click whose captured row has since been replaced, without consuming the latch', async () => {
    const { result } = renderHook(() => usePlanActionMutation('slot-stale', 'row-2'), { wrapper })
    // The click happened while 'row-1' was on screen; 'row-2' is current now.
    await act(async () => { result.current.mutate({ slot: 'slot-stale', action: 'Go', clickedSourceKey: 'row-1' }) })
    expect(planAction).not.toHaveBeenCalled()
    // The refusal must also leave the single-flight untouched — otherwise it
    // would silently swallow the CURRENT row's own first click.
    await act(async () => { result.current.mutate({ slot: 'slot-stale', action: 'Go', clickedSourceKey: 'row-2' }) })
    expect(planAction).toHaveBeenCalledTimes(1)
    expect(planAction).toHaveBeenCalledWith('slot-stale', 'Go')
  })

  it('dispatches a click whose captured row is still the current one', async () => {
    const { result } = renderHook(() => usePlanActionMutation('slot-match', 'row-1'), { wrapper })
    await act(async () => { result.current.mutate({ slot: 'slot-match', action: 'Go All', clickedSourceKey: 'row-1' }) })
    expect(planAction).toHaveBeenCalledTimes(1)
    expect(planAction).toHaveBeenCalledWith('slot-match', 'Go All')
  })

  it('dispatches a click that supplies NO row key at all (unchanged behaviour)', async () => {
    // A caller that does not pass one keeps its previous behaviour exactly:
    // refusing it wholesale would silently disable dispatch for any chip
    // surface not yet wired, which is worse than the race being guarded.
    const { result } = renderHook(() => usePlanActionMutation('slot-nokey', 'row-1'), { wrapper })
    await act(async () => { result.current.mutate({ slot: 'slot-nokey', action: 'Cancel' }) })
    expect(planAction).toHaveBeenCalledTimes(1)
    expect(planAction).toHaveBeenCalledWith('slot-nokey', 'Cancel')
  })
})

/* `isRefused` is what both hosts feed the FULL followUpOptions row through, and
 * a plan row is not necessarily plan-only — ChatPage's own handler routes
 * non-protocol labels to the composer. So the latch class lookup has to be
 * gated on `isPlanAction` first: without it every non-"cancel" label on the row
 * (an "Approve" or a free-text suggestion) falls into the Go class and dims the
 * moment a Go is held, refusing chips the dispatch never owned. */
describe('usePlanActionMutation isRefused', () => {
  it('refuses only plan labels in the held class, never a mixed row\'s other chips', async () => {
    const { result } = renderHook(() => usePlanActionMutation('slot-refused', 'row-1'), { wrapper })
    await act(async () => { result.current.mutate({ slot: 'slot-refused', action: 'Go', clickedSourceKey: 'row-1' }) })
    expect(planAction).toHaveBeenCalledWith('slot-refused', 'Go')
    // Go's class is held, so both of its labels are refused...
    expect(result.current.isRefused('Go')).toBe(true)
    expect(result.current.isRefused('Go All')).toBe(true)
    // ...Cancel is a different class and stays live...
    expect(result.current.isRefused('Cancel')).toBe(false)
    // ...and a non-protocol label on the same row is not a plan action at all.
    for (const label of ['Approve', 'Stage-1-APPROVE', 'Tell me more', '']) {
      expect(result.current.isRefused(label)).toBe(false)
    }
  })
})

/* The component refuses a click on a chip it has DIMMED, so the hook's own
 * `latch.has(vars.slot)` guard is no longer reachable from a dimmed chip. It is
 * still reachable from every caller that has no dim to go on — a second mount
 * whose props lag a publish, keyboard activation, a host that passes no
 * `refusedOptions` at all — and the ORDER inside it is load-bearing: clearing the
 * failure before the guard would retire the only account of why the chip is inert
 * while leaving the latch wedged. That is what this pins. */
describe('usePlanActionMutation refused-click guard order', () => {
  it('a refused click sends nothing AND keeps the failure that explains why', async () => {
    planAction.mockRejectedValueOnce(new ApiError(500, 'internal error'))
    const { result } = renderHook(() => usePlanActionMutation('slot-guard', 'row-1'), { wrapper })

    // A 5xx KEEPS the latch: the server may have committed and lost the response.
    await act(async () => { result.current.mutate({ slot: 'slot-guard', action: 'Go', source: 'row-1', clickedSourceKey: 'row-1' }) })
    await act(async () => { await Promise.resolve() })
    expect(result.current.failure).toBe('internal error')

    // Same class, so the held latch drops this one before any request goes out...
    await act(async () => { result.current.mutate({ slot: 'slot-guard', action: 'Go All', source: 'row-1', clickedSourceKey: 'row-1' }) })
    expect(planAction).toHaveBeenCalledTimes(1)
    // ...and the explanation must survive it. Clearing on the way IN would leave a
    // wedged latch with nothing on screen saying so.
    expect(result.current.failure).toBe('internal error')
  })
})
