// Feature: code blocks mount Pierre immediately, and every pre-mount stand-in is
// the SAME HEIGHT as the mounted render.
//
// Two invariants, each learned from a real-phone regression:
//
// 1. Height. Pierre renders a code block at exactly 50 + 20×lines px (measured
//    from 7 real blocks in a browser: 2px border + 32px header + 16px body
//    padding, 20px per line). Both stand-ins — the Suspense fallback and the
//    streaming view — used `leading-relaxed`, which at 13px text is 21.125px per
//    line, so every block SHRANK by 1.125px per line the moment the real render
//    took over: 45px for a 40-line snippet, felt as the transcript moving under
//    the reader. The stand-ins carry `leading-5` (20px) now; the jsdom-checkable
//    proxy for the measurement is the class name.
//
// 2. No staging. Code blocks were briefly routed through the pierreStaging
//    queue (EAGER_ROWS + idle slices). They churn through the virtualizer
//    window during scroll and every remount re-queues them, so under a busy
//    main thread the queue starves and a completed block sits as a bare header
//    bar for seconds — reported from a real phone. A complete block must mount
//    Pierre in its FIRST commit, regardless of how many siblings rendered
//    before it. (The diff rows stay staged: they mount once per switch,
//    collapsed, and do not churn.)

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, renderHook } from '@testing-library/react'

import { CodeBlock } from '../components/CodeBlock'
import { PlainCodeFallback } from '../pierre/PlainCodeFallback'
import { EAGER_ROWS, __resetStagingForTests, __stagedWaitingCount, useStagedMount } from '../components/pierreStaging'

// Pierre's real chunk never resolves under vitest, so the mount is stubbed. The
// stub is what tells "mounted" apart from a stand-in.
vi.mock('../pierre', () => ({
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-mounted">{file.contents}</div>
  ),
}))

const LINES = 6
const code = Array.from({ length: LINES }, (_, i) => `const x${i} = ${i}`).join('\n')

describe('CodeBlock: immediate Pierre mount with height-identical stand-ins', () => {
  beforeEach(() => {
    __resetStagingForTests()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('mounts a complete block in its first commit', () => {
    render(<CodeBlock code={code} lang="ts" complete />)
    expect(screen.getByTestId('pierre-mounted')).toBeTruthy()
  })

  it('a burst past the eager budget shows READABLE text stand-ins, then highlights on drain', () => {
    // Pierre highlighting is queued (mounting all of a turn's blocks in one
    // commit measured 441ms of main thread), but the stand-in is the real
    // code text at Pierre's metrics — the "bare header bar" defect that made
    // the first staging attempt get reverted must stay impossible.
    const n = EAGER_ROWS + 6
    const views = [] as ReturnType<typeof render>[]
    for (let i = 0; i < n; i++) {
      views.push(render(<CodeBlock code={`snippet ${i}`} lang="ts" complete />))
    }
    // Past-budget blocks: no Pierre yet, but the code TEXT is on screen.
    expect(screen.queryAllByTestId('pierre-mounted').length).toBe(EAGER_ROWS)
    expect(screen.getByText(`snippet ${n - 1}`)).toBeTruthy()
    // Drain the queue: every block highlights.
    for (let i = 0; i < 40; i++) act(() => { vi.advanceTimersByTime(1) })
    expect(screen.queryAllByTestId('pierre-mounted')).toHaveLength(n)
  })

  it('a HELD caller stays out of the queue until its gate opens (viewport gating)', () => {
    // Exhaust the eager budget so any registration must queue.
    for (let i = 0; i < EAGER_ROWS; i++) {
      renderHook(() => useStagedMount(false, `warm-${i}`))
    }
    // Held: far from the viewport — no queue slot spent, not ready.
    const { result, rerender } = renderHook(
      ({ hold }: { hold: boolean }) => useStagedMount(false, 'gated-block', hold),
      { initialProps: { hold: true } },
    )
    expect(result.current).toBe(false)
    expect(__stagedWaitingCount()).toBe(0)
    // Gate opens (scrolled near): joins the queue and releases on drain.
    rerender({ hold: false })
    expect(__stagedWaitingCount()).toBe(1)
    for (let i = 0; i < 40; i++) act(() => { vi.advanceTimersByTime(1) })
    expect(result.current).toBe(true)
  })

  it('a previously admitted block REMOUNTS with Pierre immediately (latch, no re-queue)', () => {
    // The starvation that killed the first staging attempt: virtualizer churn
    // remounts a block every time it scrolls back into the window, and
    // re-queueing each remount starves the queue permanently.
    const first = render(<CodeBlock code={code} lang="ts" complete />)
    for (let i = 0; i < 40; i++) act(() => { vi.advanceTimersByTime(1) })
    expect(screen.queryAllByTestId('pierre-mounted')).toHaveLength(1)
    first.unmount()
    // Budget is already spent (module state): an unlatched remount would queue.
    render(<CodeBlock code={code} lang="ts" complete />)
    expect(screen.queryAllByTestId('pierre-mounted')).toHaveLength(1)
  })

  it('gives the STREAMING stand-in Pierre\'s measured 20px line box', () => {
    render(<CodeBlock code={code} lang="ts" complete={false} />)
    const el = document.querySelector('pre > code')
    expect(el?.className).toContain('leading-5')
    // The 1.125px-per-line surplus that moved the transcript on every swap.
    expect(el?.className).not.toContain('leading-relaxed')
  })

  it('gives the Suspense fallback the same 20px line box', () => {
    render(<PlainCodeFallback text={code} />)
    const pre = document.querySelector('pre')
    expect(pre?.className).toContain('leading-5')
    expect(pre?.className).not.toContain('leading-relaxed')
  })
})
