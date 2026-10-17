import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  __resetBottomTerminal,
  addTab,
  closeBottomTerminal,
  MAX_TERMINALS,
  removeTab,
  setActiveTab,
  useBottomTerminal,
} from '../hooks/useBottomTerminal'

beforeEach(() => { __resetBottomTerminal(); localStorage.clear() })
afterEach(() => { __resetBottomTerminal() })

describe('terminal capacity recovery', () => {
  it('reveals the last existing tab at capacity and permits a new tab only after one is closed', () => {
    const { result } = renderHook(() => useBottomTerminal())
    act(() => {
      for (let i = 0; i < MAX_TERMINALS; i++) addTab(`/work/project-${i}`)
    })
    const existing = result.current.tabs
    act(() => { setActiveTab(existing[0].id); closeBottomTerminal() })
    expect(result.current.open).toBe(false)
    expect(result.current.activeId).toBe(existing[0].id)

    let refused: string | null = 'not called'
    act(() => { refused = addTab('/work/requested') })
    expect(refused).toBeNull()
    expect(result.current.tabs).toEqual(existing)
    expect(result.current.open).toBe(true)
    expect(result.current.activeId).toBe(existing.at(-1)!.id)

    let created: string | null = null
    act(() => { removeTab(existing[0].id); created = addTab('/work/requested') })
    expect(created).not.toBeNull()
    expect(result.current.tabs).toHaveLength(MAX_TERMINALS)
    expect(result.current.tabs.at(-1)).toEqual({ id: created, cwd: '/work/requested' })
    expect(result.current.activeId).toBe(created)
  })
})
