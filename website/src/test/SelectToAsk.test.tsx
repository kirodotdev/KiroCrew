import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { useRef } from 'react'
import type { RootState } from '../store'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'
import SelectionToolbar, { useSelectionActions, type SelectionAction } from '../components/SelectionToolbar'

// SideChat pulls the api client — stub the side-* calls it may touch.
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = vi.fn().mockResolvedValue({})
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

// The composer blocks sends while the gateway reads as offline, so scenes run
// against a connected dashboard.
const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }

// Harness that mounts the toolbar from an external selection so the actions
// render deterministically without simulating a real DOM range.
function ToolbarHarness({ onAsk }: { onAsk?: (t: string, r: DOMRect) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const actions = useSelectionActions(undefined, onAsk)
  return (
    <div ref={ref}>
      <SelectionToolbar containerRef={ref} actions={actions} externalSelection={{ text: 'hi', x: 10, y: 10 }} />
    </div>
  )
}

describe('Select-to-Ask', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('useSelectionActions exposes an Ask action only when onAsk is provided', () => {
    renderWithProviders(<ToolbarHarness onAsk={() => {}} />)
    expect(screen.getByRole('button', { name: 'Ask in Side Chat' })).toBeInTheDocument()
  })

  it('omits the Ask action when onAsk is absent', () => {
    renderWithProviders(<ToolbarHarness />)
    expect(screen.queryByRole('button', { name: 'Ask in Side Chat' })).not.toBeInTheDocument()
  })

  it('clicking Ask invokes the handler with the selected text', () => {
    const onAsk = vi.fn()
    renderWithProviders(<ToolbarHarness onAsk={onAsk} />)
    act(() => { screen.getByRole('button', { name: 'Ask in Side Chat' }).click() })
    expect(onAsk).toHaveBeenCalledWith('hi', expect.anything())
  })

  it('SideChat seeds the draft as a grounding quote on the side-seed event', () => {
    const SLOT = 'seed-slot'
    const store = createTestStore({
      dashboard: dashInitial,
      chat: {
        activeSlot: SLOT,
        messages: [],
        slotSide: {},
        slotHistory: [SLOT],
        activityOpen: true,
        activityTab: 'side',
      } as unknown as RootState['chat'],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const ta = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(ta.value).toBe('')
    act(() => {
      window.dispatchEvent(new CustomEvent('side-seed', { detail: { text: 'line one\nline two' } }))
    })
    expect(ta.value).toBe('> line one\n> line two\n\n')
  })
})

/**
 * Multi-click (double/triple-click) selection must surface the toolbar (#7847).
 *
 * Browsers normalize a word/paragraph selection made by multi-click on the
 * container's LAST block to a boundary point just past the container: a
 * triple-click paragraph selection ends "at the start of the next block", and
 * for the last block that position lives in the container's PARENT. That hoists
 * `range.commonAncestorContainer` above the container, and the toolbar's
 * containment early-return dismissed the selection — so multi-click worked on
 * every line except the last, exactly as reported.
 *
 * These tests model that normalized geometry directly (happy-dom performs no
 * native multi-click selection), fire the real mousedown/mouseup sequence with
 * `detail` >= 2, and assert both sides of the invariant: an overhang holding no
 * text is accepted; an overhang holding a sibling's text keeps being rejected.
 */
function MultiClickHarness({ actions }: { actions: SelectionAction[] }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div data-testid="outer">
      <div ref={ref} data-testid="bubble">
        <p>first paragraph</p>
        <p>last line of the message</p>
      </div>
      <div data-testid="sibling">next message text</div>
      <SelectionToolbar containerRef={ref} actions={actions} />
    </div>
  )
}

function selectRange(startNode: Node, startOffset: number, endNode: Node, endOffset: number) {
  const range = document.createRange()
  range.setStart(startNode, startOffset)
  range.setEnd(endNode, endOffset)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
}

/** Real multi-click event order: the selection exists by the time the final
 *  mousedown's 0ms dismiss and the final mouseup's 50ms check both run. */
function multiClick(target: Element, detail: number) {
  for (let d = detail - 1; d <= detail; d++) {
    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, detail: d, clientX: 40, clientY: 30 }))
    target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, detail: d, clientX: 40, clientY: 30 }))
  }
}

describe('SelectionToolbar multi-click selection (#7847)', () => {
  const ACTIONS: SelectionAction[] = [{ id: 'copy', icon: null, label: 'Copy', onClick: () => {} }]

  beforeEach(() => {
    vi.useFakeTimers()
    if (!Range.prototype.getBoundingClientRect) {
      Range.prototype.getBoundingClientRect = () => new DOMRect(10, 10, 100, 20)
    }
  })
  afterEach(() => {
    vi.useRealTimers()
    window.getSelection()?.removeAllRanges()
  })

  it('shows the toolbar for a triple-click selection of the LAST line (end normalized past the container)', () => {
    render(<MultiClickHarness actions={ACTIONS} />)
    const bubble = screen.getByTestId('bubble')
    const outer = screen.getByTestId('outer')
    const lastP = bubble.querySelectorAll('p')[1]
    // Browser-normalized triple-click geometry: starts at the paragraph text,
    // ends just PAST the bubble in its parent — commonAncestorContainer = outer.
    selectRange(lastP.firstChild!, 0, outer, Array.from(outer.childNodes).indexOf(bubble) + 1)

    act(() => { multiClick(lastP, 3) })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('shows the toolbar for a double-click selection of the last word (same boundary normalization)', () => {
    render(<MultiClickHarness actions={ACTIONS} />)
    const bubble = screen.getByTestId('bubble')
    const outer = screen.getByTestId('outer')
    const lastP = bubble.querySelectorAll('p')[1]
    const text = lastP.firstChild as Text
    selectRange(text, text.length - 'message'.length, outer, Array.from(outer.childNodes).indexOf(bubble) + 1)

    act(() => { multiClick(lastP, 2) })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('keeps working for a multi-click on a NON-last line (end at the start of the next block)', () => {
    render(<MultiClickHarness actions={ACTIONS} />)
    const bubble = screen.getByTestId('bubble')
    const [firstP, lastP] = Array.from(bubble.querySelectorAll('p'))
    selectRange(firstP.firstChild!, 0, lastP, 0)

    act(() => { multiClick(firstP, 3) })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('accepts a selection whose START is normalized before the container (whitespace-only leading overhang)', () => {
    // Mirror of the last-line cases on the FIRST block: a multi-click on the
    // first paragraph can be normalized to start just BEFORE the container.
    // The leading overhang holds no text, so the start-side clamp must accept
    // it — this pins the acceptance half of the `before` term (its rejection
    // half is pinned by the preceding-sibling test).
    render(<MultiClickHarness actions={ACTIONS} />)
    const bubble = screen.getByTestId('bubble')
    const outer = screen.getByTestId('outer')
    const firstP = bubble.querySelectorAll('p')[0]
    const text = firstP.firstChild as Text
    selectRange(outer, Array.from(outer.childNodes).indexOf(bubble), text, text.length)

    act(() => { multiClick(firstP, 3) })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  it('still rejects a selection that genuinely extends into a sibling message', () => {
    render(<MultiClickHarness actions={ACTIONS} />)
    const bubble = screen.getByTestId('bubble')
    const sibling = screen.getByTestId('sibling')
    const lastP = bubble.querySelectorAll('p')[1]
    // Overhang past the container holds REAL text — must stay dismissed.
    selectRange(lastP.firstChild!, 0, sibling.firstChild!, 'next'.length)

    act(() => { multiClick(lastP, 3) })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('rejects a selection that STARTS in a preceding sibling and ends inside the bubble', () => {
    // Mirrors the previous case with the roles reversed, so the start-side
    // (`before`) half of the overhang guard is load-bearing under test: drop
    // the `before` term from the predicate and this selection — whose leading
    // overhang holds the preceding sibling's text — would be accepted, handing
    // another message's text to Quote/Ask.
    function LeadingSiblingHarness({ actions }: { actions: SelectionAction[] }) {
      const ref = useRef<HTMLDivElement>(null)
      return (
        <div data-testid="outer">
          <div data-testid="preceding">previous message text</div>
          <div ref={ref} data-testid="bubble">
            <p>only paragraph of the message</p>
          </div>
          <SelectionToolbar containerRef={ref} actions={actions} />
        </div>
      )
    }
    render(<LeadingSiblingHarness actions={ACTIONS} />)
    const bubble = screen.getByTestId('bubble')
    const preceding = screen.getByTestId('preceding')
    const p = bubble.querySelector('p')!
    selectRange(preceding.firstChild!, 0, p.firstChild!, 4)

    act(() => { multiClick(p, 3) })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
  })

  it('never stringifies the selection for a toolbar whose container holds neither endpoint', () => {
    // One toolbar mounts per assistant message, each listening on `document`,
    // so for the N−1 non-owning instances the containment check must stay O(1):
    // a fallthrough into the overhang stringification would serialize text
    // growing with transcript distance on every mouseup (select-all being the
    // worst case). Pin the cheap-reject path by asserting no Range.toString()
    // call while a foreign toolbar processes a selection it does not own.
    function TwoBubbleHarness({ actions }: { actions: SelectionAction[] }) {
      const owning = useRef<HTMLDivElement>(null)
      const foreign = useRef<HTMLDivElement>(null)
      return (
        <div data-testid="outer">
          <div ref={owning} data-testid="owning"><p>selected here</p></div>
          <div ref={foreign} data-testid="foreign"><p>another message</p></div>
          {/* Only the FOREIGN toolbar is mounted: the selection lives wholly in
              the other bubble, so this instance must reject without measuring. */}
          <SelectionToolbar containerRef={foreign} actions={actions} />
        </div>
      )
    }
    render(<TwoBubbleHarness actions={ACTIONS} />)
    const owning = screen.getByTestId('owning')
    const text = owning.querySelector('p')!.firstChild as Text
    selectRange(text, 0, text, text.length)

    const cloneSpy = vi.spyOn(Range.prototype, 'cloneRange')
    act(() => { multiClick(owning.querySelector('p')!, 3) })
    act(() => { vi.advanceTimersByTime(60) })

    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument()
    // The expensive path begins by cloning the range for the overhang
    // stringification (and the clamped measurement); the cheap reject must
    // never get there. (`Selection.toString` delegates to `Range.toString` in
    // happy-dom, so the clone — unique to the expensive branch — is the pin.)
    expect(cloneSpy).not.toHaveBeenCalled()
    cloneSpy.mockRestore()
  })
})
