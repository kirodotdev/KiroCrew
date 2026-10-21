import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useTerminalSearch } from '../hooks/useTerminalSearch'

/* useTerminalSearch (#10176) drives xterm's SearchAddon for one pane. These
 * tests use a fake addon + term: the addon search itself is xterm's, so what is
 * under test is the hook's wiring — which query/options reach findNext, that an
 * empty query clears, that onDidChangeResults feeds the count, and that
 * open/close manage focus + highlights. */

type ResultsCb = (r: { resultIndex: number; resultCount: number }) => void

function makeFakes() {
  let resultsCb: ResultsCb | null = null
  const search = {
    findNext: vi.fn(),
    findPrevious: vi.fn(),
    clearDecorations: vi.fn(),
    onDidChangeResults: vi.fn((cb: ResultsCb) => { resultsCb = cb; return { dispose: vi.fn() } }),
  }
  const term = { focus: vi.fn() }
  return { search, term, emitResults: (r: { resultIndex: number; resultCount: number }) => resultsCb?.(r) }
}

// The hook uses requestAnimationFrame to defer focus + searches; run it
// synchronously so a single act() flush settles everything.
beforeEach(() => {
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => { cb(0); return 0 })
})
afterEach(() => vi.restoreAllMocks())

describe('useTerminalSearch', () => {
  it('starts closed with an empty query and no results', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    expect(result.current.open).toBe(false)
    expect(result.current.query).toBe('')
    expect(result.current.resultCount).toBe(0)
    expect(result.current.resultIndex).toBe(-1)
  })

  it('openSearch opens the bar', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => result.current.openSearch())
    expect(result.current.open).toBe(true)
  })

  it('setQuery runs findNext with the query and the current options', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('error') })
    expect(result.current.query).toBe('error')
    expect(search.findNext).toHaveBeenCalled()
    const [term0, opts] = search.findNext.mock.calls.at(-1)!
    expect(term0).toBe('error')
    // Decorations drive the addon's match count, so they are always supplied.
    // No case/word/regex options — search uses the addon defaults.
    expect(opts.decorations).toBeTruthy()
  })

  it('an empty query clears decorations and zeroes the count', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('x') })
    search.clearDecorations.mockClear()
    act(() => result.current.setQuery(''))
    expect(search.clearDecorations).toHaveBeenCalled()
    expect(result.current.resultCount).toBe(0)
  })

  it('findNext / findPrevious call through to the addon', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('q') })
    search.findNext.mockClear()
    act(() => result.current.findNext())
    expect(search.findNext).toHaveBeenCalledWith('q', expect.anything())
    act(() => result.current.findPrevious())
    expect(search.findPrevious).toHaveBeenCalledWith('q', expect.anything())
  })

  it('types incrementally so find-as-you-type stays on the first match, but Enter/Next steps forward', () => {
    // addon-search starts findNext past the current selection unless
    // incremental keeps it anchored; the live typing path must pass
    // incremental:true so f → fo → foo lands on the first match, while the
    // explicit Next button must pass incremental:false so it advances.
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => result.current.openSearch())
    search.findNext.mockClear()
    act(() => result.current.setQuery('foo'))
    // The typing path searched incrementally.
    expect(search.findNext).toHaveBeenCalledWith('foo', expect.objectContaining({ incremental: true }))
    search.findNext.mockClear()
    act(() => result.current.findNext())
    // The explicit Next advanced (non-incremental). Assert on any call, since a
    // re-run effect may also fire an incremental call in the same flush.
    expect(search.findNext).toHaveBeenCalledWith('foo', expect.objectContaining({ incremental: false }))
    // incremental only affects findNext in the addon, so Prev never sets it.
    act(() => result.current.findPrevious())
    const prevOpts = search.findPrevious.mock.calls.at(-1)![1]
    expect(prevOpts.incremental).toBeUndefined()
  })

  it('tracks the match count from onDidChangeResults', () => {
    const { search, term, emitResults } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => emitResults({ resultIndex: 2, resultCount: 7 }))
    expect(result.current.resultIndex).toBe(2)
    expect(result.current.resultCount).toBe(7)
  })

  it('closeSearch clears decorations, closes, and refocuses the shell', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => result.current.openSearch())
    act(() => result.current.closeSearch())
    expect(result.current.open).toBe(false)
    expect(search.clearDecorations).toHaveBeenCalled()
    expect(term.focus).toHaveBeenCalled()
  })

  it('clears highlights when the pane goes hidden while the bar is open', () => {
    const { search, term } = makeFakes()
    const { result, rerender } = renderHook(
      ({ active }: { active: boolean }) => useTerminalSearch(term as never, search as never, active),
      { initialProps: { active: true } },
    )
    act(() => result.current.openSearch())
    search.clearDecorations.mockClear()
    rerender({ active: false })
    expect(search.clearDecorations).toHaveBeenCalled()
  })

  it('re-applies the query when a searched pane becomes active again', () => {
    const { search, term } = makeFakes()
    const { result, rerender } = renderHook(
      ({ active }: { active: boolean }) => useTerminalSearch(term as never, search as never, active),
      { initialProps: { active: true } },
    )
    act(() => { result.current.openSearch(); result.current.setQuery('err') })
    rerender({ active: false })   // hide: highlights dropped
    search.findNext.mockClear()
    rerender({ active: true })    // return: highlights must come back
    expect(search.findNext).toHaveBeenCalledWith('err', expect.anything())
  })

  it('swallows a thrown find (invalid regex) — clears and zeroes, never uncaught', () => {
    const { search, term } = makeFakes()
    // The addon throws on an incomplete regex pattern.
    search.findNext.mockImplementation(() => { throw new SyntaxError('Invalid regular expression') })
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('(') })
    // No uncaught error propagated; the stale highlights were cleared.
    expect(search.clearDecorations).toHaveBeenCalled()
    expect(result.current.resultCount).toBe(0)
    expect(result.current.resultIndex).toBe(-1)
  })

  it('a thrown findNext/findPrevious navigation is caught too', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('q') })
    search.findNext.mockImplementation(() => { throw new Error('boom') })
    search.findPrevious.mockImplementation(() => { throw new Error('boom') })
    // Neither throws out of the hook.
    expect(() => act(() => result.current.findNext())).not.toThrow()
    expect(() => act(() => result.current.findPrevious())).not.toThrow()
  })

  it('search options always carry opaque, distinct match decoration colours', () => {
    const { search, term } = makeFakes()
    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('e') })
    const [, opts] = search.findNext.mock.calls.at(-1)!
    const d = opts.decorations
    expect(d).toBeTruthy()
    // Opaque #RRGGBB (the addon rejects any other form), and the active match
    // is a different colour from an inactive one so the current match reads.
    expect(d.activeMatchBackground).toMatch(/^#[0-9a-fA-F]{6}$/)
    expect(d.matchBackground).toMatch(/^#[0-9a-fA-F]{6}$/)
    expect(d.activeMatchBackground).not.toBe(d.matchBackground)
  })

  it('reads the theme tokens: opaque accent for the active match, subtle tint flattened over --bg', () => {
    const root = document.documentElement.style
    root.setProperty('--accent', '#3b82f6')
    root.setProperty('--accent-subtle', 'rgba(59, 130, 246, 0.12)')
    root.setProperty('--bg', '#000000')
    try {
      const { search, term } = makeFakes()
      const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
      act(() => { result.current.openSearch(); result.current.setQuery('e') })
      const [, opts] = search.findNext.mock.calls.at(-1)!
      expect(opts.decorations.activeMatchBackground).toBe('#3b82f6')
      // 12% of (59,130,246) over black = (7,16,30).
      expect(opts.decorations.matchBackground).toBe('#07101e')
    } finally {
      root.removeProperty('--accent')
      root.removeProperty('--accent-subtle')
      root.removeProperty('--bg')
    }
  })

  it('clears the cached addon decorations on unmount (pane hidden via {open && ...})', () => {
    const { search, term } = makeFakes()
    const { result, unmount } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('foo') })
    search.clearDecorations.mockClear()
    unmount()
    // The addon outlives the hook (termCache), so the hook must clear its
    // decorations on unmount or stale highlights persist with no bar.
    expect(search.clearDecorations).toHaveBeenCalled()
  })

  it('a search queued just before close is cancelled and does not repaint after', () => {
    const { search, term } = makeFakes()
    // Deferred rAF for THIS test: queue callbacks instead of firing them, so a
    // frame can sit pending across the close (the default mock fires inline).
    const queue: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => {
      queue.push(cb); return queue.length
    })
    const cancelled = new Set<number>()
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((id: number) => { cancelled.add(id) })

    const { result } = renderHook(() => useTerminalSearch(term as never, search as never, true))
    act(() => { result.current.openSearch(); result.current.setQuery('err') })
    search.findNext.mockClear()
    act(() => result.current.closeSearch())
    // Flush any frame the deferred mock still holds; a cancelled one must not run.
    act(() => { queue.forEach((cb, i) => { if (!cancelled.has(i + 1)) cb(0) }) })
    expect(search.findNext).not.toHaveBeenCalled()
  })
})
