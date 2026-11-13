import { afterEach, describe, expect, it } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useShellSlot } from './useShellSlot'

afterEach(() => { document.body.replaceChildren() })

describe('useShellSlot', () => {
  it('a bumped rev latches the element that replaced a detached one', () => {
    const first = document.createElement('div')
    first.id = 'activity-bar-slot'
    document.body.append(first)
    const { result, rerender } = renderHook(({ rev }) => useShellSlot('activity-bar-slot', true, rev), { initialProps: { rev: 0 } })
    expect(result.current).toBe(first)
    const second = document.createElement('div')
    second.id = 'activity-bar-slot'
    first.replaceWith(second)
    rerender({ rev: 0 })
    expect(result.current).toBe(first)
    rerender({ rev: 1 })
    expect(result.current).toBe(second)
  })
})
