import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FLASH_HIGHLIGHT_MS, LANDED_HIGHLIGHT_MS, STEP_HIGHLIGHT_MS, useJumpMark } from '../pages/chat/page/jumpMark'

afterEach(() => { vi.useRealTimers() })

function setup() {
  const setHighlightTs = vi.fn()
  const setLandedTs = vi.fn()
  const { result } = renderHook(() => useJumpMark(setHighlightTs, setLandedTs))
  return { mark: result.current.markJump, result, setHighlightTs, setLandedTs }
}

describe('useJumpMark', () => {
  it('marks the landing highlight, clears the flash, and expires after its own time', () => {
    vi.useFakeTimers()
    const { mark, setHighlightTs, setLandedTs } = setup()
    act(() => mark('a', true))
    expect(setLandedTs).toHaveBeenLastCalledWith('a')
    expect(setHighlightTs).toHaveBeenLastCalledWith(null)
    act(() => { vi.advanceTimersByTime(LANDED_HIGHLIGHT_MS - 1) })
    expect(setLandedTs).toHaveBeenLastCalledWith('a')
    act(() => { vi.advanceTimersByTime(1) })
    expect(setLandedTs).toHaveBeenLastCalledWith(null)
  })

  it('marks the flash for its shorter time', () => {
    vi.useFakeTimers()
    const { mark, setHighlightTs, setLandedTs } = setup()
    act(() => mark('p', false))
    expect(setHighlightTs).toHaveBeenLastCalledWith('p')
    expect(setLandedTs).toHaveBeenLastCalledWith(null)
    act(() => { vi.advanceTimersByTime(FLASH_HIGHLIGHT_MS) })
    expect(setHighlightTs).toHaveBeenLastCalledWith(null)
  })

  it('an older mark’s expiry never clears a newer target', () => {
    vi.useFakeTimers()
    const { mark, setLandedTs } = setup()
    act(() => mark('a', true))
    act(() => { vi.advanceTimersByTime(LANDED_HIGHLIGHT_MS - 100) })
    act(() => mark('b', true))
    act(() => { vi.advanceTimersByTime(100) })
    expect(setLandedTs).toHaveBeenLastCalledWith('b')
    act(() => { vi.advanceTimersByTime(LANDED_HIGHLIGHT_MS) })
    expect(setLandedTs).toHaveBeenLastCalledWith(null)
  })

  it('counts every mark, a repeat of the same target included', () => {
    vi.useFakeTimers()
    const { mark, result } = setup()
    expect(result.current.jumpSeq).toBe(0)
    act(() => mark('a', false))
    act(() => mark('a', false))
    expect(result.current.jumpSeq).toBe(2)
  })

  it('keeps the jumped-to message\u2019s mid with its ts until the mark expires', () => {
    vi.useFakeTimers()
    const { mark, result } = setup()
    act(() => mark('t', true, 'm-2'))
    expect(result.current.jumpTarget).toEqual({ ts: 't', mid: 'm-2' })
    act(() => mark('u', false))
    expect(result.current.jumpTarget).toEqual({ ts: 'u', mid: null })
    act(() => { vi.advanceTimersByTime(FLASH_HIGHLIGHT_MS) })
    expect(result.current.jumpTarget).toBeNull()
  })

  it('a turn step replaces a landed jump: no jump target, and the landing expiry never fires', () => {
    vi.useFakeTimers()
    const { result, setHighlightTs, setLandedTs } = setup()
    act(() => result.current.markJump('hit', true, 'm-1'))
    const seq = result.current.jumpSeq
    act(() => result.current.markStep('prompt'))
    expect(setLandedTs).toHaveBeenLastCalledWith(null)
    expect(setHighlightTs).toHaveBeenLastCalledWith('prompt')
    // No jump target, so no fold reveal or row scroll reruns for the old hit.
    expect(result.current.jumpTarget).toBeNull()
    expect(result.current.jumpSeq).toBe(seq)
    const landedCalls = setLandedTs.mock.calls.length
    act(() => { vi.advanceTimersByTime(STEP_HIGHLIGHT_MS) })
    expect(setHighlightTs).toHaveBeenLastCalledWith(null)
    act(() => { vi.advanceTimersByTime(LANDED_HIGHLIGHT_MS) })
    expect(setLandedTs.mock.calls.length).toBe(landedCalls)
  })

  it('a jump after a turn step cancels the step flash expiry', () => {
    vi.useFakeTimers()
    const { result, setHighlightTs } = setup()
    act(() => result.current.markStep('prompt'))
    act(() => result.current.markJump('pin', false))
    act(() => { vi.advanceTimersByTime(STEP_HIGHLIGHT_MS) })
    expect(setHighlightTs).toHaveBeenLastCalledWith('pin')
    expect(result.current.jumpTarget).toEqual({ ts: 'pin', mid: null })
  })
})
