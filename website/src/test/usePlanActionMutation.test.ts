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

import { api } from '../api/client'
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
