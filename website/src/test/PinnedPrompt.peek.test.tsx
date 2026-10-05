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
  const clip = card.parentElement as HTMLElement
  const band = clip.parentElement as HTMLElement
  // The opaque mask layer is the band's absolute z-0 child (fill + lower fade);
  // the clip wrapper is the in-flow sibling that holds the card.
  const mask = band.querySelector(':scope > .z-0') as HTMLElement
  const fill = mask?.querySelector('.bg-bg') as HTMLElement
  const fade = mask?.querySelector('.bg-gradient-to-b.from-bg.to-transparent') as HTMLElement
  const box = card.firstElementChild as HTMLElement
  const p = box.querySelector('p') as HTMLElement
  return { ...utils, card, clip, band, mask, fill, fade, box, p }
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

  it('masks transcript text behind the card and fades the band into the reply', () => {
    const { card, clip, band, mask, fill, fade } = renderCard()
    // The opaque mask layer sits behind the card as the band's absolute z-0 child,
    // carrying the fill and the lower fade. The push shrink lives here, not on the
    // band, so the band can size to its in-flow card.
    expect(mask).not.toBeNull()
    expect(mask.className).toContain('absolute')
    expect(mask.className).toContain('z-0')
    expect(fill).not.toBeNull()
    expect(fill.className).toContain('inset-0')
    // The lower fade is a child of the MASK layer (overflow-visible), hung at
    // top-full off its bottom, so it is never clipped and tracks the shrink.
    expect(fade).not.toBeNull()
    expect(fade.className).toContain('top-full')
    expect(mask.style.overflow).not.toBe('hidden')
    // The card is in flow (so the band sizes to it) and paints above the mask.
    expect(card.parentElement).toBe(clip)
    expect(card.className).toContain('z-[1]')
    // The band itself never translates (its top is the fold) so it cannot rise
    // over the chat header.
    expect(band.style.transform).toBe('')
    expect(band.style.height).toBe('')
  })

  it('keeps the backdrop under the resting, peeked, and expanded card heights', () => {
    const { box, band, mask, rerender } = renderCard()
    // The band has NO explicit height — it tracks its in-flow card so an expanded
    // or peeked card is never clipped to a sliver. The opaque mask layer behind
    // the card carries the height (card box + ROW_PAD_Y*2; bannerH 40 is the
    // collapsed fallback before a real measure), minus the push.
    expect(band.style.height).toBe('')
    expect(mask.style.height).toBe('48px')

    hoverAndRest(box)
    // A peek stays at the same fallback height here (happy-dom measures the card
    // box as 0), and the band still has no explicit height.
    expect(band.style.height).toBe('')
    expect(mask.style.height).toBe('48px')
    act(() => { pointer(box, 'pointerleave', 'mouse') })
    expect(mask.style.height).toBe('48px')

    const expandedProps = {
      text: 'expanded prompt',
      fullText: 'expanded prompt\nwith more content',
      images: [] as string[],
      bodyBeyondPreview: true,
      bannerH: 40,
      expanded: true,
      onToggleExpanded: () => {},
      onJump: () => {},
      onCollapsedHeight: () => {},
    }
    rerender(<PinnedPrompt {...expandedProps} pushUp={0} />)
    expect(band.style.height).toBe('')
    expect(mask.style.height).toBe('48px')

    // Expansion stays overflow-visible while the next prompt pushes it; the mask
    // shrinks from the bottom by pushUp so the fill and fade follow the card and
    // no transcript text leaks around its lower lines.
    rerender(<PinnedPrompt {...expandedProps} pushUp={12} />)
    expect(band.style.height).toBe('')
    expect(mask.style.height).toBe('36px')
  })

  it('carries the push on the card while expanded and never translates the band, so it cannot paint over the header', () => {
    // Regression fix for the self-review: an earlier revision translated the whole
    // band up by `pushUp` while expanded, which slid the band's opaque fill up over
    // the chat header's title row and divider in the same overlay. The band now
    // NEVER translates — its top is pinned at the fold — and the push rides the
    // card (and the fill, which lives in the card's clip wrapper and shrinks with
    // the band height), so the backdrop still follows the card out of view but
    // nothing ever rises over the header.
    const expandedProps = {
      text: 'expanded prompt',
      fullText: 'expanded prompt\nwith more content',
      images: [] as string[],
      bodyBeyondPreview: true,
      bannerH: 40,
      expanded: true,
      onToggleExpanded: () => {},
      onJump: () => {},
      onCollapsedHeight: () => {},
    }
    const { card, band, rerender } = renderCard(expandedProps)

    // At rest neither the band nor the card is translated.
    expect(band.style.transform).toBe('')
    expect(card.style.transform).toBe('translateY(0px)')

    // Pushed: the card carries the translate, the band stays put at the fold.
    rerender(<PinnedPrompt {...expandedProps} pushUp={12} />)
    expect(band.style.transform).toBe('')
    expect(card.style.transform).toBe('translateY(-12px)')
  })

  it('while folding, ends the mask at the card bottom and drops the lower fade so the action strip stays visible', () => {
    // During a fold the hidden row's action strip is forced visible just below the
    // card (index.css `[data-pinned-standin="folding"]`). The opaque mask fill must
    // not carry its extra bottom ROW_PAD_Y over that strip, and the 24px top-full
    // lower fade must not render over it either — both would hide clickable controls.
    const props = {
      text: 'a prompt long enough that one line cannot hold it, and neither can three',
      fullText: 'a prompt long enough that one line cannot hold it, and neither can three\nsecond paragraph',
      images: [] as string[], bodyBeyondPreview: true, bannerH: 40, expanded: false,
      onToggleExpanded: () => {}, onJump: () => {}, onCollapsedHeight: () => {},
    }
    // At rest (not folding) the lower fade is present over the mask.
    const { mask, rerender } = renderCard(props)
    expect(mask.querySelector('.bg-gradient-to-b.from-bg.to-transparent')).not.toBeNull()

    // Folding (liveH set): the lower fade is dropped — a 24px top-full gradient
    // starting fully opaque would otherwise paint over the hidden row's action
    // strip, which is forced visible just below the card during a fold. (The mask
    // fill likewise ends at the card's bottom while folding; see `maskBottomPad`.)
    rerender(<PinnedPrompt {...props} liveH={120} />)
    const cardFolding = screen.getByTestId('pinned-prompt')
    const maskFolding = cardFolding.parentElement!.parentElement!.querySelector(':scope > .z-0') as HTMLElement
    expect(maskFolding.querySelector('.bg-gradient-to-b.from-bg.to-transparent')).toBeNull()
  })

  it('keeps the push on the card for a collapsed banner, and the lower fade is never clipped', () => {
    // The card is clipped by its in-flow wrapper ONLY while pushing (an inline
    // overflow, not a class), so the part risen above the fold is hidden and never
    // paints over the header. The lower fade lives on the absolute mask layer,
    // outside that clip wrapper, so `overflow: hidden` during the push can never
    // clip it — the regression the self-review flagged. The card carries its own
    // translate; the band never moves and keeps no explicit height.
    const props = {
      text: 'a prompt long enough that one line cannot hold it, and neither can three',
      fullText: 'a prompt long enough that one line cannot hold it, and neither can three\nsecond paragraph\nthird paragraph',
      images: [] as string[], bodyBeyondPreview: true, bannerH: 40, expanded: false,
      onToggleExpanded: () => {}, onJump: () => {}, onCollapsedHeight: () => {},
    }
    const { card, band, clip, mask, rerender } = renderCard(props)
    // At rest the clip wrapper is overflow-visible and the fade is mounted on the
    // mask layer, outside it.
    expect(clip.style.overflow).toBe('visible')
    expect(mask.querySelector('.bg-gradient-to-b.from-bg.to-transparent')).not.toBeNull()

    rerender(<PinnedPrompt {...props} pushUp={12} />)
    expect(card.style.transform).toBe('translateY(-12px)')
    expect(band.style.transform).toBe('')
    expect(band.style.height).toBe('')
    // The clip wrapper engages overflow:hidden during the push (clips the card at
    // the fold); the mask shrinks from the bottom (48 - 12) so the fill and fade
    // rise with the card and nothing is left behind over the transcript.
    expect(clip.style.overflow).toBe('hidden')
    expect(mask.style.height).toBe('36px')
    // The fade is still a mask child during the push — not swallowed by the clip.
    const fadePushed = mask.querySelector('.bg-gradient-to-b.from-bg.to-transparent')
    expect(fadePushed).not.toBeNull()
    expect(fadePushed?.className).toContain('top-full')
    expect(mask.style.overflow).not.toBe('hidden')
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
