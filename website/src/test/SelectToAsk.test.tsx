import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import type { RootState } from '../store'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import SelectionToolbar, { useSelectionActions, type SelectionAction } from '../components/SelectionToolbar'

// SideChat pulls the api client — stub the side-* calls it may touch. The
// two submit calls are STABLE fns (the Proxy mints a fresh fn per access for
// everything else) so a test can script a rejection and assert the call.
const sideTurn = vi.fn(() => Promise.resolve({}))
const sideOpen = vi.fn(() => Promise.resolve({}))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = prop === 'sideTurn'
        ? sideTurn
        : prop === 'sideOpen'
          ? sideOpen
          : vi.fn().mockResolvedValue({})
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { readSideChatDraft, seedSideChatDraft } from '../chat-core/composer/sideChatDrafts'

// The composer blocks sends while the gateway reads as offline, so scenes run
// against a connected dashboard.
const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }
const chatInitial = chatReducer(undefined, { type: '@@INIT' })

// Harness that mounts the toolbar from an external selection so the actions
// render deterministically without simulating a real DOM range.
function ToolbarHarness({ onAsk, onQuote }: { onAsk?: (t: string, r: DOMRect) => void; onQuote?: (t: string, r: DOMRect) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const actions = useSelectionActions(onQuote, onAsk)
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
    const ask = screen.getByRole('button', { name: 'Ask about this' })
    expect(ask).toBeInTheDocument()
    // The label says what happens to the selection; WHERE the question lands
    // (and that nothing is sent yet) is the hint's job, since a first-time
    // reader has not met the panel. It is the tooltip AND the accessible
    // description, so keyboard/screen-reader users get it too.
    expect(ask).toHaveAttribute('title', expect.stringContaining('Side Chat'))
    expect(ask).toHaveAccessibleDescription(/nothing is sent/)
    // The description node is a sibling, not a child: the button's own text is the label.
    expect(ask).toHaveTextContent(/^Ask about this$/)
  })

  it('omits the Ask action when onAsk is absent', () => {
    renderWithProviders(<ToolbarHarness />)
    expect(screen.queryByRole('button', { name: 'Ask about this' })).not.toBeInTheDocument()
  })

  it('the Quote / Ask pair name different destinations in their descriptions', () => {
    renderWithProviders(<ToolbarHarness onQuote={() => {}} onAsk={() => {}} />)
    // Neither description merely restates its label: Quote says the reply goes
    // to the main chat, Ask says the question lands in the Side Chat.
    expect(screen.getByRole('button', { name: 'Quote' })).toHaveAccessibleDescription(/main chat/)
    expect(screen.getByRole('button', { name: 'Ask about this' })).toHaveAccessibleDescription(/Side Chat/)
  })

  it('clicking Ask invokes the handler with the selected text', () => {
    const onAsk = vi.fn()
    renderWithProviders(<ToolbarHarness onAsk={onAsk} />)
    act(() => { screen.getByRole('button', { name: 'Ask about this' }).click() })
    expect(onAsk).toHaveBeenCalledWith('hi', expect.anything())
  })

  it('SideChat renders a seed written into its slot as a grounding quote, and puts the caret after it', () => {
    const SLOT = 'seed-slot'
    const store = createTestStore({
      dashboard: dashInitial,
      chat: { ...chatInitial, activeSlot: SLOT, slotHistory: [SLOT], activityOpen: true, activityTab: 'side' } as unknown as RootState['chat'],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const ta = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(ta.value).toBe('')
    act(() => { seedSideChatDraft(SLOT, 'line one\nline two') })
    expect(ta.value).toBe('> line one\n> line two\n\n')
  })

  it('a seed for another slot never shows in this panel; a seed written BEFORE mount is waiting when it mounts', () => {
    const SLOT = 'seed-slot'
    const store = createTestStore({
      dashboard: dashInitial,
      chat: { ...chatInitial, activeSlot: SLOT, slotHistory: [SLOT], activityOpen: true, activityTab: 'side' } as unknown as RootState['chat'],
    })
    // Late-panel case: the Ask landed while the panel was still coming up.
    seedSideChatDraft(SLOT, 'asked before mount')
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const ta = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(ta.value).toBe('> asked before mount\n\n')
    // The composer wrapper names its slot — a test / capture-harness hook.
    expect(ta.closest('[data-side-chat-input]')?.getAttribute('data-side-chat-slot')).toBe(SLOT)
    act(() => { seedSideChatDraft('other-slot', 'not mine') })
    expect(ta.value).toBe('> asked before mount\n\n')
    expect(readSideChatDraft('other-slot')).toBe('> not mine\n\n')
  })

  it('a Side Chat draft survives the panel unmounting and is kept per slot', () => {
    // Every host unmounts the panel through a control beside the composer
    // (another tab, the Members drawer's "Details", a member switch); the
    // draft must not die with it.
    const store = createTestStore({
      dashboard: dashInitial,
      // Full reducer state, not a hand-picked subset: the panel is also mounted
      // on slot-b, and the per-slot selectors read maps a subset would lack.
      chat: { ...chatInitial, activeSlot: 'slot-a', slotHistory: ['slot-a'], activityOpen: true, activityTab: 'side' } as unknown as RootState['chat'],
    })
    const first = renderWithProviders(<SideChat slot="slot-a" />, { store })
    const ta = () => screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    fireEvent.change(ta(), { target: { value: 'half a question' } })
    expect(ta().value).toBe('half a question')
    first.unmount()

    // Another slot's panel does not see it …
    const other = renderWithProviders(<SideChat slot="slot-b" />, { store })
    expect(ta().value).toBe('')
    other.unmount()

    // … and the same slot's panel gets it back.
    renderWithProviders(<SideChat slot="slot-a" />, { store })
    expect(ta().value).toBe('half a question')
  })

  it('a failed submit hands the text back to the slot it was SUBMITTED for, not the slot now shown', async () => {
    // The host can re-bind the panel while a request is in flight (split
    // view's Ask, a member switch). A rejection must restore A's question to
    // A's draft, never append it to B's.
    let rejectTurn: (e: Error) => void = () => {}
    sideTurn.mockImplementationOnce(() => new Promise((_, rej) => { rejectTurn = rej }))
    const store = createTestStore({
      dashboard: dashInitial,
      // Full reducer state, not a hand-picked subset: the panel is also mounted
      // on slot-b, and the per-slot selectors read maps a subset would lack.
      chat: { ...chatInitial, activeSlot: 'slot-a', slotHistory: ['slot-a'], activityOpen: true, activityTab: 'side' } as unknown as RootState['chat'],
    })
    const view = renderWithProviders(<SideChat slot="slot-a" />, { store })
    const ta = () => screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    fireEvent.change(ta(), { target: { value: 'question for A' } })
    fireEvent.keyDown(ta(), { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(sideTurn).toHaveBeenCalled())
    expect(ta().value).toBe('')

    // Re-bind the SAME instance to slot B while A's request is in flight.
    view.rerender(<SideChat slot="slot-b" />)
    fireEvent.change(ta(), { target: { value: 'typing in B' } })

    await act(async () => { rejectTurn(new Error('network')); await Promise.resolve() })
    await waitFor(() => expect(readSideChatDraft('slot-a')).toContain('question for A'))
    // B's visible draft is untouched by A's failure.
    expect(ta().value).toBe('typing in B')
    expect(readSideChatDraft('slot-b')).toBe('typing in B')

    // Back to A: the restored question is what the composer SHOWS — the store
    // is the single source of truth, no cached copy from before the submit
    // can shadow it — and typing on appends to it instead of overwriting.
    view.rerender(<SideChat slot="slot-a" />)
    expect(ta().value).toContain('question for A')
    fireEvent.change(ta(), { target: { value: `${ta().value} and more` } })
    expect(readSideChatDraft('slot-a')).toContain('question for A')
    expect(readSideChatDraft('slot-a')).toContain('and more')
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
