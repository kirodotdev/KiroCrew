/**
 * Staged mounting for file-change diff rows.
 *
 * The cost being spread: each row mounts a Pierre file-pair, and mounting all of
 * a switch's rows in one commit measured ~950ms of main-thread blocking with a
 * single 474ms task across 28 rows. Staging releases them in idle slices.
 *
 * The hazard being guarded: releasing a row must not change its height, or the
 * transcript moves under the reader — a scroll jump per released row. That is
 * why `STAGED_ROW_HEIGHT_PX` is asserted against the placeholder's own reserved
 * height here rather than left to the utility class to keep.
 *
 * Pierre paints behind a lazy chunk that never resolves under vitest, so
 * `PierreFilePair` is mocked and its presence in the DOM means "this row
 * mounted".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, act, fireEvent } from '@testing-library/react'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ oldFile }: { oldFile: { name: string } }) => (
    <div data-testid="pierre-pair" data-name={oldFile.name} />
  ),
}))

import FileChangeChips, { STAGED_ROW_HEIGHT_PX } from '../components/FileChangeChips'
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
const staged = (c: HTMLElement) => c.querySelectorAll('[aria-busy="true"]').length

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

describe('staged row mounting', () => {
  it('reserves exactly the mounted row height, so releasing a row cannot move the transcript', () => {
    // Two assertions, because one alone is self-referential: the placeholder is
    // tied to the constant (so deleting the reservation reddens), and the
    // constant is tied to the MEASURED value (so drifting it reddens too). A
    // single `toBe(STAGED_ROW_HEIGHT_PX)` would follow the constant anywhere.
    expect(STAGED_ROW_HEIGHT_PX).toBe(36)
    const { container } = render(
      <FileChangeChips fileChanges={Array.from({ length: EAGER_ROWS + 2 }, (_, i) => change(i))} />,
    )
    const placeholder = container.querySelector('[aria-busy="true"] > div') as HTMLElement
    expect(placeholder).toBeTruthy()
    // The inline height IS the contract: `.fcc-row` adds no box of its own, so
    // this is the row's height both before and after release.
    expect(placeholder.style.height).toBe(`${STAGED_ROW_HEIGHT_PX}px`)
  })

  it('mounts the eager budget in the first commit and stages the rest', () => {
    const { container } = render(
      <FileChangeChips fileChanges={Array.from({ length: EAGER_ROWS + 3 }, (_, i) => change(i))} />,
    )
    expect({ rows: rows(container), mounted: mounted(container), staged: staged(container) })
      .toEqual({ rows: EAGER_ROWS + 3, mounted: EAGER_ROWS, staged: 3 })
  })

  it('releases every staged row, so nothing is dropped', () => {
    const { container } = render(
      <FileChangeChips fileChanges={Array.from({ length: EAGER_ROWS + 3 }, (_, i) => change(i))} />,
    )
    flushStaging()
    expect({ mounted: mounted(container), staged: staged(container) })
      .toEqual({ mounted: EAGER_ROWS + 3, staged: 0 })
  })

  it('remounts a previously admitted row immediately instead of re-queueing it', () => {
    // The starvation that read as "text vanished to background" on a phone: a
    // virtualized row REMOUNTS every time it scrolls back into the window, and
    // re-queueing it each time keeps whole screens of chips as stand-ins
    // forever (the queue never drains faster than scroll churn refills it).
    // Admission is a one-way latch per row identity: only the FIRST mount
    // pays the queue.
    const changes = Array.from({ length: EAGER_ROWS + 3 }, (_, i) => change(i))
    const first = render(<FileChangeChips fileChanges={changes} />)
    flushStaging()
    expect(mounted(first.container)).toBe(EAGER_ROWS + 3)
    first.unmount()
    // The eager budget is already spent by the first render (module state, not
    // reset between renders), so an unlatched row here could only queue.
    const second = render(<FileChangeChips fileChanges={changes} />)
    expect({ mounted: mounted(second.container), staged: staged(second.container) })
      .toEqual({ mounted: EAGER_ROWS + 3, staged: 0 })
  })

  it('never stages a degenerate change, which mounts to a zero-height row', () => {
    // Reserving a header's height for a row that ends at 0px would ADD height on
    // release — the same jump, inverted.
    const same = Array.from({ length: EAGER_ROWS + 3 }, (_, i) => ({
      path: `/repo/gone-${i}.ts`, before: '', after: '',
    }))
    const { container } = render(<FileChangeChips fileChanges={same} />)
    expect({ mounted: mounted(container), staged: staged(container) })
      .toEqual({ mounted: EAGER_ROWS + 3, staged: 0 })
  })

  it('mounts a staged row at once when the reader opens it', () => {
    const { container } = render(
      <FileChangeChips fileChanges={Array.from({ length: EAGER_ROWS + 1 }, (_, i) => change(i))} />,
    )
    const last = `/repo/file-${EAGER_ROWS}.ts`
    expect(container.querySelector(`[data-testid="fcc-row-${last}"][aria-busy="true"]`)).toBeTruthy()

    fireEvent.click(container.querySelector(`[data-testid="fcc-toggle-${last}"]`) as HTMLElement)

    expect(container.querySelector(`[data-testid="fcc-row-${last}"][aria-busy="true"]`)).toBeNull()
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
