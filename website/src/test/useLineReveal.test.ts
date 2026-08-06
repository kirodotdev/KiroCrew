/**
 * Tests for useLineReveal — the `file.py:447` jump.
 *
 * Driven against a FAKE editor rather than a real Monaco (heavy, lazy-loaded and
 * unrenderable in jsdom), which is the whole reason the logic lives in a hook of
 * its own. What is pinned here is everything a real editor would not tell us:
 * the out-of-range clamp, the one-shot consumption, that a repeat request with
 * the same line still fires, and that nothing is left decorated on unmount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useLineReveal, REVEAL_FLASH_MS, REVEAL_HOLD_MS, REVEAL_FADE_MS, type RevealTarget } from '../hooks/useLineReveal'

/** Minimal stand-in for the surface useLineReveal actually touches. */
function fakeEditor(lineCount = 1000) {
  const decorations = { clear: vi.fn() }
  /** Stands in for the editor container: the hook publishes the fade timings onto
   *  it as custom properties so index.css animates on the same numbers. */
  const container = document.createElement('div')
  const dispose = vi.fn()
  /** Fires the layout listener, standing in for automaticLayout's ResizeObserver
   *  handing the editor a real height a beat after mount. `height` is what the
   *  hook uses to decide the settling correction is done. */
  let onLayout: (() => void) | undefined
  let height = 0
  const ed = {
    getModel: () => ({ getLineCount: () => lineCount }),
    getLayoutInfo: () => ({ height }),
    revealLineInCenter: vi.fn(),
    revealLinesInCenter: vi.fn(),
    setPosition: vi.fn(),
    getContainerDomNode: () => container,
    focus: vi.fn(),
    createDecorationsCollection: vi.fn(() => decorations),
    onDidLayoutChange: vi.fn((cb: () => void) => { onLayout = cb; return { dispose } }),
  }
  const monaco = { Range: class { constructor(public a: number, public b: number, public c: number, public d: number) {} } }
  return {
    ed, monaco, decorations, dispose, container,
    /** Emit a layout change. `h` is the viewport height the editor now reports;
     *  0 models the pre-layout state that causes the top-edge bug. */
    relayout: (h = 600) => { height = h; onLayout?.() },
  }
}

type Mounted = ReturnType<typeof fakeEditor>
/* eslint-disable @typescript-eslint/no-explicit-any -- the fake implements only
   the handful of members the hook calls, not the full Monaco interfaces. */
const mount = (result: { current: { onEditorMount: (e: any, m: any) => void } }, f: Mounted) =>
  act(() => { result.current.onEditorMount(f.ed as any, f.monaco as any) })
/* eslint-enable @typescript-eslint/no-explicit-any */

const target = (line: number, nonce = 1): RevealTarget => ({ line, nonce })
const span = (line: number, endLine: number, nonce = 1): RevealTarget => ({ line, endLine, nonce })

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('useLineReveal', () => {
  it('reveals and decorates the requested line on editor mount', () => {
    // First open: the editor does not exist yet when the effect runs, so mount
    // is what performs the jump.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(447)))
    mount(result, f)
    expect(f.ed.revealLineInCenter).toHaveBeenCalledWith(447)
    expect(f.ed.setPosition).toHaveBeenCalledWith({ lineNumber: 447, column: 1 })
    expect(f.ed.createDecorationsCollection).toHaveBeenCalledTimes(1)
  })

  it('does not steal focus from the chat input', () => {
    // The click came from the transcript, not from an editing gesture.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(10)))
    mount(result, f)
    expect(f.ed.focus).not.toHaveBeenCalled()
  })

  it('clamps a line past the end of the file instead of doing nothing', () => {
    // The file may have changed since the message, or been read truncated.
    const f = fakeEditor(120)
    const { result } = renderHook(() => useLineReveal(target(9999)))
    mount(result, f)
    expect(f.ed.revealLineInCenter).toHaveBeenCalledWith(120)
  })

  it('clamps a line below 1', () => {
    const f = fakeEditor(120)
    const { result } = renderHook(() => useLineReveal(target(-5)))
    mount(result, f)
    expect(f.ed.revealLineInCenter).toHaveBeenCalledWith(1)
  })

  it('clears the flash after the timeout', () => {
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(42)))
    mount(result, f)
    expect(f.decorations.clear).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(REVEAL_FLASH_MS + 1) })
    expect(f.decorations.clear).toHaveBeenCalled()
  })

  it('reveals an already-mounted editor when a new target arrives', () => {
    const f = fakeEditor()
    const { result, rerender } = renderHook(
      ({ t }: { t: RevealTarget | undefined }) => useLineReveal(t),
      { initialProps: { t: undefined as RevealTarget | undefined } },
    )
    mount(result, f)
    expect(f.ed.revealLineInCenter).not.toHaveBeenCalled()
    act(() => rerender({ t: target(88, 2) }))
    expect(f.ed.revealLineInCenter).toHaveBeenCalledWith(88)
  })

  it('re-fires for the SAME line when the nonce changes', () => {
    // Re-clicking a chip after scrolling away must jump again. Without the nonce
    // the prop would be === to the previous value and nothing would run.
    const f = fakeEditor()
    const { result, rerender } = renderHook(
      ({ t }: { t: RevealTarget | undefined }) => useLineReveal(t),
      { initialProps: { t: target(447, 1) as RevealTarget | undefined } },
    )
    mount(result, f)
    expect(f.ed.revealLineInCenter).toHaveBeenCalledTimes(1)
    act(() => rerender({ t: target(447, 2) }))
    expect(f.ed.revealLineInCenter).toHaveBeenCalledTimes(2)
  })

  it('lands exactly one decoration when mount and effect both fire', () => {
    // Idempotence: the previous collection is cleared before a new one is made,
    // so the two entry points cannot leave two highlights lit.
    const f = fakeEditor()
    const { result, rerender } = renderHook(
      ({ t }: { t: RevealTarget | undefined }) => useLineReveal(t),
      { initialProps: { t: target(447, 1) as RevealTarget | undefined } },
    )
    mount(result, f)
    act(() => rerender({ t: target(447, 1) }))
    expect(f.decorations.clear.mock.calls.length + 1)
      .toBeGreaterThanOrEqual(f.ed.createDecorationsCollection.mock.calls.length)
  })

  it('reports the target as consumed so it can be a true one-shot', () => {
    // Without this the target lingers on the tab and re-fires on every remount —
    // switching chats and back would jump the reader again, unasked.
    const onConsumed = vi.fn()
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(447), onConsumed))
    mount(result, f)
    expect(onConsumed).toHaveBeenCalledTimes(1)
  })

  it('does not report consumption when there is no editor to reveal in', () => {
    // Panel opened while collapsed: nothing mounted, so the target must survive
    // for the mount that eventually happens.
    const onConsumed = vi.fn()
    renderHook(() => useLineReveal(target(447), onConsumed))
    expect(onConsumed).not.toHaveBeenCalled()
  })

  it('does nothing at all without a target', () => {
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(undefined))
    mount(result, f)
    expect(f.ed.revealLineInCenter).not.toHaveBeenCalled()
    expect(f.ed.createDecorationsCollection).not.toHaveBeenCalled()
  })

  it('re-centres on layout change, because mount-time height is usually zero', () => {
    // The bug this fixes: at mount the container has no height yet, so "centre"
    // resolves against a zero-height viewport and the line lands flush against
    // the TOP edge — losing the preceding context a citation is read for.
    // automaticLayout's ResizeObserver supplies the real height AFTER a frame, so
    // a requestAnimationFrame re-assert does not close the gap; this does.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(447)))
    mount(result, f)
    expect(f.ed.revealLineInCenter).toHaveBeenCalledTimes(1)
    act(() => { f.relayout() })
    expect(f.ed.revealLineInCenter).toHaveBeenCalledTimes(2)
    expect(f.ed.revealLineInCenter).toHaveBeenLastCalledWith(447)
    // Re-centring must not paint a second decoration.
    expect(f.ed.createDecorationsCollection).toHaveBeenCalledTimes(1)
  })

  it('stops re-centring once the flash has cleared', () => {
    // Otherwise a panel resize an hour later would yank the reader back to a
    // line they clicked once.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(447)))
    mount(result, f)
    act(() => { vi.advanceTimersByTime(REVEAL_FLASH_MS + 1) })
    f.ed.revealLineInCenter.mockClear()
    act(() => { f.relayout() })
    expect(f.ed.revealLineInCenter).not.toHaveBeenCalled()
  })

  it('stops re-centring as soon as the editor has a real viewport', () => {
    // The correction exists only for mount-time zero height. Left armed for the
    // whole flash window, dragging the panel divider would yank a reader who had
    // already scrolled away to read around the line.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(447)))
    mount(result, f)
    act(() => { f.relayout(600) })          // the layout that centres correctly
    f.ed.revealLineInCenter.mockClear()
    act(() => { f.relayout(400) })          // a later user-driven resize
    expect(f.ed.revealLineInCenter).not.toHaveBeenCalled()
  })

  it('keeps correcting while the viewport is still unmeasured', () => {
    // A layout change that still reports no height has not fixed anything, so the
    // correction must stay armed for the one that does.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(447)))
    mount(result, f)
    act(() => { f.relayout(0) })
    act(() => { f.relayout(0) })
    f.ed.revealLineInCenter.mockClear()
    act(() => { f.relayout(600) })
    expect(f.ed.revealLineInCenter).toHaveBeenCalledWith(447)
  })

  it('never re-centres a layout change when no reveal was requested', () => {
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(undefined))
    mount(result, f)
    act(() => { f.relayout() })
    expect(f.ed.revealLineInCenter).not.toHaveBeenCalled()
  })

  it('reveals a RANGE with revealLinesInCenter, not just its first line', () => {
    // A `file.md:10-16` citation should fit the whole passage on screen; centring
    // line 10 alone would leave the rest of what was cited below the fold.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(span(10, 16)))
    mount(result, f)
    expect(f.ed.revealLinesInCenter).toHaveBeenCalledWith(10, 16)
    expect(f.ed.revealLineInCenter).not.toHaveBeenCalled()
    // The caret goes to the START of the span, not the end.
    expect(f.ed.setPosition).toHaveBeenCalledWith({ lineNumber: 10, column: 1 })
  })

  it('decorates every line of the range', () => {
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(span(10, 16)))
    mount(result, f)
    const deco = f.ed.createDecorationsCollection.mock.calls[0][0][0]
    // isWholeLine over start..end is what paints the whole block.
    expect(deco.options.isWholeLine).toBe(true)
    expect([deco.range.a, deco.range.c]).toEqual([10, 16])
  })

  it('clamps a range at both ends and collapses one wholly past EOF', () => {
    const f = fakeEditor(120)
    const { result } = renderHook(() => useLineReveal(span(100, 9999)))
    mount(result, f)
    expect(f.ed.revealLinesInCenter).toHaveBeenCalledWith(100, 120)

    const g = fakeEditor(50)
    const r2 = renderHook(() => useLineReveal(span(400, 500)))
    mount(r2.result, g)
    // Both ends clamp to the last line, so the span degenerates to one line and
    // takes the single-line path rather than asking for a zero-height range.
    expect(g.ed.revealLineInCenter).toHaveBeenCalledWith(50)
    expect(g.ed.revealLinesInCenter).not.toHaveBeenCalled()
  })

  it('re-centres a range on layout settle, not just its first line', () => {
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(span(10, 16)))
    mount(result, f)
    f.ed.revealLinesInCenter.mockClear()
    act(() => { f.relayout(600) })
    expect(f.ed.revealLinesInCenter).toHaveBeenCalledWith(10, 16)
  })

  it('publishes the fade timings to CSS so the stylesheet cannot drift', () => {
    // The highlight fades via a CSS animation on the decoration class, because a
    // `transition` cannot run: clearing the decoration removes Monaco's element
    // outright and a removed element does not animate. These custom properties
    // are what keep the keyframe delay/duration equal to the JS clear schedule.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(42)))
    mount(result, f)
    expect(f.container.style.getPropertyValue('--mc-line-reveal-hold')).toBe(`${REVEAL_HOLD_MS}ms`)
    expect(f.container.style.getPropertyValue('--mc-line-reveal-fade')).toBe(`${REVEAL_FADE_MS}ms`)
  })

  it('clears only AFTER the fade has finished, never mid-animation', () => {
    // Clearing at the hold point would cut the fade off and reproduce the abrupt
    // disappearance this replaced. NOTE this timing alone does not prove a fade —
    // the old abrupt version also stayed decorated until 2800ms — which is why the
    // animation-name assertions below and scripts/probe-reveal-fade.mjs exist.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(42)))
    mount(result, f)
    act(() => { vi.advanceTimersByTime(REVEAL_HOLD_MS + 1) })
    expect(f.decorations.clear).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(REVEAL_FADE_MS) })
    expect(f.decorations.clear).toHaveBeenCalled()
    expect(REVEAL_FLASH_MS).toBe(REVEAL_HOLD_MS + REVEAL_FADE_MS)
  })

  it('requests a fade animation by name, so the class animates itself out', () => {
    // The mechanism, not just the timing: a `transition` could never fire here
    // because clearing the decoration removes Monaco's node, so the class has to
    // carry its own keyframe animation.
    const f = fakeEditor()
    const { result } = renderHook(() => useLineReveal(target(42)))
    mount(result, f)
    expect(f.container.style.getPropertyValue('--mc-line-reveal-anim')).toMatch(/^mc-line-reveal-out/)
    expect(f.container.style.getPropertyValue('--mc-line-reveal-gutter-anim'))
      .toMatch(/^mc-line-reveal-gutter-out/)
  })

  it('alternates the animation name so a repeat reveal restarts the fade', () => {
    // Monaco reuses a rendered line node when the overlay HTML is unchanged, so a
    // clear+add in one tick leaves the same element with its animation already
    // finished and `forwards` pinning it transparent. Re-clicking the same chip
    // inside the flash window would then scroll but paint nothing — the exact case
    // the nonce exists for. A different animation-name is what restarts it.
    const f = fakeEditor()
    const { result, rerender } = renderHook(
      ({ t }: { t: RevealTarget | undefined }) => useLineReveal(t),
      { initialProps: { t: target(42, 1) as RevealTarget | undefined } },
    )
    mount(result, f)
    const first = f.container.style.getPropertyValue('--mc-line-reveal-anim')
    // Re-click mid-window: same line, new nonce.
    act(() => { vi.advanceTimersByTime(REVEAL_HOLD_MS + 100) })
    act(() => rerender({ t: target(42, 2) }))
    const second = f.container.style.getPropertyValue('--mc-line-reveal-anim')
    expect(second).not.toBe(first)
    expect(second).toMatch(/^mc-line-reveal-out/)
  })

  it('clears the decoration and the pending timer on unmount', () => {
    const f = fakeEditor()
    const { result, unmount } = renderHook(() => useLineReveal(target(42)))
    mount(result, f)
    f.decorations.clear.mockClear()
    unmount()
    expect(f.decorations.clear).toHaveBeenCalled()
    // The layout listener must go with it, or it would re-centre a torn-down editor.
    expect(f.dispose).toHaveBeenCalled()
    // The timer must be dead too, or it would fire against a torn-down editor.
    f.decorations.clear.mockClear()
    f.ed.revealLineInCenter.mockClear()
    act(() => { vi.advanceTimersByTime(REVEAL_FLASH_MS + 1) })
    expect(f.decorations.clear).not.toHaveBeenCalled()
    expect(f.ed.revealLineInCenter).not.toHaveBeenCalled()
  })
})
