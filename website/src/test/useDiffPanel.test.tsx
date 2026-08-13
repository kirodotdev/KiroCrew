import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useDiffPanel } from '../hooks/usePanelState'

describe('useDiffPanel', () => {
  it('starts closed with empty state', () => {
    const { result } = renderHook(() => useDiffPanel())
    expect(result.current.isOpen).toBe(false)
    expect(result.current.filePath).toBe('')
    expect(result.current.original).toBe('')
    expect(result.current.modified).toBe('')
  })

  it('openDiff sets path/original/modified and opens', () => {
    const { result } = renderHook(() => useDiffPanel())
    act(() => result.current.openDiff('/abs/foo.ts', 'modified content', 'original content'))
    expect(result.current.isOpen).toBe(true)
    expect(result.current.filePath).toBe('/abs/foo.ts')
    expect(result.current.original).toBe('original content')
    expect(result.current.modified).toBe('modified content')
  })

  it('original defaults to empty string when omitted', () => {
    const { result } = renderHook(() => useDiffPanel())
    act(() => result.current.openDiff('/abs/new.txt', 'new file content'))
    expect(result.current.original).toBe('')
    expect(result.current.modified).toBe('new file content')
  })

  it('closeDiff clears all state', () => {
    const { result } = renderHook(() => useDiffPanel())
    act(() => result.current.openDiff('/x.ts', 'after', 'before'))
    act(() => result.current.closeDiff())
    expect(result.current.isOpen).toBe(false)
    expect(result.current.filePath).toBe('')
    expect(result.current.original).toBe('')
    expect(result.current.modified).toBe('')
  })

  it('reopening with a different file replaces state', () => {
    const { result } = renderHook(() => useDiffPanel())
    act(() => result.current.openDiff('/a.ts', 'mod-a', 'orig-a'))
    act(() => result.current.openDiff('/b.ts', 'mod-b', 'orig-b'))
    expect(result.current.filePath).toBe('/b.ts')
    expect(result.current.original).toBe('orig-b')
    expect(result.current.modified).toBe('mod-b')
  })

  it('callbacks are stable across renders', () => {
    const { result, rerender } = renderHook(() => useDiffPanel())
    const open1 = result.current.openDiff
    const close1 = result.current.closeDiff
    rerender()
    expect(result.current.openDiff).toBe(open1)
    expect(result.current.closeDiff).toBe(close1)
  })
})
