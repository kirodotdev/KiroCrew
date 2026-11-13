import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useGuardedMutation } from './useGuardedMutation'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('useGuardedMutation', () => {
  it.each(['success', 'failure'] as const)('holds one latch across the guard and mutation until %s', async outcome => {
    const confirmation = deferred<boolean>()
    const mutation = deferred<void>()
    const confirm = vi.fn(() => confirmation.promise)
    const mutate = vi.fn(() => mutation.promise)
    const { result } = renderHook(() => useGuardedMutation(mutate, confirm))
    let first!: Promise<void>
    act(() => {
      first = result.current.run()
      void result.current.run()
    })
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(result.current.isPending).toBe(true)
    expect(mutate).not.toHaveBeenCalled()
    await act(async () => { confirmation.resolve(true) })
    expect(mutate).toHaveBeenCalledTimes(1)
    await act(async () => { await result.current.run() })
    expect(confirm).toHaveBeenCalledTimes(1)
    await act(async () => {
      if (outcome === 'success') mutation.resolve()
      else mutation.reject(new Error('mutation failed'))
      await first
    })
    expect(result.current.isPending).toBe(false)
    await act(async () => { await result.current.run() })
    expect(mutate).toHaveBeenCalledTimes(2)
  })

  it('releases a cancelled guard without submitting', async () => {
    const confirmation = deferred<boolean>()
    const confirm = vi.fn(() => confirmation.promise)
    const mutate = vi.fn().mockResolvedValue(undefined)
    const { result } = renderHook(() => useGuardedMutation(mutate, confirm))
    let first!: Promise<void>
    act(() => { first = result.current.run() })
    await act(async () => { confirmation.resolve(false); await first })
    expect(result.current.isPending).toBe(false)
    expect(mutate).not.toHaveBeenCalled()
    confirm.mockResolvedValue(true)
    await act(async () => { await result.current.run() })
    expect(mutate).toHaveBeenCalledTimes(1)
  })
})
