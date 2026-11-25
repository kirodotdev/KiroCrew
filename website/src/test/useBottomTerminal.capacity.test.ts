import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  __resetBottomTerminal,
  addTab,
  useBottomTerminal,
} from '../hooks/useBottomTerminal'

beforeEach(() => { __resetBottomTerminal(); localStorage.clear() })
afterEach(() => { act(() => { __resetBottomTerminal() }) })

// The dock sets no tab cap: the gateway's own ceiling is the only limit, and a
// tab it refuses says so. Twenty is well past the old cap of eight.
describe('terminal tabs have no front-end cap', () => {
  it('opens a new tab on every request', () => {
    const { result } = renderHook(() => useBottomTerminal())
    const ids: string[] = []
    act(() => {
      for (let i = 0; i < 20; i++) ids.push(addTab(`/work/project-${i}`))
    })
    expect(ids.every(id => typeof id === 'string' && id.length > 0)).toBe(true)
    expect(new Set(ids).size).toBe(20)
    expect(result.current.tabs).toHaveLength(20)
    expect(result.current.activeId).toBe(ids.at(-1))
  })

  it('adopts every saved tab, not only the first eight', () => {
    const { result } = renderHook(() => useBottomTerminal())
    const tabs = Array.from({ length: 12 }, (_, i) => ({ id: `saved-${i}` }))
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({ tabs, activeId: 'saved-0' }))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'mc-bottom-terminal', storageArea: localStorage })) })
    expect(result.current.tabs.map(t => t.id)).toEqual(tabs.map(t => t.id))
  })
})
