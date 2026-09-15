// Feature: chat-virtualizer — the ResizeObserver must observe rows that were
// already mounted when it was created.
//
// Row ref callbacks (`measureRef`) attach during the COMMIT phase; the shared
// ResizeObserver is created in a PASSIVE effect, which React runs after paint.
// Any row whose ref attached before that effect ran therefore called
// `ro?.observe` on a null observer. `measureRef` deliberately returns a
// STABLE per-index callback (so a row that stays mounted is never re-invoked
// and never churns observe/unobserve), which means such a row is never
// observed again for as long as it stays mounted — its streaming growth never
// reaches the observer, and follow silently stops working.
//
// The other suites in this directory leave `ResizeObserver` undefined so the
// RO path cannot perturb their pin assertions; this one installs a recording
// stub precisely to assert on the registration itself, and removes it again.

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { useRef, type RefObject } from 'react'

import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'

/** ResizeObserver stub that records which elements are under observation. */
class RecordingResizeObserver {
  static instances: RecordingResizeObserver[] = []
  readonly observed = new Set<Element>()
  constructor(readonly cb: ResizeObserverCallback) {
    RecordingResizeObserver.instances.push(this)
  }
  observe(el: Element) { this.observed.add(el) }
  unobserve(el: Element) { this.observed.delete(el) }
  disconnect() { this.observed.clear() }
}

interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

/**
 * Renders rows exactly as ChatPage does — `measureRef(index)` on each mounted
 * row — so the refs attach in the same commit that mounts the list, before the
 * hook's passive effects run.
 */
function Harness({ items }: { items: Item[] }) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const virt = useVirtualChat<Item>({
    items,
    sessionId: 'observer-backfill',
    getKey,
    externalScrollerRef: scrollerRef as RefObject<HTMLDivElement | null>,
  })
  return (
    <div ref={scrollerRef}>
      {virt.virtualItems.map((vi) =>
        vi.mounted ? (
          <div key={vi.key} ref={virt.measureRef(vi.index)} data-row={vi.index} />
        ) : null,
      )}
    </div>
  )
}

describe('useVirtualChat: ResizeObserver registration', () => {
  const original = globalThis.ResizeObserver

  beforeEach(() => {
    localStorage.clear()
    RecordingResizeObserver.instances.length = 0
    globalThis.ResizeObserver = RecordingResizeObserver as unknown as typeof ResizeObserver
  })

  afterEach(() => {
    globalThis.ResizeObserver = original
  })

  it('observes rows that mounted before the observer was created', () => {
    const { container } = render(<Harness items={mkItems(4)} />)

    const rows = Array.from(container.querySelectorAll('[data-row]'))
    expect(rows.length).toBeGreaterThan(0)

    expect(RecordingResizeObserver.instances).toHaveLength(1)
    const ro = RecordingResizeObserver.instances[0]

    // Every row present at mount must be under observation. Without the
    // back-fill these rows registered against a null observer and stay
    // unobserved forever, so their growth never fires the follow pin.
    for (const row of rows) expect(ro.observed.has(row)).toBe(true)
  })

  it('drops a row from observation when it unmounts', () => {
    const { container, rerender } = render(<Harness items={mkItems(4)} />)
    const ro = RecordingResizeObserver.instances[0]
    const firstRow = container.querySelector('[data-row]')!
    expect(ro.observed.has(firstRow)).toBe(true)

    // An empty transcript unmounts every row. The back-fill must not resurrect
    // a detached node on a later pass.
    act(() => { rerender(<Harness items={[]} />) })

    expect(ro.observed.has(firstRow)).toBe(false)
  })
})
