// Feature: side-panel scroll memory (issue #5701) — a chat-slot switch
// unmounts the previous slot's tab bodies, so a document the user had
// scrolled remounts at scrollTop = 0. `useScrollMemory` remembers the
// position in a module-scope map (keyed slot + tab id) and restores it on
// the remount, one-shot, once content is ready.
import { describe, it, expect, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { useRef } from 'react'
import { useScrollMemory, _resetScrollMemory } from '../hooks/useScrollMemory'

/** Minimal scroll-container consumer, shaped like the real call sites
 *  (ArtifactPanel / MarkdownPanel): the hook gets the caller's own ref plus a
 *  readiness flag, and the returned onScroll is attached as a React prop. */
function Scroller({ memoryKey, ready = true, suppressRestore = false }: {
  memoryKey: string | null
  ready?: boolean
  suppressRestore?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const { onScroll } = useScrollMemory(memoryKey, ref, ready, { suppressRestore })
  return <div data-testid="scroller" ref={ref} onScroll={onScroll} />
}

/** happy-dom has no layout engine, so scrollTop is a plain settable number —
 *  exactly what the hook reads and writes. */
function scrollTo(el: HTMLElement, top: number) {
  el.scrollTop = top
  fireEvent.scroll(el)
}

const getScroller = (r: { getByTestId: (id: string) => HTMLElement }) => r.getByTestId('scroller')

beforeEach(() => _resetScrollMemory())

describe('useScrollMemory', () => {
  it('restores the recorded position on a remount with the same key', () => {
    const first = render(<Scroller memoryKey={'slot-a\u001Ftab-1'} />)
    scrollTo(getScroller(first), 420)
    first.unmount()

    const second = render(<Scroller memoryKey={'slot-a\u001Ftab-1'} />)
    expect(getScroller(second).scrollTop).toBe(420)
  })

  it('does not leak positions across keys (another slot, another tab)', () => {
    const first = render(<Scroller memoryKey={'slot-a\u001Ftab-1'} />)
    scrollTo(getScroller(first), 420)
    first.unmount()

    const other = render(<Scroller memoryKey={'slot-b\u001Ftab-1'} />)
    expect(getScroller(other).scrollTop).toBe(0)
  })

  it('waits for ready before restoring, then restores once', () => {
    const first = render(<Scroller memoryKey="k" />)
    scrollTo(getScroller(first), 300)
    first.unmount()

    // Remount in the loading state: nothing to restore into yet.
    const second = render(<Scroller memoryKey="k" ready={false} />)
    expect(getScroller(second).scrollTop).toBe(0)

    // Content commits → the one-shot restore fires.
    second.rerender(<Scroller memoryKey="k" ready />)
    expect(getScroller(second).scrollTop).toBe(300)
  })

  it('is one-shot per mount: a later content refresh cannot yank the position the user chose', () => {
    const first = render(<Scroller memoryKey="k" />)
    scrollTo(getScroller(first), 300)
    first.unmount()

    const second = render(<Scroller memoryKey="k" />)
    const el = getScroller(second)
    expect(el.scrollTop).toBe(300)

    // User scrolls back to the top, then the content re-renders (file watch,
    // version bump). The stored 300 must NOT be re-applied.
    scrollTo(el, 0)
    second.rerender(<Scroller memoryKey="k" ready />)
    expect(el.scrollTop).toBe(0)
  })

  it('suppressRestore burns the latch: an explicit reveal target outranks memory for the whole mount', () => {
    const first = render(<Scroller memoryKey="k" />)
    scrollTo(getScroller(first), 500)
    first.unmount()

    // Remount carrying a line reveal: memory must not fire now…
    const second = render(<Scroller memoryKey="k" suppressRestore />)
    const el = getScroller(second)
    expect(el.scrollTop).toBe(0)

    // …and must not fire later either, when the reveal is consumed.
    second.rerender(<Scroller memoryKey="k" suppressRestore={false} />)
    expect(el.scrollTop).toBe(0)

    // Recording kept working throughout: the next remount restores the
    // position from THIS mount, not the pre-reveal one.
    scrollTo(el, 120)
    second.unmount()
    const third = render(<Scroller memoryKey="k" />)
    expect(getScroller(third).scrollTop).toBe(120)
  })

  it('a null key disables both recording and restoring', () => {
    const first = render(<Scroller memoryKey={null} />)
    scrollTo(getScroller(first), 250)
    first.unmount()

    const second = render(<Scroller memoryKey={null} />)
    expect(getScroller(second).scrollTop).toBe(0)
  })

  it('re-arms the latch when the key changes in place (rail re-targets a file tab)', () => {
    // Record a position for the SECOND document, from an earlier visit.
    const earlier = render(<Scroller memoryKey={'slot\u001Fdoc-2'} />)
    scrollTo(getScroller(earlier), 640)
    earlier.unmount()

    // A mount showing doc-1 restores (nothing recorded → stays at 0), then
    // the rail re-targets the same mount to doc-2: the latch re-arms and
    // doc-2's remembered position lands.
    const r = render(<Scroller memoryKey={'slot\u001Fdoc-1'} />)
    const el = getScroller(r)
    expect(el.scrollTop).toBe(0)
    r.rerender(<Scroller memoryKey={'slot\u001Fdoc-2'} />)
    expect(el.scrollTop).toBe(640)
  })

  it('does not write scrollTop at all when nothing meaningful is recorded', () => {
    // A remembered 0 is indistinguishable from the default — the hook skips
    // the write so it can never disturb a host that scrolled programmatically
    // between mount and ready (e.g. a comment scroll-to).
    const first = render(<Scroller memoryKey="k" />)
    scrollTo(getScroller(first), 0)
    first.unmount()

    const second = render(<Scroller memoryKey="k" ready={false} />)
    const el = getScroller(second)
    el.scrollTop = 90 // programmatic scroll before ready
    second.rerender(<Scroller memoryKey="k" ready />)
    expect(el.scrollTop).toBe(90)
  })

  it('caps the store FIFO so a weeks-old dashboard cannot grow it unbounded', () => {
    // Fill beyond the cap under distinct keys, then confirm the oldest entry
    // was evicted (restores 0) while the newest survives.
    const seed = render(<Scroller memoryKey="key-0" />)
    scrollTo(getScroller(seed), 111)
    seed.unmount()

    for (let i = 1; i <= 500; i++) {
      const r = render(<Scroller memoryKey={`key-${i}`} />)
      scrollTo(getScroller(r), 10 + i)
      r.unmount()
    }

    const evicted = render(<Scroller memoryKey="key-0" />)
    expect(getScroller(evicted).scrollTop).toBe(0)
    evicted.unmount()

    const kept = render(<Scroller memoryKey="key-500" />)
    expect(getScroller(kept).scrollTop).toBe(510)
  })
})
