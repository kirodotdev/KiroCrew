/**
 * Tests for usePersistedBool — localStorage-backed boolean view preferences
 * (word wrap, line numbers, diff split/unified, capsule collapse, …).
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { safeSetItem } from '../utils/safeStorage'

describe('usePersistedBool', () => {
  beforeEach(() => localStorage.clear())

  it('falls back to the default when nothing is persisted', () => {
    const { result } = renderHook(() => usePersistedBool('t-key', true))
    expect(result.current[0]).toBe(true)
  })

  it('reads a persisted "1"/"0" over the default on mount', () => {
    safeSetItem('t-key', '0')
    const { result } = renderHook(() => usePersistedBool('t-key', true))
    expect(result.current[0]).toBe(false)

    safeSetItem('t-key2', '1')
    const { result: r2 } = renderHook(() => usePersistedBool('t-key2', false))
    expect(r2.current[0]).toBe(true)
  })

  it('writes changes back to localStorage', () => {
    const { result } = renderHook(() => usePersistedBool('t-key', false))
    act(() => result.current[1](true))
    expect(result.current[0]).toBe(true)
    expect(localStorage.getItem('t-key')).toBe('1')
    act(() => result.current[1](false))
    expect(localStorage.getItem('t-key')).toBe('0')
  })

  it('a fresh mount picks up the value a previous instance persisted', () => {
    const first = renderHook(() => usePersistedBool('t-shared', false))
    act(() => first.result.current[1](true))
    first.unmount()
    const second = renderHook(() => usePersistedBool('t-shared', false))
    expect(second.result.current[0]).toBe(true)
  })

  it('supports functional updates', () => {
    const { result } = renderHook(() => usePersistedBool('t-fn', false))
    act(() => result.current[1](v => !v))
    expect(result.current[0]).toBe(true)
    expect(localStorage.getItem('t-fn')).toBe('1')
  })
})
