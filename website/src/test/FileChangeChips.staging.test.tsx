/**
 * File-change rows bypass the shared Pierre staging queue.
 *
 * Closed rows render complete lightweight headers and mount no Pierre surface;
 * disclosure mounts only the selected row. The scheduler tests below remain
 * here because other Pierre consumers still rely on its eager budget, scroll
 * hold, cancellation, and remount latch.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act, fireEvent } from '@testing-library/react'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ oldFile }: { oldFile: { name: string } }) => (
    <div data-testid="pierre-pair" data-name={oldFile.name} />
  ),
}))

import FileChangeChips from '../components/FileChangeChips'
import {
  EAGER_ROWS,
  STAGE_SLICE_BUDGET_MS,
  STAGE_SCROLL_HOLD_MS,
  requestStage,
  __resetStagingForTests,
  __stagedWaitingCount,
} from '../components/pierreStaging'

const change = (i: number) => ({
  path: `/repo/file-${i}.ts`,
  before: `before ${i}`,
  after: `after ${i}`,
})

const mounted = (c: HTMLElement) => c.querySelectorAll('[data-testid="pierre-pair"]').length
const rows = (c: HTMLElement) => c.querySelectorAll('[data-testid^="fcc-row-"]').length

/** Drain every pending idle slice. The scheduler re-arms itself per batch, so
 *  one advance is not enough for a queue longer than `STAGE_BATCH`. */
function flushStaging(): void {
  for (let i = 0; i < 40; i++) act(() => { vi.advanceTimersByTime(1) })
}

beforeEach(() => {
  __resetStagingForTests()
  localStorage.clear()
  vi.useFakeTimers()
  cleanup()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('FileChangeChips bypasses Pierre staging', () => {
  it('renders every closed row immediately without mounting or queueing Pierre', () => {
    const count = EAGER_ROWS + 3
    const { container } = render(
      <FileChangeChips fileChanges={Array.from({ length: count }, (_, i) => change(i))} />,
    )
    expect({ rows: rows(container), mounted: mounted(container), queued: __stagedWaitingCount() })
      .toEqual({ rows: count, mounted: 0, queued: 0 })
    expect(container.querySelectorAll('[data-testid^="fcc-header-"]')).toHaveLength(count)
  })

  it('mounts only the row the reader opens', () => {
    const count = EAGER_ROWS + 3
    const { container } = render(
      <FileChangeChips fileChanges={Array.from({ length: count }, (_, i) => change(i))} />,
    )
    const path = `/repo/file-${count - 1}.ts`
    fireEvent.click(container.querySelector(`[data-testid="fcc-toggle-${path}"]`) as HTMLElement)
    expect({ mounted: mounted(container), queued: __stagedWaitingCount() })
      .toEqual({ mounted: 1, queued: 0 })
  })

  it('remounts closed rows as lightweight headers without touching the staging queue', () => {
    const changes = Array.from({ length: EAGER_ROWS + 3 }, (_, i) => change(i))
    const first = render(<FileChangeChips fileChanges={changes} />)
    expect(mounted(first.container)).toBe(0)
    first.unmount()
    const second = render(<FileChangeChips fileChanges={changes} />)
    expect({ mounted: mounted(second.container), queued: __stagedWaitingCount() })
      .toEqual({ mounted: 0, queued: 0 })
  })
})

describe('the staging queue', () => {
  it('spends the eager budget, then queues', () => {
    const seen: number[] = []
    for (let i = 0; i < EAGER_ROWS + 2; i++) requestStage(() => seen.push(i))
    expect({ released: seen, queued: __stagedWaitingCount() })
      .toEqual({ released: Array.from({ length: EAGER_ROWS }, (_, i) => i), queued: 2 })
  })

  it('drains newest-first, so the eager budget is spent where the reader is looking', () => {
    const seen: number[] = []
    for (let i = 0; i < EAGER_ROWS + 3; i++) requestStage(() => seen.push(i))
    act(() => { vi.advanceTimersByTime(1) })
    // React mounts in DOM order, so the highest index is the bottom-most row.
    expect(seen.slice(EAGER_ROWS)).toEqual([EAGER_ROWS + 2, EAGER_ROWS + 1, EAGER_ROWS])
  })

  it('holds the drain while scrolling, resumes a hold-window after it stops', () => {
    // A release repaints and can mount ~90ms of Pierre — felt as hitching
    // under a momentum scroll. Any scroll (document-level capture) parks the
    // drain; it resumes one hold window after the last scroll event.
    const seen: number[] = []
    for (let i = 0; i < EAGER_ROWS; i++) requestStage(() => seen.push(i))
    requestStage(() => seen.push(100))
    // Mark scrolling NOW: the queued release must not drain.
    document.dispatchEvent(new Event('scroll'))
    act(() => { vi.advanceTimersByTime(STAGE_SCROLL_HOLD_MS - 50) })
    expect(seen).not.toContain(100)
    // Scroll settled: the delayed retry drains it.
    act(() => { vi.advanceTimersByTime(STAGE_SCROLL_HOLD_MS * 2 + 20) })
    expect(seen).toContain(100)
  })

  it('budget-bounds each slice: an expensive release ends its slice', () => {
    // A release triggers the registrant's REAL mount synchronously (~90ms for
    // a Pierre surface), so the drain is TIME-budgeted, not count-batched: one
    // expensive release exhausts the slice and the rest wait for the next one.
    // The clock is a spy (never a spin-wait: fake timers freeze performance.now,
    // which turns a spin into a hang); each release advances it past the budget.
    const seen: number[] = []
    let fakeNow = 0
    const nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => fakeNow)
    try {
      for (let i = 0; i < EAGER_ROWS; i++) requestStage(() => seen.push(i))
      for (let i = 0; i < 3; i++) {
        requestStage(() => { fakeNow += STAGE_SLICE_BUDGET_MS + 1; seen.push(100 + i) })
      }
      act(() => { vi.advanceTimersToNextTimer() })
      expect(seen.length - EAGER_ROWS).toBe(1)
      act(() => { vi.advanceTimersToNextTimer() })
      expect(seen.length - EAGER_ROWS).toBe(2)
    } finally {
      nowSpy.mockRestore()
    }
  })

  it('does not release a row that unmounted while queued', () => {
    const seen: string[] = []
    for (let i = 0; i < EAGER_ROWS; i++) requestStage(() => seen.push(`eager-${i}`))
    const cancel = requestStage(() => seen.push('gone'))
    requestStage(() => seen.push('stays'))
    cancel()
    flushStaging()
    expect(seen).not.toContain('gone')
    expect(seen).toContain('stays')
  })

  it('restores the eager budget once the queue empties, so a live turn does not stage', () => {
    const seen: string[] = []
    for (let i = 0; i < EAGER_ROWS + 1; i++) requestStage(() => seen.push(`first-${i}`))
    flushStaging()
    // A later turn appends one row; it must mount in its own commit, not shimmer.
    let immediate = false
    requestStage(() => { immediate = true })
    expect({ immediate, queued: __stagedWaitingCount() }).toEqual({ immediate: true, queued: 0 })
  })
})
