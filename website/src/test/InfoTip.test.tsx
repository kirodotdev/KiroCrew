import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import InfoTip, { HOVER_LEAVE_GRACE_MS, HOVER_OPEN_DELAY_MS } from '../components/InfoTip'

const TIP_W = 300 // matches tipW in InfoTip's pos()

const setInnerWidth = (w: number) =>
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: w })
const setInnerHeight = (h: number) =>
  Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: h })

const stubRect = (el: HTMLElement, rect: { left: number; top: number; right: number; bottom: number }) => {
  el.getBoundingClientRect = () =>
    ({
      ...rect,
      width: rect.right - rect.left,
      height: rect.bottom - rect.top,
      x: rect.left,
      y: rect.top,
      toJSON: () => ({}),
    }) as DOMRect
}

const trigger = () => screen.getByRole('button', { name: 'More information' })
// A mouse ENTERING the glyph only arms the open delay; `hover` is a mouse that
// also stays on it long enough for the tip to open.
const enter = (el: HTMLElement) => fireEvent.pointerEnter(el, { pointerType: 'mouse' })
const openSettle = () => act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS + 1) })
const hover = (el: HTMLElement) => { enter(el); openSettle() }
const unhover = (el: HTMLElement) => fireEvent.pointerLeave(el, { pointerType: 'mouse' })
// The hover close is deferred by a short grace; settle it.
const settle = () => act(() => { vi.advanceTimersByTime(HOVER_LEAVE_GRACE_MS + 1) })

describe('InfoTip', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); setInnerWidth(1024); setInnerHeight(768) })

  it('is named by a short phrase, and describes itself with the tip text', () => {
    // The glyph carries no text, so the NAME is a short generic phrase and the
    // tip prose is the DESCRIPTION: a name is read on every visit and is the
    // handle other controls are queried by, so a paragraph-length one both
    // talks over the user and collides with real actions named inside it.
    // Closed, the text sits on `title` -- the native fallback, and nothing of it
    // is body copy; open, the visible tooltip is referenced instead and `title`
    // is dropped so the browser cannot show a second tooltip over ours.
    render(<InfoTip text="What this binding does" />)
    const btn = trigger()
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(btn).toHaveAttribute('title', 'What this binding does')
    expect(btn).not.toHaveAttribute('aria-describedby')
    expect(screen.queryByText('What this binding does')).not.toBeInTheDocument()

    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    expect(btn).not.toHaveAttribute('title')
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('What this binding does')
    expect(btn.getAttribute('aria-describedby')).toBe(tip.id)
  })

  it('shows on mouse hover and hides when the pointer leaves', () => {
    render(<InfoTip text="Hover help" />)
    hover(trigger())
    expect(screen.getByRole('tooltip')).toHaveTextContent('Hover help')
    unhover(trigger())
    // Still there for the grace -- the pointer may be on its way to the bubble.
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('a mouse that only crosses the glyph never opens the tip', () => {
    // Settings rows place the bubble ABOVE the glyph, over the previous row,
    // and it is opaque: a pointer travelling through the glyph on its way to
    // that row opened a bubble that then took the click. The open waits a
    // short delay, and leaving before it fires withdraws it for good.
    render(<InfoTip text="Pass-through help" />)
    const btn = trigger()
    enter(btn)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS - 1) })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    unhover(btn)
    openSettle()
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(btn).toHaveAttribute('aria-expanded', 'false')
  })

  it('a mouse that rests on the glyph past the open delay opens the tip', () => {
    render(<InfoTip text="Dwell help" />)
    const btn = trigger()
    enter(btn)
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS - 1) })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(2) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Dwell help')
    expect(btn).toHaveAttribute('aria-expanded', 'true')
  })

  it('keeps a focused tip closed after the second tap and its pointer leave', () => {
    render(<InfoTip text="Touch toggle help" />)
    const btn = trigger()
    fireEvent.focus(btn)
    fireEvent.click(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.pointerLeave(btn, { pointerType: 'touch' })
    settle()
    fireEvent.click(btn)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    fireEvent.pointerLeave(btn, { pointerType: 'touch' })
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    fireEvent.blur(btn)
    fireEvent.focus(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('the bubble always takes the pointer, and a click on it neither closes it nor reaches anything beneath', () => {
    // The bubble is a fixed overlay beside the glyph, over the next row. It is
    // never pointer-transparent: a click keeps the tip open for selecting and
    // copying text, and reaches nothing underneath. The settings-row test
    // covers the owner side (the row does not flip).
    render(<InfoTip text="Glance help" />)
    const btn = trigger()
    hover(btn)
    const tip = screen.getByRole('tooltip')
    expect(tip.className).not.toContain('pointer-events-none')
    fireEvent.click(tip)
    expect(screen.getByRole('tooltip')).toBe(tip)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    expect(btn).not.toHaveAttribute('title')
  })

  it('stays open while the pointer moves from the glyph onto the bubble, and closes when it leaves', () => {
    // Hover alone, no pin: crossing the gap from glyph to bubble fires the
    // glyph's leave on the way, so the bubble counts as hovered until the
    // pointer leaves it too.
    render(<InfoTip text="Hover across help" />)
    const btn = trigger()
    hover(btn)
    const tip = screen.getByRole('tooltip')
    unhover(btn)
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    settle()
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.pointerLeave(tip, { pointerType: 'mouse' })
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('a hover-held tip survives a selection drag that runs past the bubble edge', () => {
    // Dragging to select text that ends at or past the bubble's edge fires the
    // bubble's leave with the button still held. Scheduling the close there
    // unmounted the portal 150ms into the drag and destroyed the selection.
    render(<InfoTip text="Select me by dragging" />)
    const btn = trigger()
    hover(btn)
    const tip = screen.getByRole('tooltip')
    unhover(btn)
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    fireEvent.pointerLeave(tip, { pointerType: 'mouse', buttons: 1 })
    settle()
    expect(screen.getByRole('tooltip')).toBe(tip)
    // The same leave with the button released is the reader moving on.
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    fireEvent.pointerLeave(tip, { pointerType: 'mouse', buttons: 0 })
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('a drag that left the bubble closes it once the button is released elsewhere, unless the pointer came back', () => {
    // Nothing pins a drag that started outside the bubble, so without a close
    // keyed to the release the tip would stay open until Escape.
    render(<InfoTip text="Select me by dragging" />)
    const btn = trigger()
    hover(btn)
    const tip = screen.getByRole('tooltip')
    unhover(btn)
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    fireEvent.pointerLeave(tip, { pointerType: 'mouse', buttons: 1 })
    // Back inside before the release: the pending close is withdrawn.
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    fireEvent.pointerUp(window)
    settle()
    expect(screen.getByRole('tooltip')).toBe(tip)
    // Out again with the button held, released outside: closes after the grace.
    fireEvent.pointerLeave(tip, { pointerType: 'mouse', buttons: 1 })
    settle()
    expect(screen.getByRole('tooltip')).toBe(tip)
    fireEvent.pointerUp(window)
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('the bubble holds the hover reveal itself: a pinned tip under the pointer survives being unpinned by focus moving on', () => {
    // Pinned from the keyboard, then read with the mouse: the glyph's hover has
    // long since ended when the pointer reaches the bubble. If the bubble only
    // cancelled timers, Tab moving focus elsewhere would unpin AND unmount a
    // bubble the pointer is sitting on; asserting hover on enter keeps it until
    // the pointer leaves.
    render(
      <>
        <InfoTip text="Bubble hover help" />
        <button type="button">Next control</button>
      </>,
    )
    const btn = trigger()
    fireEvent.focus(btn)
    fireEvent.click(btn) // Enter pins
    const tip = screen.getByRole('tooltip')
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    fireEvent.blur(btn)
    fireEvent.focus(screen.getByRole('button', { name: 'Next control' })) // unpins
    expect(screen.getByRole('tooltip')).toBe(tip)
    fireEvent.pointerLeave(tip, { pointerType: 'mouse' })
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('a pointer down inside a focus-held tip pins it, so the blur it causes does not unmount the bubble', () => {
    // A mousedown on the non-focusable bubble blurs the glyph. With focus the
    // only reveal, that blur closed the tip on the first press, so its text
    // could never be selected from the keyboard-opened state.
    render(<InfoTip text="Focus then select help" />)
    const btn = trigger()
    fireEvent.focus(btn)
    const tip = screen.getByRole('tooltip')
    fireEvent.pointerDown(tip, { pointerType: 'mouse', button: 0 })
    fireEvent.mouseDown(tip)
    fireEvent.blur(btn)
    expect(screen.getByRole('tooltip')).toBe(tip)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    // Now pinned: leaving the bubble does not close it, an outside press does.
    fireEvent.pointerLeave(tip, { pointerType: 'mouse' })
    settle()
    expect(screen.getByRole('tooltip')).toBe(tip)
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('stays open while the pointer moves from the glyph onto a pinned tip', () => {
    // Reading or copying a long tip means pinning it, then crossing the gap
    // between glyph and bubble; the trigger's leave fires on the way, so the
    // pinned bubble counts as hovered until the pointer leaves it too, and
    // unpinning it then lets the leave close it.
    render(<InfoTip text="Long help to copy" />)
    const btn = trigger()
    hover(btn)
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    unhover(btn)
    fireEvent.pointerEnter(tip, { pointerType: 'mouse' })
    settle()
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.pointerLeave(tip, { pointerType: 'mouse' })
    settle()
    expect(screen.getByRole('tooltip')).toBeInTheDocument() // pinned: leaving does not close
    fireEvent.mouseDown(document.body) // unpin
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('does not treat a touch pointer entering as hover', () => {
    // A tap fires a synthetic enter before its click. Treating it as hover
    // would leave the tip open after the click that was meant to toggle it.
    render(<InfoTip text="Touch help" />)
    fireEvent.pointerEnter(trigger(), { pointerType: 'touch' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    fireEvent.click(trigger())
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.click(trigger())
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('shows on keyboard focus, hides on Escape, and shows again on the next focus or hover', () => {
    render(<InfoTip text="Focus help" />)
    const btn = trigger()
    fireEvent.focus(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(btn, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    // No latch: Escape closed this showing only, and a real focus reopens.
    fireEvent.blur(btn)
    fireEvent.focus(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.blur(btn)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    // Escape while both focused and hovered closes it; it stays closed through
    // the hover grace, and the next hover shows it again without a blur first.
    fireEvent.focus(btn)
    hover(btn)
    fireEvent.keyDown(btn, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    unhover(btn)
    settle()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    hover(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(btn, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    // Still focused in the browser's eyes: the next real focus event reopens.
    fireEvent.focus(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('a click pins the tip so leaving no longer hides it', () => {
    render(<InfoTip text="Pinned help" />)
    const btn = trigger()
    hover(btn)
    fireEvent.click(btn)
    unhover(btn)
    settle()
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('a second click closes a pinned tip even while the trigger stays focused', () => {
    // A tap focuses the trigger as it clicks. Focus alone would keep the tip
    // open after the unpin, so a touch reader could never close it.
    render(<InfoTip text="Toggle help" />)
    const btn = trigger()
    fireEvent.focus(btn)
    fireEvent.click(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.click(btn)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    // Leaving and returning reveals it again: the unpin cleared the focus
    // reveal, and the next real focus sets it back.
    fireEvent.blur(btn)
    fireEvent.focus(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('unpins on an outside click', () => {
    render(<InfoTip text="Tip content" />)
    fireEvent.click(trigger())
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('unpins when focus moves to another control', () => {
    // A tip pinned with Enter outlives the glyph's blur on purpose (clicking
    // into the bubble to copy blurs the glyph too), so Tab alone leaves it
    // open; focus landing outside glyph and bubble is what closes it.
    render(
      <>
        <InfoTip text="Keyboard pinned help" />
        <button type="button">Next control</button>
      </>,
    )
    const btn = trigger()
    fireEvent.focus(btn)
    fireEvent.click(btn) // Enter on a focused button fires click
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.blur(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument() // pinned survives blur
    fireEvent.focus(screen.getByRole('button', { name: 'Next control' }))
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('a pinned tip closes on Escape after the glyph has lost focus, and claims the key', () => {
    render(<InfoTip text="Escape anywhere help" />)
    const btn = trigger()
    fireEvent.focus(btn)
    fireEvent.click(btn)
    fireEvent.blur(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    // Dispatched by hand so the event is cancelable: the tip must mark it
    // handled (Modal's window Escape checks `defaultPrevented`), so one
    // Escape closes the tip and not the dialog it sits in.
    const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    act(() => { document.dispatchEvent(esc) })
    expect(esc.defaultPrevented).toBe(true)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    // No latch survives: the next focus shows the tip again.
    fireEvent.focus(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('claims Escape before document capture dismissal and lets it through once closed', () => {
    const documentCapture = vi.fn((event: KeyboardEvent) => event.defaultPrevented)
    const windowCapture = vi.fn((event: KeyboardEvent) => event.defaultPrevented)
    document.addEventListener('keydown', documentCapture, { capture: true })
    try {
      render(<InfoTip text="Dialog escape help" />)
      const btn = trigger()
      fireEvent.focus(btn)
      expect(screen.getByRole('tooltip')).toBeInTheDocument()
      window.addEventListener('keydown', windowCapture, { capture: true })

      const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
      act(() => { btn.dispatchEvent(esc) })
      expect(esc.defaultPrevented).toBe(true)
      expect(windowCapture).toHaveReturnedWith(true)
      // stopPropagation blocks the document listener entirely, not just dismissal.
      expect(documentCapture).not.toHaveBeenCalled()
      expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

      const closedEsc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
      act(() => { btn.dispatchEvent(closedEsc) })
      expect(closedEsc.defaultPrevented).toBe(false)
      expect(documentCapture).toHaveBeenCalledTimes(1)
      expect(documentCapture).toHaveReturnedWith(false)
    } finally {
      document.removeEventListener('keydown', documentCapture, { capture: true })
      window.removeEventListener('keydown', windowCapture, { capture: true })
    }
  })

  it('a hover-opened tip closes on Escape without the glyph ever being focused, and claims the key', () => {
    // Hover never focuses the glyph, so the trigger's own onKeyDown is out of
    // reach; the bubble is an opaque overlay over the rows beneath and must
    // still be dismissible from the keyboard (WCAG 2.1 SC 1.4.13).
    render(<InfoTip text="Hover escape help" />)
    const btn = trigger()
    hover(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    expect(document.activeElement).not.toBe(btn)
    const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    act(() => { document.dispatchEvent(esc) })
    expect(esc.defaultPrevented).toBe(true)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    // No latch: the next real hover shows it again.
    hover(btn)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  it('does not submit an enclosing form', () => {
    render(<InfoTip text="In a form" />)
    expect(trigger()).toHaveAttribute('type', 'button')
  })

  it('while open, the arrow and Page keys on the focused glyph scroll a long tip and claim the key', () => {
    // The bubble is capped to the room on its side and scrolls, but it is not a
    // tab stop: moving focus toward it blurs the glyph and ends the focus
    // reveal. A reader on the keyboard scrolls it FROM the glyph instead, and
    // the page beneath must not scroll in its place.
    render(<InfoTip text="A tip far longer than the room it was given" />)
    const btn = trigger()
    act(() => { btn.focus() }) // real focus, so the glyph is the active element throughout
    const tip = screen.getByRole('tooltip')
    Object.defineProperty(tip, 'scrollHeight', { configurable: true, value: 600 })
    Object.defineProperty(tip, 'clientHeight', { configurable: true, value: 186 })
    expect(tip.scrollTop).toBe(0)

    const down = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(down) })
    expect(down.defaultPrevented).toBe(true)
    expect(tip.scrollTop).toBe(24)

    const up = new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(up) })
    expect(up.defaultPrevented).toBe(true)
    expect(tip.scrollTop).toBe(0)

    const pageDown = new KeyboardEvent('keydown', { key: 'PageDown', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(pageDown) })
    expect(pageDown.defaultPrevented).toBe(true)
    expect(tip.scrollTop).toBe(186)

    // End clamps to the bottom; Home returns to the top; neither runs past.
    const end = new KeyboardEvent('keydown', { key: 'End', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(end) })
    expect(tip.scrollTop).toBe(600 - 186)
    const home = new KeyboardEvent('keydown', { key: 'Home', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(home) })
    expect(tip.scrollTop).toBe(0)
    const pageUp = new KeyboardEvent('keydown', { key: 'PageUp', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(pageUp) })
    expect(pageUp.defaultPrevented).toBe(true)
    expect(tip.scrollTop).toBe(0)

    // Scrolling never closes the tip, and it is still the glyph's description.
    expect(screen.getByRole('tooltip')).toBe(tip)
    expect(btn.getAttribute('aria-describedby')).toBe(tip.id)
    expect(document.activeElement).toBe(btn)
  })

  it('with the tip closed, the scroll keys on the glyph fall through to the page', () => {
    render(<InfoTip text="Closed tip" />)
    const btn = trigger()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    const down = new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(down) })
    expect(down.defaultPrevented).toBe(false)
    const pageDown = new KeyboardEvent('keydown', { key: 'PageDown', bubbles: true, cancelable: true })
    act(() => { btn.dispatchEvent(pageDown) })
    expect(pageDown.defaultPrevented).toBe(false)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('keeps auto placement on-screen on a narrow viewport', () => {
    // Phone-width regression: right-side placement overflows, and the left-flip
    // (r.left - tipW - 6) goes far negative for a button near the left edge.
    // Unclamped, the tip renders mostly past the left viewport edge.
    setInnerWidth(390)
    render(<InfoTip text="Narrow viewport tip" />)
    const btn = trigger()
    stubRect(btn, { left: 100, top: 200, right: 116, bottom: 216 })
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    const left = parseFloat(tip.style.left)
    expect(left).toBeGreaterThanOrEqual(8)
    expect(left + TIP_W).toBeLessThanOrEqual(390) // fully on-screen
  })

  it('still flips left of the button when the flipped position fits', () => {
    // The clamp must not defeat the flip: a button near the RIGHT edge flips
    // left and the flipped value already fits, so it is used as-is.
    setInnerWidth(390)
    render(<InfoTip text="Right edge tip" />)
    const btn = trigger()
    stubRect(btn, { left: 350, top: 200, right: 366, bottom: 216 })
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    // flipped: 350 - 300 - 6 = 44; inside [8, 390-300-8=82], so unchanged.
    expect(parseFloat(tip.style.left)).toBe(44)
  })

  it('keeps a placement="top" tip above a glyph in the upper third when the room above is readable', () => {
    // The callers that ask for 'top' did so to move the bubble OFF the content
    // below the glyph. With ample room below, the old side test compared the
    // room above against the full cap and fell below anyway; the caller's side
    // is kept whenever it fits a readable bubble.
    setInnerHeight(900)
    render(<InfoTip text="Top-placed tip" placement="top" />)
    const btn = trigger()
    stubRect(btn, { left: 100, top: 300, right: 116, bottom: 316 })
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    expect(tip.style.top).toBe('')
    expect(parseFloat(tip.style.bottom)).toBe(900 - 300 + 6)
    expect(parseFloat(tip.style.maxHeight)).toBe(300 - 6 - 8)
  })

  it('falls below for a placement="top" glyph with no readable room above', () => {
    setInnerHeight(900)
    render(<InfoTip text="Top-placed tip, no room" placement="top" />)
    const btn = trigger()
    stubRect(btn, { left: 100, top: 40, right: 116, bottom: 56 }) // above = 26px
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    expect(tip.style.bottom).toBe('')
    expect(parseFloat(tip.style.top)).toBe(56 + 6)
  })

  it('anchors a tip for a glyph low in the viewport to its bottom edge and caps its height', () => {
    // The bubble's height is unknown when it is placed and can be a whole
    // paragraph, so the side is chosen by room: a glyph in the lower half gets
    // a bubble that grows upward from its bottom edge, with maxHeight capped to
    // the space above, and anything longer scrolls inside the bubble. A fixed
    // element that ran past the bottom edge could never be scrolled to.
    setInnerHeight(600)
    render(<InfoTip text="A long tip" />)
    const btn = trigger()
    stubRect(btn, { left: 100, top: 480, right: 116, bottom: 496 })
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    expect(tip.style.top).toBe('')
    expect(parseFloat(tip.style.bottom)).toBe(600 - 496)
    expect(parseFloat(tip.style.maxHeight)).toBeLessThanOrEqual(496 - 8)
    expect(tip.className).toContain('overflow-y-auto')
    expect(tip.className).toMatch(/max-h-\[/)
  })

  it('keeps a glyph high in the viewport top-aligned with the full cap', () => {
    setInnerHeight(600)
    render(<InfoTip text="Short tip" />)
    const btn = trigger()
    stubRect(btn, { left: 100, top: 100, right: 116, bottom: 116 })
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    expect(parseFloat(tip.style.top)).toBe(100)
    expect(parseFloat(tip.style.maxHeight)).toBe(Math.min(480, Math.round(600 * 0.6)))
  })
})
