/**
 * useReadingWidth — the md/full preview-width toggle.
 *
 * Covers the persisted-read on mount, the toggle in both directions, and the
 * `previewStyle` that only exists in `md` (full-width mode must return
 * `undefined` so the preview inherits the container width).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'

const safeSetItem = vi.hoisted(() => vi.fn())
vi.mock('../utils/safeStorage', () => ({ safeSetItem }))

import { useReadingWidth } from './useReadingWidth'

const LS_KEY = 'mc-reading-width'

beforeEach(() => {
  localStorage.removeItem(LS_KEY)
  safeSetItem.mockClear()
})

describe('useReadingWidth', () => {
  it('defaults to md and centres the preview at the content width', () => {
    const { result } = renderHook(() => useReadingWidth())
    expect(result.current.readingWidth).toBe('md')
    expect(result.current.previewStyle).toEqual({
      maxWidth: 'var(--mc-content-width, 900px)',
      margin: '0 auto',
    })
  })

  it('restores full from localStorage and drops the width cap', () => {
    localStorage.setItem(LS_KEY, 'full')
    const { result } = renderHook(() => useReadingWidth())
    expect(result.current.readingWidth).toBe('full')
    expect(result.current.previewStyle).toBeUndefined()
  })

  it('treats an unrecognised stored value as md', () => {
    localStorage.setItem(LS_KEY, 'zzq-bogus')
    const { result } = renderHook(() => useReadingWidth())
    expect(result.current.readingWidth).toBe('md')
  })

  it('toggle flips md → full and persists through safeSetItem', () => {
    const { result } = renderHook(() => useReadingWidth())
    act(() => result.current.toggle())
    expect(result.current.readingWidth).toBe('full')
    expect(result.current.previewStyle).toBeUndefined()
    expect(safeSetItem).toHaveBeenCalledWith(LS_KEY, 'full')
  })

  it('toggle flips full → md and persists that too', () => {
    localStorage.setItem(LS_KEY, 'full')
    const { result } = renderHook(() => useReadingWidth())
    act(() => result.current.toggle())
    expect(result.current.readingWidth).toBe('md')
    expect(safeSetItem).toHaveBeenLastCalledWith(LS_KEY, 'md')
  })
})
