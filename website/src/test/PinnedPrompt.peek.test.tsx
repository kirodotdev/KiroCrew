import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import PinnedPrompt, { PEEK_OPEN_DELAY_MS } from '../pages/chat/PinnedPrompt'
import { PINNED_PREVIEW_LINES, PINNED_RESTING_LINES } from '../utils/pinnedPrompt'

// The card's text clamp is set from a constant as an inline style, so the line
// count it is showing is readable straight off the paragraph — no layout needed.
// happy-dom keeps vendor-prefixed longhands under their camelCase key.
function clampOf(p: HTMLElement): string {
  return p.style.webkitLineClamp || (p.style as unknown as Record<string, string>)['WebkitLineClamp'] || ''
}

function renderCard(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  const utils = render(
    <PinnedPrompt
      text="a prompt long enough that one line cannot hold it, and neither can three"
      fullText={'a prompt long enough that one line cannot hold it, and neither can three\nsecond paragraph\nthird paragraph'}
      images={[]}
      bodyBeyondPreview
      pushUp={0}
      bannerH={40}
      expanded={false}
      onToggleExpanded={() => {}}
      onJump={() => {}}
      onCollapsedHeight={() => {}}
      {...over}
    />,
  )
  const card = screen.getByTestId('pinned-prompt')
  const box = card.firstElementChild as HTMLElement
  const p = box.querySelector('p') as HTMLElement
  return { ...utils, card, box, p }
}

/** The card listens natively (`pointerenter` / `pointerleave` do not bubble, so
 *  React's synthetic over/out path is not what it uses); dispatch the same. */
function pointer(el: Element, type: 'pointerenter' | 'pointerleave', pointerType: string) {
  const Ctor = (window as unknown as { PointerEvent: typeof PointerEvent }).PointerEvent
  el.dispatchEvent(new Ctor(type, { bubbles: false, pointerType }))
}

/** Enter and then REST for the intent delay — what a deliberate hover does. */
function hoverAndRest(el: Element, pointerType = 'mouse') {
  act(() => { pointer(el, 'pointerenter', pointerType) })
  act(() => { vi.advanceTimersByTime(PEEK_OPEN_DELAY_MS + 1) })
}

/** Move keyboard focus the way a browser does: `focusout` on the old element with
 *  the new one as relatedTarget, then `focusin` on the new one. */
function moveFocus(from: Element | null, to: Element | null) {
  if (from) from.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: to }))
  if (to) to.dispatchEvent(new FocusEvent('focusin', { bubbles: true, relatedTarget: from }))
}

describe('PinnedPrompt peek', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // The morph reads rects; happy-dom returns zeros, so no transition runs and
    // the inline height override is never set. Nothing to stub there — but make
    // sure ResizeObserver exists, as the clamp measurer observes the paragraph.
    if (!('ResizeObserver' in globalThis)) {
      (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      }
    }
  })
  afterEach(() => { vi.useRealTimers() })

  it('rests on one line', () => {
    const { p } = renderCard()
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
    expect(PINNED_RESTING_LINES).toBe(1)
  })

  it('opens to the preview line count once a mouse has rested on it, and closes on leave', () => {
    const { box, p } = renderCard()
    hoverAndRest(box)
    expect(clampOf(p)).toBe(String(PINNED_PREVIEW_LINES))
    act(() => { pointer(box, 'pointerleave', 'mouse') })
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
  })

  it('does not open for a pointer that merely crosses the card', () => {
    // The card sits between the transcript and the title row, so a pointer on
    // its way to the header passes over it. That transit must not fire the
    // morph: enter, leave before the intent delay, and nothing has moved.
    const { box, p } = renderCard()
    act(() => { pointer(box, 'pointerenter', 'mouse') })
    act(() => { vi.advanceTimersByTime(PEEK_OPEN_DELAY_MS - 1) })
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
    act(() => { pointer(box, 'pointerleave', 'mouse') })
    // The cancelled timer must not fire late and open a card nobody is over.
    act(() => { vi.advanceTimersByTime(PEEK_OPEN_DELAY_MS * 2) })
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
  })

  it('ignores a touch pointer: a tap has no leave, so it would stick open', () => {
    const { box, p } = renderCard()
    hoverAndRest(box, 'touch')
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
  })

  it('holds the peek open while a control inside it has keyboard focus', () => {
    const { box, p } = renderCard()
    const jump = box.querySelector('button') as HTMLButtonElement
    const chevron = screen.getByLabelText(/expand/i) as HTMLButtonElement
    act(() => { moveFocus(null, jump) })
    expect(clampOf(p)).toBe(String(PINNED_PREVIEW_LINES))
    // Tab from the jump region to the chevron: focusout fires with the chevron
    // as relatedTarget, still inside the box, so the peek must not blink shut.
    act(() => { moveFocus(jump, chevron) })
    expect(clampOf(p)).toBe(String(PINNED_PREVIEW_LINES))
    // Leaving the box entirely closes it.
    act(() => { moveFocus(chevron, null) })
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
  })

  it('does not let a mouse click on the chevron hold the peek through the focus it leaves', () => {
    // Real browser order: pointerdown → the button takes focus (focusin) →
    // pointerup → click. A mouse user collapsing the card must land on ONE line
    // once the pointer leaves, not on three because the chevron kept focus.
    const { box, p } = renderCard()
    const chevron = screen.getByLabelText(/expand/i) as HTMLButtonElement
    hoverAndRest(box)
    act(() => {
      chevron.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'mouse' }))
      moveFocus(null, chevron)
      chevron.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerType: 'mouse' }))
    })
    act(() => { pointer(box, 'pointerleave', 'mouse') })
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
  })

  it('never reports a peeked height as the collapsed height', () => {
    const onCollapsedHeight = vi.fn()
    const { box } = renderCard({ onCollapsedHeight })
    const atRest = onCollapsedHeight.mock.calls.length
    expect(atRest).toBeGreaterThan(0)
    hoverAndRest(box)
    // The peek morph must not have reported: the hand-off line would follow it.
    expect(onCollapsedHeight.mock.calls.length).toBe(atRest)
    act(() => { pointer(box, 'pointerleave', 'mouse') })
    // Closing lands back at rest and re-reports the resting height.
    expect(onCollapsedHeight.mock.calls.length).toBeGreaterThan(atRest)
  })

  it('does not peek while expanded — the whole prompt is already showing', () => {
    const { box, p } = renderCard({ expanded: true })
    hoverAndRest(box)
    expect(clampOf(p)).toBe('')
  })

  it('closes the peek while the card is being pushed out, and reopens once it is at rest again', () => {
    // A pointer parked on the card while the user wheel-scrolls: the next prompt
    // pushes the card up through a band sized for its RESTING height. A
    // three-line card there would overhang the band, so the peek yields to the
    // push and the card departs at one line. The hover is still held, so once
    // the push recedes (the user scrolls back) the peek returns without a new
    // enter.
    const { box, p, rerender } = renderCard()
    hoverAndRest(box)
    expect(clampOf(p)).toBe(String(PINNED_PREVIEW_LINES))
    const props = {
      text: 'a prompt long enough that one line cannot hold it, and neither can three',
      fullText: 'a prompt long enough that one line cannot hold it, and neither can three\nsecond paragraph\nthird paragraph',
      images: [] as string[], bodyBeyondPreview: true, bannerH: 40, expanded: false,
      onToggleExpanded: () => {}, onJump: () => {}, onCollapsedHeight: () => {},
    }
    rerender(<PinnedPrompt {...props} pushUp={12} />)
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
    rerender(<PinnedPrompt {...props} pushUp={0} />)
    expect(clampOf(p)).toBe(String(PINNED_PREVIEW_LINES))
  })

  it('starts a fresh pin at rest even if the pointer never left', () => {
    const { box, p, rerender } = renderCard()
    hoverAndRest(box)
    expect(clampOf(p)).toBe(String(PINNED_PREVIEW_LINES))
    rerender(
      <PinnedPrompt
        text="a different prompt"
        fullText="a different prompt, with a different body"
        images={[]}
        bodyBeyondPreview
        pushUp={0}
        bannerH={40}
        expanded={false}
        onToggleExpanded={() => {}}
        onJump={() => {}}
        onCollapsedHeight={() => {}}
      />,
    )
    expect(clampOf(p)).toBe(String(PINNED_RESTING_LINES))
  })

  it('cancels a pending open on unmount', () => {
    const { box, unmount } = renderCard()
    act(() => { pointer(box, 'pointerenter', 'mouse') })
    unmount()
    // A timer that survived unmount would call setState on a dead component.
    expect(() => act(() => { vi.advanceTimersByTime(PEEK_OPEN_DELAY_MS * 2) })).not.toThrow()
  })
})
