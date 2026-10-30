/**
 * `useMenuOpensDialog`: the one-shot restore-skip flag, the one-macrotask
 * deferred open, and the hand-back to the recorded opener.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { SyntheticEvent } from 'react'
import { useMenuOpensDialog } from './useMenuOpensDialog'

function closeEvent() {
  return { preventDefault: vi.fn() } as unknown as Event & { preventDefault: ReturnType<typeof vi.fn> }
}

afterEach(() => {
  vi.useRealTimers()
  document.body.replaceChildren()
})

describe('useMenuOpensDialog', () => {
  it('leaves the trigger restore alone when no item opened a layer', () => {
    const { result } = renderHook(() => useMenuOpensDialog())
    const e = closeEvent()
    expect(result.current.onCloseAutoFocus(e)).toBe(false)
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('openLayer suppresses exactly one restore and opens one macrotask later', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMenuOpensDialog())
    const open = vi.fn()
    act(() => result.current.openLayer(open))
    expect(open).not.toHaveBeenCalled()
    const first = closeEvent()
    expect(result.current.onCloseAutoFocus(first)).toBe(true)
    expect(first.preventDefault).toHaveBeenCalledTimes(1)
    act(() => { vi.runAllTimers() })
    expect(open).toHaveBeenCalledTimes(1)
    const second = closeEvent()
    expect(result.current.onCloseAutoFocus(second)).toBe(false)
    expect(second.preventDefault).not.toHaveBeenCalled()
  })

  it('armSkipRestore arms without deferring, and disarm clears it', () => {
    const { result } = renderHook(() => useMenuOpensDialog())
    act(() => result.current.armSkipRestore())
    act(() => result.current.disarm())
    const e = closeEvent()
    expect(result.current.onCloseAutoFocus(e)).toBe(false)
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('returnFocus focuses the element whose own event opened the menu', () => {
    const { result } = renderHook(() => useMenuOpensDialog())
    const opener = document.createElement('button')
    const other = document.createElement('button')
    document.body.append(opener, other)
    other.focus()
    act(() => result.current.rememberOpener({ currentTarget: opener } as unknown as SyntheticEvent<HTMLElement>))
    act(() => result.current.returnFocus())
    expect(document.activeElement).toBe(opener)
  })

  it('returns stable function identities across renders', () => {
    const { result, rerender } = renderHook(() => useMenuOpensDialog())
    const before = result.current
    rerender()
    expect(result.current.onCloseAutoFocus).toBe(before.onCloseAutoFocus)
    expect(result.current.openLayer).toBe(before.openLayer)
    expect(result.current.returnFocus).toBe(before.returnFocus)
  })
})
