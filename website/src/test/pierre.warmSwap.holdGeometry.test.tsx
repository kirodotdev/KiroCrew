/**
 * WarmSwap must keep its fallback on screen until the Pierre impl has rows of
 * its own — a within-budget diff row must never collapse to nothing while the
 * highlight pool is still answering.
 *
 * Harness: the REAL `pierre/index.tsx` runs; only the lazy impl chunk is a stub
 * that shows Pierre's three visible states — nothing applied (the pool has not
 * answered), rows applied, or the app-owned plain text a failed pool hands the
 * surface — and re-renders in place when the test moves it between them, the
 * way Pierre's imperative renderer redraws into its container.
 *
 * jsdom lays nothing out, so the geometry a browser reports is modelled:
 *  - `scrollHeight` never reads below an element's own height, and a box pinned
 *    to all four edges of its positioned parent (`absolute inset-0`) is exactly
 *    as tall as that parent. WarmSwap's wrapper is as tall as the fallback it
 *    shows, so a measurement taken on that box is satisfied by the fallback
 *    itself, before the impl has anything to show.
 *  - The impl's own in-flow height is 0 until rows are applied (with a worker
 *    pool, every render until the pool's generation has initialised: a few
 *    frames on a warm machine, seconds on a slow one), and a plain-text height
 *    once a failed pool hands the surface app-owned text.
 *  - A ResizeObserver fires when the observed element's size changes; the test
 *    fires the live observers after each modelled change.
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FileContents } from '@pierre/diffs'

const state = vi.hoisted(() => ({
  /** What Pierre has applied into its container. */
  phase: 'empty' as 'empty' | 'rows' | 'plain',
  listeners: new Set<() => void>(),
  /** Live ResizeObserver callbacks, fired by the test when geometry changes. */
  resizeCallbacks: [] as Array<() => void>,
}))

const setPhase = (phase: typeof state.phase) => act(() => {
  state.phase = phase
  for (const notify of [...state.listeners]) notify()
})

vi.mock('../pierre/PierreImpl', async () => {
  const { useSyncExternalStore } = await import('react')
  const subscribe = (listener: () => void) => {
    state.listeners.add(listener)
    return () => { state.listeners.delete(listener) }
  }
  const Impl = () => {
    const phase = useSyncExternalStore(subscribe, () => state.phase)
    return (
      <div data-testid="impl">
        {phase === 'rows' && <div data-testid="impl-rows" data-line-type="context">row</div>}
        {phase === 'plain' && <pre data-testid="impl-plain">plain text</pre>}
      </div>
    )
  }
  return { PierreCodeImpl: Impl, PierrePatchImpl: Impl, PierreFilePairImpl: Impl }
})

/** Heights a browser reports for the modelled content, in px. */
const FALLBACK_PX = 132
const ROWS_PX = 184
const TEXT_PX = 120

/** The in-flow height of `el`'s content. A node inside a positioned box is out
 *  of the flow of every ancestor above that box, so the box's own content does
 *  not add to the wrapper's height while the box is positioned. */
function inFlowHeight(el: HTMLElement): number {
  const has = (selector: string) => [...el.querySelectorAll(selector)].some(node => {
    const box = node.closest('.absolute.inset-0')
    return box == null || box === el || box.contains(el)
  })
  let h = 0
  if (has('pre.pierre-plain')) h += FALLBACK_PX
  if (has('[data-testid="impl-rows"]')) h += ROWS_PX
  if (has('[data-testid="impl-plain"]')) h += TEXT_PX
  return h
}

let scrollHeightSpy: ReturnType<typeof vi.spyOn> | undefined

function stubBrowserGeometry() {
  scrollHeightSpy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    const own = inFlowHeight(this)
    const pinned = this.classList.contains('absolute') && this.classList.contains('inset-0')
    // Pinned to its parent's edges: as tall as the parent, and scrollHeight
    // never reads below that.
    return pinned && this.parentElement ? Math.max(own, inFlowHeight(this.parentElement)) : own
  })
}

const fireResize = () => act(() => { for (const cb of [...state.resizeCallbacks]) cb() })

class FakeResizeObserver {
  constructor(private readonly cb: () => void) {}
  observe() { if (!state.resizeCallbacks.includes(this.cb)) state.resizeCallbacks.push(this.cb) }
  unobserve() { this.disconnect() }
  disconnect() {
    const i = state.resizeCallbacks.indexOf(this.cb)
    if (i >= 0) state.resizeCallbacks.splice(i, 1)
  }
}

const oldFile: FileContents = { name: 'toggle.ts', contents: 'export const open = false\n' }
const newFile: FileContents = { name: 'toggle.ts', contents: 'export const open = true\n' }
/** The file-change card's within-budget row: Pierre draws the header. */
const OPTIONS = { collapsed: false, diffStyle: 'split' as const, overflow: 'wrap' as const, disableFileHeader: false }

/** The reader sees the fallback: its text is on screen and not inside a
 *  hidden box. */
const fallbackOnScreen = (container: HTMLElement) =>
  [...container.querySelectorAll('pre.pierre-plain')].filter(el => el.closest('[aria-hidden="true"]') == null)
/** The impl is still staged inside WarmSwap's hidden box. */
const implHidden = () => screen.getByTestId('impl').closest('[aria-hidden="true"]') != null

async function mountWithinBudgetPair(onVisible = vi.fn()) {
  const { PierreFilePair } = await import('../pierre')
  const view = render(<PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} onVisible={onVisible} />)
  // The impl chunk has resolved and Pierre's container is mounted, empty.
  expect(await screen.findByTestId('impl')).toBeInTheDocument()
  return { ...view, onVisible }
}

beforeEach(() => {
  state.phase = 'empty'
  state.listeners.clear()
  state.resizeCallbacks.length = 0
  vi.resetModules()
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  stubBrowserGeometry()
})

afterEach(() => {
  cleanup()
  scrollHeightSpy?.mockRestore()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('WarmSwap holds until the impl has rows of its own', () => {
  it('keeps the fallback while the pool has not answered, then swaps on the first rows', async () => {
    const { container, onVisible } = await mountWithinBudgetPair()

    // Layout settles with Pierre's container still empty: the fallback owns
    // the row and the impl stays staged. A measurement satisfied by the
    // fallback's own height would release here and leave the reader an empty
    // row until the rows land.
    fireResize()
    expect(fallbackOnScreen(container)).toHaveLength(1)
    expect(implHidden()).toBe(true)
    expect(onVisible).not.toHaveBeenCalled()

    // The pool answers and Pierre applies its rows: the hold releases.
    await setPhase('rows')
    fireResize()
    expect(fallbackOnScreen(container)).toHaveLength(0)
    expect(implHidden()).toBe(false)
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull()
    expect(onVisible).toHaveBeenCalledTimes(1)
  })

  it('keeps holding through a slow pool and releases on the rows, not on a timer', async () => {
    // Only the deadline's own timer is faked, and it keeps advancing with real
    // time: the lazy chunk and the polling `findBy` still settle.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true })
    const { container, onVisible } = await mountWithinBudgetPair()

    // Seconds pass with no rows: still the fallback, still one visible copy.
    fireResize()
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    fireResize()
    expect(fallbackOnScreen(container)).toHaveLength(1)
    expect(implHidden()).toBe(true)
    expect(onVisible).not.toHaveBeenCalled()

    await setPhase('rows')
    fireResize()
    expect(fallbackOnScreen(container)).toHaveLength(0)
    expect(implHidden()).toBe(false)
    expect(onVisible).toHaveBeenCalledTimes(1)

    // The deadline was cleared by the release: nothing fires again.
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(onVisible).toHaveBeenCalledTimes(1)
  })

  it('releases on the plain text a failed pool hands the surface', async () => {
    const { container, onVisible } = await mountWithinBudgetPair()
    fireResize()
    expect(implHidden()).toBe(true)

    // The pool failed: the impl shows app-owned text. That text has height
    // of its own, so the hold releases on it -- no hold is left behind.
    await setPhase('plain')
    fireResize()
    expect(implHidden()).toBe(false)
    expect(fallbackOnScreen(container)).toHaveLength(0)
    expect(screen.getByTestId('impl-plain')).toBeVisible()
    expect(onVisible).toHaveBeenCalledTimes(1)
  })

  it('still releases on the deadline when nothing ever paints', async () => {
    // Only the deadline's own timer is faked, and it keeps advancing with real
    // time: the lazy chunk and the polling `findBy` still settle.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true })
    const { container, onVisible } = await mountWithinBudgetPair()
    fireResize()
    expect(implHidden()).toBe(true)

    await act(async () => { await vi.advanceTimersByTimeAsync(2600) })
    expect(implHidden()).toBe(false)
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull()
    expect(onVisible).toHaveBeenCalledTimes(1)
  })
})
