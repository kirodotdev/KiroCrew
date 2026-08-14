/**
 * usePanelState / useDiffPanel — the two side-panel state hooks.
 *
 * Both promise a STABLE returned object between renders (consumers put it in
 * dependency arrays), plus full reset on close, so both are asserted here
 * alongside the open/close transitions.
 */
import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { usePanelState, useDiffPanel } from './usePanelState'

describe('usePanelState', () => {
  it('starts closed and empty', () => {
    const { result } = renderHook(() => usePanelState())
    expect(result.current.isOpen).toBe(false)
    expect(result.current.filePath).toBe('')
    expect(result.current.content).toBe('')
    expect(result.current.slot).toBeNull()
  })

  it('openPanel records path, content and the originating slot', () => {
    const { result } = renderHook(() => usePanelState())
    act(() => result.current.openPanel('zzq/notes.md', 'zzq body', 'zzq-slot-1'))
    expect(result.current.isOpen).toBe(true)
    expect(result.current.filePath).toBe('zzq/notes.md')
    expect(result.current.content).toBe('zzq body')
    expect(result.current.slot).toBe('zzq-slot-1')
  })

  it('defaults the origin slot to null when the caller omits it', () => {
    const { result } = renderHook(() => usePanelState())
    act(() => result.current.openPanel('zzq/a.md', 'zzq'))
    expect(result.current.slot).toBeNull()
  })

  it('setContent updates the buffer without closing the panel', () => {
    const { result } = renderHook(() => usePanelState())
    act(() => result.current.openPanel('zzq/a.md', 'zzq one'))
    act(() => result.current.setContent('zzq two'))
    expect(result.current.content).toBe('zzq two')
    expect(result.current.isOpen).toBe(true)
  })

  it('closePanel clears every field', () => {
    const { result } = renderHook(() => usePanelState())
    act(() => result.current.openPanel('zzq/a.md', 'zzq', 'zzq-slot-1'))
    act(() => result.current.closePanel())
    expect(result.current).toMatchObject({
      isOpen: false,
      filePath: '',
      content: '',
      slot: null,
    })
  })

  it('returns a referentially stable object across an unrelated re-render', () => {
    const { result, rerender } = renderHook(() => usePanelState())
    const before = result.current
    rerender()
    expect(result.current).toBe(before)
  })
})

describe('useDiffPanel', () => {
  it('starts closed and empty', () => {
    const { result } = renderHook(() => useDiffPanel())
    expect(result.current.isOpen).toBe(false)
    expect(result.current.filePath).toBe('')
    expect(result.current.original).toBe('')
    expect(result.current.modified).toBe('')
  })

  it('openDiff stores modified + original', () => {
    const { result } = renderHook(() => useDiffPanel())
    act(() => result.current.openDiff('zzq/x.ts', 'zzq new', 'zzq old'))
    expect(result.current.isOpen).toBe(true)
    expect(result.current.filePath).toBe('zzq/x.ts')
    expect(result.current.modified).toBe('zzq new')
    expect(result.current.original).toBe('zzq old')
  })

  it('treats a missing original as empty (added file)', () => {
    const { result } = renderHook(() => useDiffPanel())
    act(() => result.current.openDiff('zzq/x.ts', 'zzq new'))
    expect(result.current.original).toBe('')
  })

  it('closeDiff clears every field', () => {
    const { result } = renderHook(() => useDiffPanel())
    act(() => result.current.openDiff('zzq/x.ts', 'zzq new', 'zzq old'))
    act(() => result.current.closeDiff())
    expect(result.current).toMatchObject({
      isOpen: false,
      filePath: '',
      original: '',
      modified: '',
    })
  })

  it('returns a referentially stable object across an unrelated re-render', () => {
    const { result, rerender } = renderHook(() => useDiffPanel())
    const before = result.current
    rerender()
    expect(result.current).toBe(before)
  })
})
