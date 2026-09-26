/** WarmSwap's held state carries a one-line pending cue in the fallback's header row.
 *
 *  While the plain fallback is held, uncoloured text reads as a deliberate
 *  display mode rather than as a load in progress (#13937). A held fallback
 *  that has a header row ends that row with "Highlighting code…" for exactly as long
 *  as the hold lasts: the cue is gone the moment `painted` flips true — on the
 *  first rows, on the plain text a failed pool hands the surface, or on the
 *  deadline fail-safe — and it never renders on a path that does not hold (farm
 *  measurement, a collapsed pair, plain-diff mode, a whole-file scroller).
 *
 *  The row exists in both states (the fallback's header while held, Pierre's
 *  own header with its metadata in the same place once painted), so the cue
 *  costs the hold no height: the stand-in was measured to Pierre's own metrics
 *  so the reveal is a restyle, not a reflow (`CodeBlock.tsx`, `pierre-plain`).
 *  A hold whose fallback has no header row — a code fence, a patch surface, a
 *  pair whose caller disabled the header — shows NO cue element at all: a row
 *  of the cue's own would grow the box and shrink it back on the paint, and an
 *  overlay was ruled out on the issue, so the ruling's "no change to the hold's
 *  geometry" leaves those surfaces silent by design.
 *
 *  Harness: the REAL `pierre/index.tsx` runs; only the lazy impl chunk is a
 *  stub that the test moves between Pierre's visible states (nothing applied,
 *  rows applied, app-owned plain text), with the browser's geometry modelled
 *  through `scrollHeight` the way `pierre.warmSwap.holdGeometry.test.tsx` does.
 *  The oversized pair's off-thread diff is a stub too: its opted-in hold is the
 *  one caller that hands WarmSwap a `header`, and the test only needs a patch
 *  to arrive.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FileContents } from '@pierre/diffs'
import { PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE } from '../pierre/config'

const state = vi.hoisted(() => ({
  phase: 'empty' as 'empty' | 'rows' | 'plain',
  listeners: new Set<() => void>(),
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

vi.mock('../pierre/diffOffThread', () => ({
  computePairPatch: () => Promise.resolve('diff --git a/big.ts b/big.ts\n--- a/big.ts\n+++ b/big.ts\n@@ -13 +13 @@\n-const v12 = 12\n+const v12 = 12 // patched\n'),
}))

const FALLBACK_PX = 132
const ROWS_PX = 184
const TEXT_PX = 120
/** The plain header row (`min-h-9`): the same height with or without the cue. */
const HEADER_PX = 36
/** What a row of the cue's own would cost the box: charged only to a cue that
 *  sits in the flow OUTSIDE a header row, so a cue inside the row costs nothing
 *  and a stray in-flow line is caught. */
const HINT_PX = 24
const HINT = 'Highlighting code…'

const isHintNode = (node: Element) => node.childElementCount === 0 && node.textContent === HINT

/** In-flow height of `el`'s content. A node inside a positioned box is out of
 *  the flow of every ancestor above that box, so the box's own content does not
 *  add to the wrapper's height while the box is positioned. */
function inFlowHeight(el: HTMLElement): number {
  const inFlow = (node: Element) => {
    const box = node.closest('.absolute')
    return box == null || box === el || box.contains(el)
  }
  const has = (selector: string) => [...el.querySelectorAll(selector)].some(inFlow)
  const strayHint = [...el.querySelectorAll('div, span')].some(node => isHintNode(node) && node.closest('[data-diffs-header]') == null && inFlow(node))
  let h = 0
  if (has('[data-diffs-header]')) h += HEADER_PX
  if (has('pre.pierre-plain')) h += FALLBACK_PX
  // The oversized pair's simplified fallback: one box, header row included.
  if (has('[data-pierre-plain-file-pair]')) h += FALLBACK_PX
  if (has('[data-testid="impl-rows"]')) h += ROWS_PX
  if (has('[data-testid="impl-plain"]')) h += TEXT_PX
  if (strayHint) h += HINT_PX
  return h
}

let scrollHeightSpy: ReturnType<typeof vi.spyOn> | undefined

function stubBrowserGeometry() {
  scrollHeightSpy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    const own = inFlowHeight(this)
    const pinned = this.classList.contains('absolute') && this.classList.contains('inset-0')
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

const PATCH = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'
const oldFile: FileContents = { name: 'toggle.ts', contents: 'export const open = false\n' }
const newFile: FileContents = { name: 'toggle.ts', contents: 'export const open = true\n' }
/** The file-change card's within-budget row: a header row in both states (the
 *  fallback's while held, Pierre's once painted). */
const OPTIONS = { collapsed: false, diffStyle: 'split' as const, overflow: 'wrap' as const, disableFileHeader: false }
/** The same pair with the header disabled (the Changes panel's shape): no row
 *  for the cue to ride in. */
const HEADERLESS = { ...OPTIONS, disableFileHeader: true }
/** A pair over the render budget: its line-by-line opt-in is the one hold that
 *  hands WarmSwap a `header` of its own (and `heldHint={false}`). */
const BIG = Array.from({ length: PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1 }, (_, i) => `const v${i} = ${i}`).join('\n')
const bigOld: FileContents = { name: 'big.ts', contents: BIG }
const bigNew: FileContents = { name: 'big.ts', contents: BIG.replace('const v12 = 12', 'const v12 = 12 // patched') }

/** The cue the reader sees: on screen, outside the hidden staging box. */
const hintOnScreen = () =>
  screen.queryAllByText(HINT).filter(el => el.closest('[aria-hidden="true"]') == null)
const implHidden = () => screen.getByTestId('impl').closest('[aria-hidden="true"]') != null
/** WarmSwap's outer box: the parent of the pinned staging box. */
const heldBox = () => screen.getByTestId('impl').closest('.absolute.inset-0')!.parentElement as HTMLElement
/** Header rows the reader can see — outside the hidden staging box. */
const visibleHeaders = (container: HTMLElement) =>
  [...container.querySelectorAll('[data-diffs-header]')].filter(el => el.closest('[aria-hidden="true"]') == null)

async function mountWithinBudgetPair(options: typeof OPTIONS = OPTIONS) {
  const { PierreFilePair } = await import('../pierre')
  const view = render(<PierreFilePair oldFile={oldFile} newFile={newFile} options={options} />)
  expect(await screen.findByTestId('impl')).toBeInTheDocument()
  return view
}

/** The held box is exactly its fallback's height (header row included where
 *  there is one) and exactly the impl's once painted: no row of the cue's own
 *  comes and goes. */
async function expectNoCueRow(fallbackPx: number) {
  fireResize()
  expect(implHidden()).toBe(true)
  const outer = heldBox()
  // Nothing recorded for this surface yet: the box is its in-flow content's
  // height, and that content is the fallback alone.
  expect(outer.style.height).toBe('')
  expect(outer.scrollHeight).toBe(fallbackPx)

  await setPhase('rows')
  fireResize()
  expect(implHidden()).toBe(false)
  expect(screen.queryByText(HINT)).toBeNull()
  expect(outer.scrollHeight).toBe(ROWS_PX)
}

beforeEach(() => {
  state.phase = 'empty'
  state.listeners.clear()
  state.resizeCallbacks.length = 0
  localStorage.removeItem('mc-diff-plain')
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
  localStorage.removeItem('mc-diff-plain')
})

describe('a held fallback with a header row carries the cue in that row, only while it holds', () => {
  it('shows the cue at the end of the header row while the pool has not answered and drops it on the first rows', async () => {
    const { container } = await mountWithinBudgetPair()
    fireResize()
    expect(implHidden()).toBe(true)
    // One visible cue, inside the fallback's header row, outside the staging box.
    const [hint] = hintOnScreen()
    expect(hint).toBeVisible()
    expect(hintOnScreen()).toHaveLength(1)
    const [headerRow] = visibleHeaders(container)
    expect(headerRow).toBeDefined()
    expect(headerRow.contains(hint)).toBe(true)
    expect(headerRow.lastElementChild).toBe(hint)
    // The cue is not inside the measured wrapper: its own height must never
    // pass for a paint and release the hold early.
    expect(screen.getByTestId('impl').parentElement!.contains(hint)).toBe(false)

    await setPhase('rows')
    fireResize()
    expect(implHidden()).toBe(false)
    expect(hintOnScreen()).toHaveLength(0)
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('drops the cue when the hold releases on the plain text a failed pool hands the surface', async () => {
    await mountWithinBudgetPair()
    fireResize()
    expect(hintOnScreen()).toHaveLength(1)

    await setPhase('plain')
    fireResize()
    expect(implHidden()).toBe(false)
    expect(screen.queryByText(HINT)).toBeNull()
    expect(screen.getByTestId('impl-plain')).toBeVisible()
  })

  it('drops the cue when the deadline fail-safe releases a surface that never paints', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true })
    await mountWithinBudgetPair()
    fireResize()
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    fireResize()
    // Still held a second and a half in: still the cue.
    expect(hintOnScreen()).toHaveLength(1)

    await act(async () => { await vi.advanceTimersByTimeAsync(1200) })
    expect(implHidden()).toBe(false)
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('beside a long filename the cue steps aside by the ROW\'s width, not the window\'s: a narrow card in a wide window', async () => {
    // The row's only flexible child is the filename (`min-w-0 flex-1 truncate`);
    // a fixed-width cue beside it would take ~110px (en) to ~165px (it, ru) of
    // the name's room for the whole hold and hand it back on the paint. The
    // card that is narrow is not always in a narrow window: a 1920px window
    // with the side panel dragged out leaves the chat pane at `CHAT_PANE_MIN_W`
    // (320px) and the card at ~300px, where a viewport query (`max-[420px]:`)
    // never fires. So the hide is a container query against the header row
    // itself: the row is the query container (`@container`) and the cue hides
    // below 420px of ITS width (`@max-[420px]:hidden`). happy-dom lays nothing
    // out, so the pin is on the declarations the browser evaluates; the harness
    // measured the 1920px window with a 300px card (cue `display: none`, the
    // filename keeping the row) and the 390px viewport by eye.
    const { PierreFilePair } = await import('../pierre')
    const innerWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth')
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1920 })
    try {
      const long = { name: 'website/src/components/notifications/veryLongComponentNameThatKeepsGoing.test.tsx', contents: 'export const open = true\n' }
      const { container } = render(
        <div style={{ width: 300 }}>
          <PierreFilePair oldFile={{ ...long, contents: 'export const open = false\n' }} newFile={long} options={OPTIONS} />
        </div>,
      )
      expect(await screen.findByTestId('impl')).toBeInTheDocument()
      fireResize()
      const [hint] = hintOnScreen()
      const [headerRow] = visibleHeaders(container)
      const title = headerRow.querySelector('[data-title]') as HTMLElement
      expect(title.textContent).toBe(long.name)
      expect(title.classList.contains('truncate')).toBe(true)
      expect(headerRow.contains(hint)).toBe(true)
      // The row queries its own inline size; the cue's hide reads that query.
      expect(headerRow.classList.contains('@container')).toBe(true)
      expect(hint.classList.contains('@max-[420px]:hidden')).toBe(true)
      // Never the window's: at 1920px a media query would leave the cue in the
      // 300px row for the whole hold.
      expect(window.innerWidth).toBe(1920)
      expect([...hint.classList].some(cls => /(^|:)max-\[/.test(cls))).toBe(false)
    } finally {
      if (innerWidth) Object.defineProperty(window, 'innerWidth', innerWidth)
      else delete (window as unknown as { innerWidth?: number }).innerWidth
    }
  })
})

describe('a hold whose fallback has no header row renders no cue element at all', () => {
  it('a code fence (`PierreCode`)', async () => {
    const { PierreCode } = await import('../pierre')
    render(<PierreCode file={{ name: 'a.ts', contents: 'const visible = 1\n' }} langHint="ts" />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    fireResize()
    expect(implHidden()).toBe(true)
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('a patch surface (`PierrePatch`)', async () => {
    const { PierrePatch } = await import('../pierre')
    render(<PierrePatch patch={PATCH} />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    fireResize()
    expect(implHidden()).toBe(true)
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('a within-budget pair whose caller disabled the header', async () => {
    const { container } = await mountWithinBudgetPair(HEADERLESS)
    fireResize()
    expect(implHidden()).toBe(true)
    expect(visibleHeaders(container)).toHaveLength(0)
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('a within-budget pair in plain-diff mode, where nothing is being highlighted', async () => {
    localStorage.setItem('mc-diff-plain', '1')
    const { container } = await mountWithinBudgetPair()
    fireResize()
    expect(implHidden()).toBe(true)
    // The header row is there; the cue is not.
    expect(visibleHeaders(container)).toHaveLength(1)
    expect(screen.queryByText(HINT)).toBeNull()
  })
})

describe('the cue never renders on a path that does not hold', () => {
  it('farm measurement renders the fallback alone, its header row without the cue', async () => {
    // Both from the same module graph: `vi.resetModules()` runs before each
    // test, so a statically imported context would be a different instance
    // from the one the freshly imported `pierre` reads.
    const { PierreFarmHoldContext } = await import('../components/pierreStaging')
    const { PierreFilePair } = await import('../pierre')
    const { container } = render(
      <PierreFarmHoldContext.Provider value={true}>
        <PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} />
      </PierreFarmHoldContext.Provider>,
    )
    expect(visibleHeaders(container)).toHaveLength(1)
    expect(screen.queryByText(HINT)).toBeNull()
    expect(screen.queryByTestId('impl')).toBeNull()
  })

  it('a collapsed pair renders outside WarmSwap', async () => {
    const { PierreFilePair } = await import('../pierre')
    render(<PierreFilePair oldFile={oldFile} newFile={newFile} options={{ ...OPTIONS, collapsed: true }} />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('plain-diff mode on a patch surface is the final render, with no hold and no cue', async () => {
    localStorage.setItem('mc-diff-plain', '1')
    const { PierrePatch } = await import('../pierre')
    const view = render(<PierrePatch patch={PATCH} />)
    expect(view.container.querySelector('pre.pierre-plain')).toBeInTheDocument()
    expect(screen.queryByTestId('impl')).toBeNull()
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('a whole-file scroller keeps its direct Suspense fallback, with no cue', async () => {
    const { PierreCode } = await import('../pierre')
    render(<PierreCode file={{ name: 'a.ts', contents: 'const visible = 1\n' }} scrollClassName="h-40 overflow-auto" />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    expect(screen.queryByText(HINT)).toBeNull()
  })
})

describe('the cue costs the held box no height: the reveal stays a restyle, never a reflow', () => {
  it('a code fence (`PierreCode`) is held at the stand-in\'s exact height and revealed at the impl\'s', async () => {
    const { PierreCode } = await import('../pierre')
    render(<PierreCode file={{ name: 'a.ts', contents: 'const visible = 1\n' }} langHint="ts" />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    await expectNoCueRow(FALLBACK_PX)
  })

  it('a patch surface (`PierrePatch`) is held at the fallback\'s exact height and revealed at the impl\'s', async () => {
    const { PierrePatch } = await import('../pierre')
    render(<PierrePatch patch={PATCH} />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    await expectNoCueRow(FALLBACK_PX)
  })

  it('a within-budget pair with a header row is held at header + fallback, cue included, and revealed at the impl\'s', async () => {
    await mountWithinBudgetPair()
    fireResize()
    expect(hintOnScreen()).toHaveLength(1)
    await expectNoCueRow(HEADER_PX + FALLBACK_PX)
  })

  it('a within-budget pair without a header row is held at the fallback\'s exact height', async () => {
    await mountWithinBudgetPair(HEADERLESS)
    await expectNoCueRow(FALLBACK_PX)
  })

  it('the header-bearing hold (the oversized pair opted in) adds no row while held and draws its header only once painted', async () => {
    const { PierreFilePair } = await import('../pierre')
    const { container } = render(<PierreFilePair oldFile={bigOld} newFile={bigNew} options={OPTIONS} />)
    fireEvent.click(screen.getByRole('button', { name: 'Show line-by-line diff' }))
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    fireResize()
    expect(implHidden()).toBe(true)
    // This hold's fallback already carries the "computing" strip: no cue of
    // WarmSwap's own, and the box is the fallback — header row and strip
    // included — and nothing else.
    expect(screen.queryByText(HINT)).toBeNull()
    const outer = heldBox()
    expect(outer.style.height).toBe('')
    expect(outer.scrollHeight).toBe(HEADER_PX + FALLBACK_PX)
    expect(visibleHeaders(container)).toHaveLength(1)

    await setPhase('rows')
    fireResize()
    expect(implHidden()).toBe(false)
    // The ready-state header replaces the fallback's: still one row, above the
    // measured wrapper (inside it, its height would pass for a paint).
    const headers = visibleHeaders(container)
    expect(headers).toHaveLength(1)
    expect(outer.contains(headers[0])).toBe(true)
    expect(screen.getByTestId('impl').parentElement!.contains(headers[0])).toBe(false)
    expect(outer.scrollHeight).toBe(HEADER_PX + ROWS_PX)
  })

  it('on a warm remount the cue rides in the header row inside the frozen box, which keeps the recorded height', async () => {
    const { PierreFilePair } = await import('../pierre')
    // First mount paints: the impl's own height is recorded under the content key.
    const first = render(<PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    fireResize()
    await setPhase('rows')
    fireResize()
    expect(implHidden()).toBe(false)
    first.unmount()

    // Remount, pool not yet answered: the box is frozen at the recorded height
    // with the fallback's header row (cue included) INSIDE it, above the plain
    // text -- so what the frozen box clips is the fallback's bottom, never the
    // cue, and the box itself does not grow by a line of the cue's own.
    state.phase = 'empty'
    const { container } = render(<PierreFilePair oldFile={oldFile} newFile={newFile} options={OPTIONS} />)
    expect(await screen.findByTestId('impl')).toBeInTheDocument()
    fireResize()
    expect(implHidden()).toBe(true)
    const [hint] = hintOnScreen()
    const outer = heldBox()
    expect(outer.contains(hint)).toBe(true)
    expect(visibleHeaders(container)[0].contains(hint)).toBe(true)
    expect(outer.style.height).toBe(`${ROWS_PX}px`)
    expect(outer.style.overflow).toBe('hidden')
    const fallback = outer.querySelector('pre.pierre-plain') as HTMLElement
    expect(fallback).not.toBeNull()
    expect(hint.compareDocumentPosition(fallback) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    await setPhase('rows')
    fireResize()
    expect(outer.style.height).toBe('')
    expect(screen.queryByText(HINT)).toBeNull()
  })
})
