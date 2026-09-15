import { describe, it, expect, afterEach } from 'vitest'
import { renderHook, act, cleanup } from '@testing-library/react'

import { useDevMode } from '../hooks/useDevMode'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe('useDevMode', () => {
  it('reads the persisted flag on mount', () => {
    localStorage.setItem('mc-dev-mode', '1')
    const { result } = renderHook(() => useDevMode())
    expect(result.current).toBe(true)
  })

  it('defaults to false when the flag is absent or not "1"', () => {
    const { result: a } = renderHook(() => useDevMode())
    expect(a.current).toBe(false)
    localStorage.setItem('mc-dev-mode', '0')
    const { result: b } = renderHook(() => useDevMode())
    expect(b.current).toBe(false)
  })

  it('updates live when the toggle fires mc-dev-mode-changed', () => {
    const { result } = renderHook(() => useDevMode())
    expect(result.current).toBe(false)
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-dev-mode-changed', { detail: true }))
    })
    expect(result.current).toBe(true)
    act(() => {
      window.dispatchEvent(new CustomEvent('mc-dev-mode-changed', { detail: false }))
    })
    expect(result.current).toBe(false)
  })

  it('reacts to a cross-tab storage event on the flag key', () => {
    const { result } = renderHook(() => useDevMode())
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'mc-dev-mode', newValue: '1' }))
    })
    expect(result.current).toBe(true)
  })
})
