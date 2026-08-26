import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useTouchedFiles } from '../hooks/useTouchedFiles'

const KEY = 'kirocrew:touched-files:'
const WM = ':toolClearedAt'

describe('useTouchedFiles', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('starts with empty list when sessionKey is undefined', () => {
    const { result } = renderHook(() => useTouchedFiles(undefined))
    expect(result.current.files).toEqual([])
  })

  it('addFile appends a new entry', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/foo.ts', 'tool'))
    expect(result.current.files).toHaveLength(1)
    expect(result.current.files[0]).toMatchObject({ path: '/foo.ts', source: 'tool' })
    expect(result.current.files[0].ts).toBeGreaterThan(0)
  })

  it('addFile promotes tool→history when re-added with source=history', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/foo.ts', 'tool'))
    act(() => result.current.addFile('/foo.ts', 'history'))
    expect(result.current.files).toHaveLength(1)
    expect(result.current.files[0].source).toBe('history')
  })

  it('addFile defaults source to history when omitted', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/foo.ts'))
    expect(result.current.files[0].source).toBe('history')
  })

  it('persists files to localStorage scoped by sessionKey', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/persisted.ts', 'tool'))
    const raw = localStorage.getItem(KEY + 's1')
    expect(raw).toBeTruthy()
    const parsed = JSON.parse(raw!)
    expect(parsed).toHaveLength(1)
    expect(parsed[0].path).toBe('/persisted.ts')
  })

  it('rehydrates files from localStorage on mount', () => {
    localStorage.setItem(
      KEY + 's-restore',
      JSON.stringify([{ path: '/saved.ts', ts: 100, source: 'tool' }]),
    )
    const { result } = renderHook(() => useTouchedFiles('s-restore'))
    expect(result.current.files).toEqual([{ path: '/saved.ts', ts: 100, source: 'tool' }])
  })

  it('removeFile drops a single entry by path', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/a.ts', 'tool'))
    act(() => result.current.addFile('/b.ts', 'tool'))
    act(() => result.current.removeFile('/a.ts'))
    expect(result.current.files.map(f => f.path)).toEqual(['/b.ts'])
  })

  it('clearBySource("tool") removes only tool-sourced entries', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/a.ts', 'tool'))
    act(() => result.current.addFile('/b.ts', 'history'))
    act(() => result.current.clearBySource('tool'))
    expect(result.current.files.map(f => f.path)).toEqual(['/b.ts'])
  })

  it('clearBySource("history") removes only history-sourced entries', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/a.ts', 'tool'))
    act(() => result.current.addFile('/b.ts', 'history'))
    act(() => result.current.clearBySource('history'))
    expect(result.current.files.map(f => f.path)).toEqual(['/a.ts'])
  })

  it('clearBySource("tool") records a watermark in localStorage', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.clearBySource('tool'))
    const wm = localStorage.getItem(KEY + 's1' + WM)
    expect(wm).toBeTruthy()
    expect(Number(wm)).toBeGreaterThan(0)
  })

  it('clearBySource("history") does not record a watermark', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.clearBySource('history'))
    expect(localStorage.getItem(KEY + 's1' + WM)).toBeNull()
  })

  it('shouldScanAdd returns true when no watermark set', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    expect(result.current.shouldScanAdd(Date.now())).toBe(true)
  })

  it('shouldScanAdd returns false for messages older than the watermark', () => {
    localStorage.setItem(KEY + 's1' + WM, String(1_000_000))
    const { result } = renderHook(() => useTouchedFiles('s1'))
    expect(result.current.shouldScanAdd(500_000)).toBe(false)
    expect(result.current.shouldScanAdd(1_500_000)).toBe(true)
  })

  it('clear() wipes everything and removes per-session storage', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/a.ts', 'tool'))
    act(() => result.current.clear())
    expect(result.current.files).toEqual([])
    expect(localStorage.getItem(KEY + 's1')).toBeNull()
    // Tool watermark also recorded so post-refresh scan won't re-add.
    expect(localStorage.getItem(KEY + 's1' + WM)).toBeTruthy()
  })

  it('switching sessionKey resets state and reloads from new session', () => {
    localStorage.setItem(
      KEY + 's2',
      JSON.stringify([{ path: '/in-s2.ts', ts: 1, source: 'history' }]),
    )
    const { result, rerender } = renderHook(({ k }: { k: string | undefined }) => useTouchedFiles(k), {
      initialProps: { k: 's1' as string | undefined },
    })
    act(() => result.current.addFile('/in-s1.ts', 'tool'))
    expect(result.current.files.map(f => f.path)).toEqual(['/in-s1.ts'])
    rerender({ k: 's2' })
    expect(result.current.files.map(f => f.path)).toEqual(['/in-s2.ts'])
  })

  it('survives malformed JSON in localStorage by starting empty', () => {
    localStorage.setItem(KEY + 's-bad', '{not-json')
    const { result } = renderHook(() => useTouchedFiles('s-bad'))
    expect(result.current.files).toEqual([])
  })

  it('addFile does NOT demote history→tool on re-add', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/foo.ts', 'history'))
    act(() => result.current.addFile('/foo.ts', 'tool'))
    expect(result.current.files).toHaveLength(1)
    // tool re-touch updates lastWrite but does NOT change source
    expect(result.current.files[0].source).toBe('history')
  })

  it('addFile with source=tool bumps lastWrite on existing tool entry', () => {
    const { result } = renderHook(() => useTouchedFiles('s1'))
    act(() => result.current.addFile('/foo.ts', 'tool'))
    const first = result.current.files[0].lastWrite!
    act(() => result.current.addFile('/foo.ts', 'tool'))
    expect(result.current.files[0].lastWrite).toBeGreaterThanOrEqual(first)
    expect(result.current.files).toHaveLength(1)
  })
})
