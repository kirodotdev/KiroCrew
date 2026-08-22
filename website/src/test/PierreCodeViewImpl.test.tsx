/**
 * PierreCodeViewImpl — the wrapper's own logic around `@pierre/diffs` CodeView.
 *
 * `fileAtScrollTop` (the pure viewport mapping) is pinned separately in
 * pierreCodeViewScroll.test.ts. This file covers the COMPONENT half with the
 * library replaced by a recording fake: how per-file patch payloads become
 * versioned CodeView items, how the path-keyed header slots wrap Pierre's
 * item-keyed ones, and the imperative scroll surface — the smooth tree-click
 * scroll with its sticky-offset compensation and the one-shot instant landing
 * correction after the scroll stream settles.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { createRef, forwardRef, useImperativeHandle } from 'react'
import type { ReactNode } from 'react'

/** Recording stand-in for `@pierre/diffs/react`'s CodeView. Renders nothing
 *  real (the library targets its own DOM), records every props update and
 *  `scrollTo` call, and lets a test drive the `onScroll` stream with a
 *  scripted viewer. */
type Viewer = { getTopForItem(id: string): number | undefined }
type CodeViewProps = {
  items: Array<{ id: string; version: number; collapsed?: boolean; fileDiff: unknown }>
  options: Record<string, unknown> & { itemMetrics?: { spacing?: number } }
  onScroll?: (scrollTop: number, viewer: Viewer) => void
  renderHeaderPrefix?: (item: { id: string }) => ReactNode
  renderHeaderFilenameSuffix?: (item: { id: string }) => ReactNode
  renderHeaderMetadata?: (item: { id: string }) => ReactNode
  className?: string
}
const codeViewMock = {
  renders: [] as CodeViewProps[],
  scrollTo: [] as Array<Record<string, unknown>>,
  reset() {
    this.renders.length = 0
    this.scrollTo.length = 0
  },
  last(): CodeViewProps {
    const props = this.renders.at(-1)
    if (!props) throw new Error('CodeView never rendered')
    return props
  },
  /** Deliver one scroll event exactly as the library would. */
  scroll(scrollTop: number, tops: Record<string, number | undefined>) {
    this.last().onScroll?.(scrollTop, { getTopForItem: id => tops[id] })
  },
}

vi.mock('@pierre/diffs/react', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    CodeView: forwardRef(function FakeCodeView(props: CodeViewProps, ref) {
      codeViewMock.renders.push(props)
      useImperativeHandle(ref, () => ({
        scrollTo: (target: Record<string, unknown>) => codeViewMock.scrollTo.push(target),
      }))
      return <div data-testid="code-view" />
    }),
  }
})
vi.mock('../hooks/useIsDark', () => ({ useIsDark: () => false }))

import { PierreCodeViewImpl, type PierreCodeViewScrollHandle, type PierrePatchFile } from '../pierre/PierreCodeViewImpl'

const HUNK = '@@ -1,2 +1,2 @@\n-old line\n+new line\n context\n'

function renderView(files: readonly PierrePatchFile[], extra: Record<string, unknown> = {}) {
  const ref = createRef<PierreCodeViewScrollHandle>()
  const view = render(<PierreCodeViewImpl ref={ref} files={files} {...extra} />)
  return {
    ref,
    ...view,
    update: (nextFiles: readonly PierrePatchFile[], nextExtra: Record<string, unknown> = extra) =>
      view.rerender(<PierreCodeViewImpl ref={ref} files={nextFiles} {...nextExtra} />),
  }
}

beforeEach(() => {
  codeViewMock.reset()
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('items', () => {
  it('turns bare provider hunks into parsed diff items with the path as identity', () => {
    renderView([
      { path: 'src/a.ts', patch: HUNK, status: 'modified' },
      { path: 'src/b.ts', patch: HUNK, status: 'added' },
    ])
    const items = codeViewMock.last().items
    expect(items.map(i => i.id)).toEqual(['src/a.ts', 'src/b.ts'])
    for (const item of items) {
      expect(item.fileDiff).toBeTruthy()
      expect(item.version).toBe(1)
      expect(item.collapsed).toBe(false)
    }
  })

  it('renders a file whose patch was withheld as a header-only item instead of dropping it', () => {
    renderView([{ path: 'assets/logo.png', status: 'modified' }])
    const items = codeViewMock.last().items
    expect(items.map(i => i.id)).toEqual(['assets/logo.png'])
  })

  it('keeps an unchanged file at its version and bumps only what changed', () => {
    const { update } = renderView([
      { path: 'src/a.ts', patch: HUNK },
      { path: 'src/b.ts', patch: HUNK },
    ])
    update([
      { path: 'src/a.ts', patch: HUNK },
      { path: 'src/b.ts', patch: HUNK, collapsed: true },
    ])
    const byId = Object.fromEntries(codeViewMock.last().items.map(i => [i.id, i]))
    // Same payload, same collapse: the controlled CodeView must NOT re-read it.
    expect(byId['src/a.ts'].version).toBe(1)
    // The collapse toggle is item state, so it moves the version.
    expect(byId['src/b.ts'].version).toBe(2)
    expect(byId['src/b.ts'].collapsed).toBe(true)
  })

  it('bumps the version for a changed patch even when the content hash collides', () => {
    // '+Aa' and '+B@' are a classic djb2 collision pair (same length, same
    // digest under contentCacheKey), so a signature derived from the HASH
    // would freeze the version here and CodeView would keep the stale diff.
    // The signature must come from the exact payload.
    const p1 = '@@ -1,1 +1,1 @@\n-x\n+Aa\n'
    const p2 = '@@ -1,1 +1,1 @@\n-x\n+B@\n'
    const { update } = renderView([{ path: 'src/a.ts', patch: p1 }])
    expect(codeViewMock.last().items[0].version).toBe(1)
    update([{ path: 'src/a.ts', patch: p2 }])
    expect(codeViewMock.last().items[0].version).toBe(2)
  })

  it('wraps the path-keyed header slots around Pierre item-keyed ones', () => {
    const metadata = vi.fn((path: string) => <span>{path}</span>)
    renderView([{ path: 'src/a.ts', patch: HUNK }], { renderHeaderMetadata: metadata })
    const props = codeViewMock.last()
    props.renderHeaderMetadata?.({ id: 'src/a.ts' })
    expect(metadata).toHaveBeenCalledWith('src/a.ts')
    // Slots the caller did not provide are not synthesized.
    expect(props.renderHeaderPrefix).toBeUndefined()
    expect(props.renderHeaderFilenameSuffix).toBeUndefined()
  })
})

describe('scrolling', () => {
  it('forwards the raw offset and reports the viewport file only when it changes', () => {
    const onScroll = vi.fn()
    const onViewportFileChange = vi.fn()
    renderView(
      [{ path: 'a.ts', patch: HUNK }, { path: 'b.ts', patch: HUNK }],
      { onScroll, onViewportFileChange },
    )
    const tops = { 'a.ts': 0, 'b.ts': 500 }
    act(() => codeViewMock.scroll(10, tops))
    act(() => codeViewMock.scroll(20, tops))
    act(() => codeViewMock.scroll(600, tops))
    expect(onScroll.mock.calls.map(c => c[0])).toEqual([10, 20, 600])
    // Two offsets inside a.ts collapse to one report; crossing into b.ts adds one.
    expect(onViewportFileChange.mock.calls.map(c => c[0])).toEqual(['a.ts', 'b.ts'])
  })

  it('scrollToFile navigates smoothly with the sticky-offset compensation', () => {
    const { ref } = renderView([{ path: 'a.ts', patch: HUNK }])
    act(() => ref.current?.scrollToFile('a.ts'))
    expect(codeViewMock.scrollTo).toEqual([
      { type: 'item', id: 'a.ts', align: 'start', behavior: 'smooth', offset: -8 },
    ])
  })

  it('closes a small landing residue with one instant correction after the stream settles', () => {
    vi.useFakeTimers()
    const { ref } = renderView([{ path: 'a.ts', patch: HUNK }, { path: 'b.ts', patch: HUNK }])
    act(() => ref.current?.scrollToFile('b.ts'))
    expect(codeViewMock.scrollTo).toHaveLength(1)
    // The animation settles 2px short of the resolved target (top - spacing).
    act(() => codeViewMock.scroll(490, { 'a.ts': 0, 'b.ts': 500 }))
    act(() => vi.advanceTimersByTime(160))
    expect(codeViewMock.scrollTo).toHaveLength(2)
    expect(codeViewMock.scrollTo.at(-1)).toMatchObject({ id: 'b.ts', behavior: 'instant', offset: -8 })
  })

  it('leaves a large residue alone — the reader grabbed the scrollbar and won', () => {
    vi.useFakeTimers()
    const { ref } = renderView([{ path: 'a.ts', patch: HUNK }, { path: 'b.ts', patch: HUNK }])
    act(() => ref.current?.scrollToFile('b.ts'))
    act(() => codeViewMock.scroll(300, { 'a.ts': 0, 'b.ts': 500 }))
    act(() => vi.advanceTimersByTime(160))
    expect(codeViewMock.scrollTo).toHaveLength(1)
  })

  it('fires the correction once per navigation, from the LAST scroll event', () => {
    vi.useFakeTimers()
    const { ref } = renderView([{ path: 'a.ts', patch: HUNK }, { path: 'b.ts', patch: HUNK }])
    act(() => ref.current?.scrollToFile('b.ts'))
    const tops = { 'a.ts': 0, 'b.ts': 500 }
    act(() => codeViewMock.scroll(200, tops))
    act(() => vi.advanceTimersByTime(100)) // debounced: not yet
    act(() => codeViewMock.scroll(490, tops))
    act(() => vi.advanceTimersByTime(160))
    expect(codeViewMock.scrollTo).toHaveLength(2)
    // Later plain scrolling never re-arms it.
    act(() => codeViewMock.scroll(100, tops))
    act(() => vi.advanceTimersByTime(500))
    expect(codeViewMock.scrollTo).toHaveLength(2)
  })

  it('pins the viewport report to the clicked file for the whole travel', () => {
    vi.useFakeTimers()
    const onViewportFileChange = vi.fn()
    const { ref } = renderView(
      [{ path: 'a.ts', patch: HUNK }, { path: 'b.ts', patch: HUNK }, { path: 'c.ts', patch: HUNK }],
      { onViewportFileChange },
    )
    act(() => ref.current?.scrollToFile('c.ts'))
    // Reported once, at click time — the tree lands on the destination now.
    expect(onViewportFileChange.mock.calls.map(c => c[0])).toEqual(['c.ts'])
    // The travel crosses b.ts; that crossing is NOT echoed.
    const tops = { 'a.ts': 0, 'b.ts': 500, 'c.ts': 1000 }
    act(() => codeViewMock.scroll(500, tops))
    act(() => codeViewMock.scroll(992, tops))
    expect(onViewportFileChange.mock.calls.map(c => c[0])).toEqual(['c.ts'])
    // After the stream settles, plain scrolling follows the reader again.
    act(() => vi.advanceTimersByTime(160))
    act(() => codeViewMock.scroll(510, tops))
    expect(onViewportFileChange.mock.calls.map(c => c[0])).toEqual(['c.ts', 'b.ts'])
  })

  it('drops the pin the moment the reader scrolls outside the travel corridor', () => {
    vi.useFakeTimers()
    const onViewportFileChange = vi.fn()
    const { ref } = renderView(
      [{ path: 'a.ts', patch: HUNK }, { path: 'b.ts', patch: HUNK }],
      { onViewportFileChange },
    )
    const tops = { 'a.ts': 0, 'b.ts': 5000 }
    act(() => codeViewMock.scroll(100, tops))
    onViewportFileChange.mockClear()
    act(() => ref.current?.scrollToFile('b.ts'))
    expect(onViewportFileChange.mock.calls.map(c => c[0])).toEqual(['b.ts'])
    // First event fixes the corridor start; a position behind it by more than
    // the slack is the reader taking over, and following resumes immediately.
    act(() => codeViewMock.scroll(100, tops))
    act(() => codeViewMock.scroll(20, tops))
    expect(onViewportFileChange.mock.calls.map(c => c[0])).toEqual(['b.ts', 'a.ts'])
  })

  it('clears the pin on its own when the click needs no scroll at all', () => {
    vi.useFakeTimers()
    const onViewportFileChange = vi.fn()
    const { ref } = renderView(
      [{ path: 'a.ts', patch: HUNK }, { path: 'b.ts', patch: HUNK }],
      { onViewportFileChange },
    )
    act(() => ref.current?.scrollToFile('a.ts'))
    // No scroll event ever fires; the fallback timer releases the pin so later
    // reading is followed normally.
    act(() => vi.advanceTimersByTime(400))
    act(() => codeViewMock.scroll(600, { 'a.ts': 0, 'b.ts': 500 }))
    expect(onViewportFileChange.mock.calls.map(c => c[0])).toEqual(['a.ts', 'b.ts'])
  })
})
