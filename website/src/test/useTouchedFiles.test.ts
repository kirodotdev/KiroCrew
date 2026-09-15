import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useTouchedFiles } from '../hooks/useTouchedFiles'

beforeEach(() => localStorage.clear())

describe('useTouchedFiles', () => {
  it('adds a file with source and timestamp', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts', 'tool'))
    expect(result.current.files).toHaveLength(1)
    expect(result.current.files[0].path).toBe('/tmp/a.ts')
    expect(result.current.files[0].source).toBe('tool')
    expect(result.current.files[0].ts).toBeGreaterThan(0)
  })

  it('sets lastWrite on initial add when source is tool', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts', 'tool'))
    expect(result.current.files[0].lastWrite).toBeGreaterThan(0)
  })

  it('does not set lastWrite on initial add when source is history', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts', 'history'))
    expect(result.current.files[0].lastWrite).toBeUndefined()
  })

  it('bumps lastWrite on re-touch with source=tool', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts', 'tool'))
    const first = result.current.files[0].lastWrite!
    act(() => result.current.addFile('/tmp/a.ts', 'tool'))
    expect(result.current.files[0].lastWrite).toBeGreaterThanOrEqual(first)
    expect(result.current.files).toHaveLength(1)
  })

  it('promotes tool→history on re-touch with source=history', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts', 'tool'))
    act(() => result.current.addFile('/tmp/a.ts', 'history'))
    expect(result.current.files[0].source).toBe('history')
    expect(result.current.files).toHaveLength(1)
  })

  it('does not duplicate files on re-add', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts', 'history'))
    act(() => result.current.addFile('/tmp/a.ts', 'history'))
    expect(result.current.files).toHaveLength(1)
  })

  it('removes a file', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts'))
    act(() => result.current.removeFile('/tmp/a.ts'))
    expect(result.current.files).toHaveLength(0)
  })

  it('clears all files', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts'))
    act(() => result.current.addFile('/tmp/b.ts'))
    act(() => result.current.clear())
    expect(result.current.files).toHaveLength(0)
  })

  it('persists to localStorage', () => {
    const { result } = renderHook(() => useTouchedFiles('test-session'))
    act(() => result.current.addFile('/tmp/a.ts', 'tool'))
    const stored = JSON.parse(localStorage.getItem('kirocrew:touched-files:test-session')!)
    expect(stored).toHaveLength(1)
    expect(stored[0].path).toBe('/tmp/a.ts')
  })

  it('loads from localStorage on mount', () => {
    localStorage.setItem('kirocrew:touched-files:s1', JSON.stringify([{ path: '/x.ts', ts: 1, source: 'tool' }]))
    const { result } = renderHook(() => useTouchedFiles('s1'))
    expect(result.current.files).toHaveLength(1)
    expect(result.current.files[0].path).toBe('/x.ts')
  })
})
