/**
 * A pane that mounts onto a window the store already holds re-reads it once
 * (`warmSlotCache`). Without this, a bot chat that was off screen while the
 * socket was down kept its frozen partial reply until the page was reloaded.
 * The hook also reports the warm's outcome, which stands in for the pane's
 * own hydrate read: a landed page and a failure the pane can retry.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, cleanup, act, waitFor } from '@testing-library/react'
import { configureStore } from '@reduxjs/toolkit'
import { Provider, useDispatch, useSelector } from 'react-redux'
import type { ReactNode } from 'react'

const outcome = { fail: false }
const warm = vi.fn()
vi.mock('../../store', () => ({ useAppDispatch: useDispatch, useAppSelector: useSelector }))
vi.mock('../../store/chatSlice', async () => {
  const { createAsyncThunk } = await import('@reduxjs/toolkit')
  return {
    warmSlotCache: createAsyncThunk('test/warmSlotCache', async (key: string) => {
      await warm(key)
      if (outcome.fail) throw new Error('HTTP 503')
      return null
    }),
  }
})

import { useHeldWindowWarm } from './useHeldWindowWarm'

const SLOT = 'member-alpha.memory-x'

function wrapperFor(slotPaneHasMore: Record<string, boolean>) {
  const store = configureStore({ reducer: { chat: (s = { slotPaneHasMore }) => s } })
  return ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
}

describe('useHeldWindowWarm', () => {
  beforeEach(() => { warm.mockClear(); outcome.fail = false; cleanup() })

  it('warms a held window once when a pane mounts onto it, and reports the page landed', async () => {
    const { result, rerender } = renderHook(({ active }) => useHeldWindowWarm(SLOT, active), {
      wrapper: wrapperFor({ [SLOT]: false }),
      initialProps: { active: false },
    })
    rerender({ active: false })
    expect(warm.mock.calls).toEqual([[SLOT]])
    expect(result.current.warm).toBe(true)
    await waitFor(() => expect(result.current.settled).toBe(true))
    expect(result.current.failed).toBe(false)
  })

  it('reports a failed warm, and retry runs it again', async () => {
    outcome.fail = true
    const { result } = renderHook(() => useHeldWindowWarm(SLOT, false), { wrapper: wrapperFor({ [SLOT]: true }) })
    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.settled).toBe(false)
    outcome.fail = false
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.settled).toBe(true))
    expect(result.current.failed).toBe(false)
    expect(warm).toHaveBeenCalledTimes(2)
  })

  it('lets only the latest warm report, so an older one settling late is ignored', async () => {
    const { result } = renderHook(() => useHeldWindowWarm(SLOT, false), { wrapper: wrapperFor({ [SLOT]: true }) })
    await waitFor(() => expect(result.current.settled).toBe(true))
    let failOlder!: (e: Error) => void
    warm.mockImplementationOnce(() => new Promise((_, reject) => { failOlder = reject }))
    act(() => result.current.retry())
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.settled).toBe(true))
    await act(async () => { failOlder(new Error('HTTP 503')) })
    expect(result.current).toMatchObject({ settled: true, failed: false })
    expect(warm).toHaveBeenCalledTimes(3)
  })

  it('leaves a slot with no held window to the pane hydrate', () => {
    const { result } = renderHook(() => useHeldWindowWarm(SLOT, false), { wrapper: wrapperFor({}) })
    expect(warm).not.toHaveBeenCalled()
    expect(result.current).toMatchObject({ warm: false, settled: false, failed: false })
  })

  it('leaves the active slot to its own readers', () => {
    renderHook(() => useHeldWindowWarm(SLOT, true), { wrapper: wrapperFor({ [SLOT]: true }) })
    expect(warm).not.toHaveBeenCalled()
  })
})
