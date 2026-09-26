import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { InstantTip, useInstantTip, OPEN_DELAY_MS, scrollMovesAnchor } from '../components/InstantTip'

/** Minimal consumer: one anchor button + the shared bubble. */
function Harness() {
  const { tip, tipHandlers, tipId } = useInstantTip()
  return (
    <>
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </>
  )
}

/** Same consumer inside a [data-tip-boundary] wrapper, anchor NOT in the first
 *  row: the bubble must lift to the boundary's top, not the anchor's. */
function BoundaryHarness() {
  const { tip, tipHandlers, tipId } = useInstantTip()
  return (
    <div data-tip-boundary data-testid="boundary">
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </div>
  )
}

/** A consumer whose anchor sits at the top of the viewport (the top bar), so
 *  the bubble opens under it instead. */
function BelowHarness() {
  const { tip, tipHandlers, tipId } = useInstantTip({ placement: 'below' })
  return (
    <>
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </>
  )
}

/** One anchor whose placement can be switched, so both placements are read
 *  from the SAME rects: `above` from the first line fragment, `below` from the
 *  bounding box. */
function PlacementHarness({ placement }: { placement: 'above' | 'below' }) {
  const { tip, tipHandlers, tipId } = useInstantTip({ placement })
  return (
    <>
      <button type="button" {...tipHandlers}>anchor</button>
      <InstantTip tip={tip} tipId={tipId}>bubble content</InstantTip>
    </>
  )
}

/** A consumer that can HOLD the bubble — the shape a copy chip has while its
 *  "Copied!" flash runs. `hold` is a counter: 0 is no hold, and every press's
 *  outcome is a new value, the way a copy chip hands over its attempt number.
 *  "outcome" bumps it (a new outcome), "idle" clears it (the flash ended), and
 *  "act" arms the anchor the way a press does. */
function HoldHarness() {
  const [hold, setHold] = useState(0)
  const { tip, tipHandlers, tipId, arm } = useInstantTip({ hold })
  return (
    <>
      <button type="button" {...tipHandlers} onMouseDown={e => arm(e.currentTarget)}>anchor</button>
      <button type="button" onClick={() => setHold(h => (h ? 0 : 1))}>toggle hold</button>
      <button type="button" onClick={() => setHold(h => h + 1)}>outcome</button>
      <button type="button" onClick={() => setHold(0)}>idle</button>
      <InstantTip tip={tip} tipId={tipId}>{hold ? 'held content' : 'bubble content'}</InstantTip>
    </>
  )
}

/** Two anchors, each with its own hook — two chips on one line. Either can
 *  hold (a press's outcome), and A's hold can end (its flash ran out). */
function TwoHarness() {
  const [holdA, setHoldA] = useState(0)
  const [holdB, setHoldB] = useState(0)
  const a = useInstantTip({ hold: holdA })
  const b = useInstantTip({ hold: holdB })
  return (
    <>
      <button type="button" {...a.tipHandlers}>anchor A</button>
      <button type="button" {...b.tipHandlers} onMouseDown={e => b.arm(e.currentTarget)}>anchor B</button>
      <button type="button" onClick={() => setHoldA(1)}>hold A</button>
      <button type="button" onClick={() => setHoldA(0)}>release A</button>
      <button type="button" onClick={() => setHoldB(h => h + 1)}>hold B</button>
      <InstantTip tip={a.tip} tipId={a.tipId}>bubble A</InstantTip>
      <InstantTip tip={b.tip} tipId={b.tipId}>bubble B</InstantTip>
    </>
  )
}

// The gesture semantics live in the shared module, so they are pinned here
// once rather than per consumer. FollowUpBar / ChatInput tests assert only
// their own tooltip CONTENT, via keyboard focus (the synchronous path).
describe('InstantTip', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows synchronously on keyboard focus — a tab stop is deliberate', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('shows after the hover-intent delay on pointer enter, not immediately', () => {
    render(<Harness />)
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('paints nothing for a pointer passing through inside the intent window', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.mouseEnter(anchor)
    fireEvent.mouseLeave(anchor)
    act(() => { vi.advanceTimersByTime(200) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('hides on mouse leave', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.mouseLeave(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a HELD bubble survives the pointer leaving, and closes when the hold ends', () => {
    render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    const toggle = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(toggle)
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
    fireEvent.mouseLeave(anchor)
    // Still there: the content is an outcome the user must be able to read.
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
    fireEvent.click(toggle)
    // The hold ended with the pointer gone, so the bubble closes on its own.
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a hold that ends under a resting pointer leaves the bubble open', () => {
    render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    const toggle = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(toggle)
    fireEvent.click(toggle)
    expect(screen.getByRole('tooltip')).toHaveTextContent('bubble content')
  })

  it('a hold that starts while the bubble is closed opens it at the last anchor', () => {
    // A click inside the intent window, or after the pointer already left: the
    // outcome still owes its bubble, at the element the user acted on.
    render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    const toggle = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.mouseEnter(anchor)
    fireEvent.mouseLeave(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
    expect(anchor).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id)
    fireEvent.click(toggle)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a re-entered pointer cancels the deferred close, so the bubble stays after the hold', () => {
    render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    const toggle = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(toggle)
    fireEvent.mouseLeave(anchor)
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(toggle)
    expect(screen.getByRole('tooltip')).toHaveTextContent('bubble content')
  })

  it('blur closes even a held bubble — a tab stop moved on deliberately', () => {
    render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    fireEvent.click(screen.getByRole('button', { name: 'toggle hold' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
    fireEvent.blur(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('keyboard focus keeps the bubble open past the hold; the focus a mouse click leaves does not', () => {
    // A tab stop: focus arrived without the pointer, so the user is still here.
    const { unmount } = render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    const toggle = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.focus(anchor)
    fireEvent.click(toggle)
    fireEvent.click(toggle)
    expect(screen.getByRole('tooltip')).toHaveTextContent('bubble content')
    unmount()

    // A mouse click: the browser focuses the anchor too, then the pointer moves
    // on. That focus must not pin a hint over a chip the user is done with.
    render(<HoldHarness />)
    const anchor2 = screen.getByRole('button', { name: 'anchor' })
    const toggle2 = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.mouseEnter(anchor2)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.focus(anchor2)
    fireEvent.click(toggle2)
    fireEvent.mouseLeave(anchor2)
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
    fireEvent.click(toggle2)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('Escape closes a held bubble too', () => {
    render(<HoldHarness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    fireEvent.click(screen.getByRole('button', { name: 'toggle hold' }))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a hold that starts after Escape or blur dismissed the bubble does not reopen it', () => {
    // Those two are the user moving on deliberately; an outcome that settles
    // afterwards must not bring the bubble back. (A mouse leave is different:
    // the outcome still owes its bubble there, pinned above.)
    const { unmount } = render(<HoldHarness />)
    let anchor = screen.getByRole('button', { name: 'anchor' })
    let toggle = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.focus(anchor)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.queryByRole('tooltip')).toBeNull()
    // Blur dismisses the same way.
    fireEvent.click(toggle)
    fireEvent.focus(anchor)
    fireEvent.blur(anchor)
    fireEvent.click(toggle)
    expect(screen.queryByRole('tooltip')).toBeNull()
    unmount()

    // A later activation (a hover) re-arms the anchor.
    render(<HoldHarness />)
    anchor = screen.getByRole('button', { name: 'anchor' })
    toggle = screen.getByRole('button', { name: 'toggle hold' })
    fireEvent.focus(anchor)
    fireEvent.blur(anchor)
    fireEvent.mouseEnter(anchor)
    fireEvent.mouseLeave(anchor)
    fireEvent.click(toggle)
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
  })

  it('a press after Escape re-arms the anchor, so ITS outcome opens the bubble', () => {
    // Escape dismissed the hint; the user then pressed again with the pointer
    // still resting on the chip. No enter or focus fires for that press, so the
    // press itself (`arm`) is what tells the hook where the outcome belongs.
    render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.mouseDown(anchor)
    fireEvent.click(screen.getByRole('button', { name: 'outcome' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
  })

  it('every new outcome is its own edge: a retry of the same kind reopens a closed bubble', () => {
    // The transcript auto-scrolls during streaming and `scrollMovesAnchor`
    // hides the bubble; the user, pointer still on the chip, presses again and
    // the copy fails AGAIN. Same kind of outcome — it must still show.
    render(<HoldHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    const outcome = screen.getByRole('button', { name: 'outcome' })
    fireEvent.mouseEnter(anchor)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(outcome)
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
    fireEvent.scroll(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.click(outcome)
    expect(screen.getByRole('tooltip')).toHaveTextContent('held content')
  })

  it('one bubble at a time, held ones first: a neighbour\'s hint yields to a held outcome and returns when the hold ends', () => {
    // Each chip owns its own hook; without the registry, "Copied!" on chip A
    // and the hint on chip B paint two portals at once, overlapping on one
    // line. And the held one must WIN: it is the only sign the write landed
    // (or was refused), the hint is a hover cue the user gets again by
    // re-entering — an eviction lost a 3s "Copy failed" ~300ms after the move.
    render(<TwoHarness />)
    const a = screen.getByRole('button', { name: 'anchor A' })
    const b = screen.getByRole('button', { name: 'anchor B' })
    fireEvent.mouseEnter(a)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(screen.getByRole('button', { name: 'hold A' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('bubble A')
    fireEvent.mouseLeave(a)
    fireEvent.mouseEnter(b)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    // Still exactly one bubble, and it is the held one.
    let tips = screen.getAllByRole('tooltip')
    expect(tips).toHaveLength(1)
    expect(tips[0]).toHaveTextContent('bubble A')
    // The hold ends with the pointer resting on B: A closes (the pointer is not
    // on it), and B's hint, which yielded, gets its turn — still one bubble.
    fireEvent.click(screen.getByRole('button', { name: 'release A' }))
    tips = screen.getAllByRole('tooltip')
    expect(tips).toHaveLength(1)
    expect(tips[0]).toHaveTextContent('bubble B')
  })

  it('a hint that yielded does not come back once the pointer has moved on', () => {
    render(<TwoHarness />)
    const a = screen.getByRole('button', { name: 'anchor A' })
    const b = screen.getByRole('button', { name: 'anchor B' })
    fireEvent.mouseEnter(a)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(screen.getByRole('button', { name: 'hold A' }))
    fireEvent.mouseLeave(a)
    fireEvent.mouseEnter(b)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.mouseLeave(b)
    fireEvent.click(screen.getByRole('button', { name: 'release A' }))
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a new outcome supersedes a held neighbour: the latest press owns the one bubble', () => {
    // Two outcomes cannot both be read; the press the user just made is the
    // one whose result matters.
    render(<TwoHarness />)
    const a = screen.getByRole('button', { name: 'anchor A' })
    const b = screen.getByRole('button', { name: 'anchor B' })
    fireEvent.mouseEnter(a)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    fireEvent.click(screen.getByRole('button', { name: 'hold A' }))
    fireEvent.mouseLeave(a)
    fireEvent.mouseEnter(b)
    fireEvent.mouseDown(b)
    fireEvent.click(screen.getByRole('button', { name: 'hold B' }))
    const tips = screen.getAllByRole('tooltip')
    expect(tips).toHaveLength(1)
    expect(tips[0]).toHaveTextContent('bubble B')
  })

  it('Escape dismisses while open, without requiring blur', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a page scroll dismisses — the captured rect is stale once the window moves', () => {
    render(<Harness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.scroll(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a scroll of a container the anchor sits in dismisses', () => {
    render(<BoundaryHarness />)
    fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    // Scroll events do not bubble; the window listener is capture-phase, and
    // fireEvent dispatches on the target itself just as a real strip would.
    fireEvent.scroll(screen.getByTestId('boundary'))
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('a scroll elsewhere in the document leaves the bubble open — the anchor did not move', () => {
    // The transcript re-pinning, a sidebar lane re-sorting, a side panel
    // following its tail: all fire `scroll` on elements the anchor is not in.
    // Without the ancestor check every one of them closes the bubble under a
    // resting pointer.
    const elsewhere = document.createElement('div')
    document.body.appendChild(elsewhere)
    try {
      render(<Harness />)
      fireEvent.focus(screen.getByRole('button', { name: 'anchor' }))
      expect(screen.getByRole('tooltip')).toBeInTheDocument()
      fireEvent.scroll(elsewhere)
      expect(screen.getByRole('tooltip')).toBeInTheDocument()
    } finally {
      elsewhere.remove()
    }
  })

  it('scrollMovesAnchor: window, document and a detached anchor count; an unrelated node does not; no anchor fails closed', () => {
    const anchor = document.createElement('button')
    const parent = document.createElement('div')
    const sibling = document.createElement('div')
    parent.appendChild(anchor)
    document.body.append(parent, sibling)
    try {
      expect(scrollMovesAnchor(window, anchor)).toBe(true)
      expect(scrollMovesAnchor(document, anchor)).toBe(true)
      expect(scrollMovesAnchor(parent, anchor)).toBe(true)
      expect(scrollMovesAnchor(sibling, anchor)).toBe(false)
      expect(scrollMovesAnchor(anchor, anchor)).toBe(false)
      expect(scrollMovesAnchor(sibling, null)).toBe(true)
      // The anchor's element was replaced while the bubble stayed open (a chip
      // changing shape on a pick): nothing contains a detached node, so the
      // ancestor test alone would keep a stranded bubble open on every scroll.
      const detached = document.createElement('button')
      expect(scrollMovesAnchor(parent, detached)).toBe(true)
    } finally {
      parent.remove(); sibling.remove()
    }
  })

  it('blur hides the focus-shown bubble', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.blur(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('links the anchor to the bubble via aria-describedby', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    fireEvent.focus(anchor)
    const described = anchor.getAttribute('aria-describedby')
    expect(described).toBeTruthy()
    expect(screen.getByRole('tooltip').id).toBe(described)
  })

  it('clamps the bubble inside the right viewport edge', () => {
    // jsdom has no layout: give every element a measured width for this test.
    const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, value: 300 })
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true })
    try {
      render(<Harness />)
      const anchor = screen.getByRole('button', { name: 'anchor' })
      // Anchor near the right edge: 1000 + 300 would overflow 1024.
      anchor.getBoundingClientRect = () => ({ top: 200, left: 1000, right: 1010, bottom: 210, width: 10, height: 10, x: 1000, y: 200, toJSON: () => ({}) }) as DOMRect
      fireEvent.focus(anchor)
      const left = parseFloat(screen.getByRole('tooltip').style.left)
      expect(left + 300).toBeLessThanOrEqual(1024 - 8)
      expect(left).toBeGreaterThanOrEqual(8)
    } finally {
      if (saved) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', saved)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
    }
  })

  it('clamps a scrolled-off-screen anchor back to the left viewport edge', () => {
    // A horizontally scrolled strip can hand us a partially visible anchor
    // whose left is already negative; the bubble must come back on-screen.
    const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, value: 300 })
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true })
    try {
      render(<Harness />)
      const anchor = screen.getByRole('button', { name: 'anchor' })
      anchor.getBoundingClientRect = () => ({ top: 200, left: -40, right: 20, bottom: 210, width: 60, height: 10, x: -40, y: 200, toJSON: () => ({}) }) as DOMRect
      fireEvent.focus(anchor)
      const left = parseFloat(screen.getByRole('tooltip').style.left)
      expect(left).toBeGreaterThanOrEqual(8)
    } finally {
      if (saved) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', saved)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
    }
  })

  it('lifts above a [data-tip-boundary] ancestor so wrapped rows are never covered', () => {
    render(<BoundaryHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    // Anchor sits in a second wrapped row (top 300); the strip starts at 240.
    anchor.getBoundingClientRect = () => ({ top: 300, left: 60, right: 160, bottom: 328, width: 100, height: 28, x: 60, y: 300, toJSON: () => ({}) }) as DOMRect
    screen.getByTestId('boundary').getBoundingClientRect = () => ({ top: 240, left: 8, right: 900, bottom: 340, width: 892, height: 100, x: 8, y: 240, toJSON: () => ({}) }) as DOMRect
    fireEvent.focus(anchor)
    // Boundary top (240) - 8, not anchor top (300) - 8.
    expect(parseFloat(screen.getByRole('tooltip').style.top)).toBe(232)
  })

  it('keeps the anchor position when no boundary ancestor exists', () => {
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    anchor.getBoundingClientRect = () => ({ top: 300, left: 60, right: 160, bottom: 328, width: 100, height: 28, x: 60, y: 300, toJSON: () => ({}) }) as DOMRect
    fireEvent.focus(anchor)
    expect(parseFloat(screen.getByRole('tooltip').style.top)).toBe(292)
    // The default is the bubble's BOTTOM edge at `top`: pulled up its own height.
    expect(screen.getByRole('tooltip').className).toMatch(/-translate-y-full/)
    expect(screen.getByRole('tooltip')).toHaveAttribute('data-placement', 'above')
  })

  it('opens under the anchor for placement: below (a top-bar anchor has no room above)', () => {
    render(<BelowHarness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    anchor.getBoundingClientRect = () => ({ top: 8, left: 60, right: 160, bottom: 36, width: 100, height: 28, x: 60, y: 8, toJSON: () => ({}) }) as DOMRect
    fireEvent.focus(anchor)
    const tip = screen.getByRole('tooltip')
    // Anchor bottom (36) + 8, and the bubble's TOP edge sits there: no translate.
    expect(parseFloat(tip.style.top)).toBe(44)
    expect(tip.className).not.toMatch(/-translate-y-full/)
    expect(tip).toHaveAttribute('data-placement', 'below')
    expect(tip.id).toBe(anchor.getAttribute('aria-describedby'))
  })

  it('anchors to the FIRST line fragment of an inline anchor that wraps', () => {
    // An inline chip broken across two lines: fragment one ends line 1 at the
    // right (left 700), fragment two starts line 2 at the left margin (left 20).
    // The bounding box's top-left (20, 300) is where NO fragment is; the bubble
    // belongs above where the chip starts, (700, 300).
    render(<Harness />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    const rect = (top: number, left: number, right: number): DOMRect =>
      ({ top, left, right, bottom: top + 20, width: right - left, height: 20, x: left, y: top, toJSON: () => ({}) }) as DOMRect
    anchor.getBoundingClientRect = () => rect(300, 20, 900)
    anchor.getClientRects = () => [rect(300, 700, 900), rect(324, 20, 300)] as unknown as DOMRectList
    fireEvent.focus(anchor)
    const tip = screen.getByRole('tooltip')
    expect(parseFloat(tip.style.top)).toBe(292)
    expect(parseFloat(tip.style.left)).toBe(700)
  })

  it('both placements read the same anchor: above from the first line fragment, below from the bounding box', () => {
    // The two consumers must not regress each other: the chip rows want the
    // bubble above where a wrapped chip STARTS (its first fragment), the
    // top-bar pill (#13516) wants it under the anchor's box. Same rects, both
    // placements, on one tip.
    const rect = (top: number, left: number, right: number): DOMRect =>
      ({ top, left, right, bottom: top + 20, width: right - left, height: 20, x: left, y: top, toJSON: () => ({}) }) as DOMRect
    const { rerender } = render(<PlacementHarness placement="above" />)
    const anchor = screen.getByRole('button', { name: 'anchor' })
    anchor.getBoundingClientRect = () => rect(300, 20, 900)
    anchor.getClientRects = () => [rect(300, 700, 900), rect(324, 20, 300)] as unknown as DOMRectList
    fireEvent.focus(anchor)
    let tip = screen.getByRole('tooltip')
    expect([parseFloat(tip.style.top), parseFloat(tip.style.left)]).toEqual([292, 700])
    expect(tip).toHaveAttribute('data-placement', 'above')
    expect(tip.className).toMatch(/-translate-y-full/)

    fireEvent.blur(anchor)
    expect(screen.queryByRole('tooltip')).toBeNull()
    rerender(<PlacementHarness placement="below" />)
    fireEvent.focus(anchor)
    tip = screen.getByRole('tooltip')
    // Bounding box bottom (320) + 8, at the box's left (20): exactly #13516's
    // shape, untouched by the fragment rule.
    expect([parseFloat(tip.style.top), parseFloat(tip.style.left)]).toEqual([328, 20])
    expect(tip).toHaveAttribute('data-placement', 'below')
    expect(tip.className).not.toMatch(/-translate-y-full/)
  })
})
