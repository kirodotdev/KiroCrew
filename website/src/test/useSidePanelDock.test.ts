import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useSidePanelDock, setSidePanelDock } from '../hooks/useSidePanelDock'

const KEY = 'mc-side-panel-dock'

describe('useSidePanelDock', () => {
  beforeEach(() => { localStorage.clear(); setSidePanelDock('right') })
  afterEach(() => { setSidePanelDock('right') })

  it('defaults to "right"', () => {
    const { result } = renderHook(() => useSidePanelDock())
    expect(result.current[0]).toBe('right')
  })

  it('setter flips the value, persists it, and broadcasts to all consumers', () => {
    const a = renderHook(() => useSidePanelDock())
    const b = renderHook(() => useSidePanelDock())

    act(() => a.result.current[1]('bottom'))

    // Both hook instances see the change (single module-level store).
    expect(a.result.current[0]).toBe('bottom')
    expect(b.result.current[0]).toBe('bottom')
    expect(localStorage.getItem(KEY)).toBe('bottom')

    act(() => a.result.current[1]('right'))
    expect(a.result.current[0]).toBe('right')
    expect(b.result.current[0]).toBe('right')
    expect(localStorage.getItem(KEY)).toBe('right')
  })

  it('is a no-op when set to the current value', () => {
    localStorage.clear() // prove idempotent set does not re-write storage
    act(() => setSidePanelDock('right'))
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})
