import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  reportActionFailure,
  clearActionFailure,
  useActionFailure,
  __resetActionFailureForTests,
} from '../utils/actionFailure'

describe('the shared surface a rejected gateway write reports to', () => {
  beforeEach(() => { __resetActionFailureForTests() })

  it('carries a rejected write to a live subscriber', () => {
    const { result } = renderHook(() => useActionFailure())
    expect(result.current.failure).toBeNull()
    act(() => { reportActionFailure('Session reload failed.') })
    expect(result.current.failure?.message).toBe('Session reload failed.')
  })

  it('survives the component that observed the rejection', () => {
    // The menu subtree that raised it unmounts as the menu closes, which is why
    // the store outlives it instead of holding the failure in that component.
    act(() => { reportActionFailure('Peer refused the transfer') })
    const { result } = renderHook(() => useActionFailure())
    expect(result.current.failure?.message).toBe('Peer refused the transfer')
  })

  it('clears on dismiss', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('Boom') })
    act(() => { result.current.clear() })
    expect(result.current.failure).toBeNull()
  })

  it('ignores an empty message, which would render an empty notice', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('') })
    expect(result.current.failure).toBeNull()
  })

  it('keeps the newest rejection when two land', () => {
    const { result } = renderHook(() => useActionFailure())
    act(() => { reportActionFailure('First') })
    act(() => { reportActionFailure('Second') })
    expect(result.current.failure?.message).toBe('Second')
  })

  it('clearing when nothing is set notifies nobody', () => {
    let renders = 0
    renderHook(() => { renders += 1; return useActionFailure() })
    const before = renders
    act(() => { clearActionFailure() })
    expect(renders).toBe(before)
  })
})
