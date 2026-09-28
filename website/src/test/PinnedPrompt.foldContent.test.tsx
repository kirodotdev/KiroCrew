import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import PinnedPrompt from '../pages/chat/PinnedPrompt'

// The progressive fold holds the card at the pinned row's REMAINING height, so a tall
// prompt hands off at a card the size of its own bubble. Growing the box is only half
// of that: the paragraph inside is clamped to PINNED_RESTING_LINES at rest, and the
// clamp opens only on hover or keyboard focus (the peek). A scrolling reader triggers
// neither, so a grown box kept the one-line clamp and the card became a tall opaque
// panel with a single ellipsized line in it, parked on top of the lines the reader had
// not read yet. Measured in the capture harness at PINNED_RESTING_LINES = 1: an 816px
// card holding one line, with the prompt's other 29 lines covered.
//
// That is the same blank hole the fold exists to close, moved inside the card, and the
// card is opaque so it hides content the hole merely spaced away. While the fold runs
// the text must therefore track the box: no clamp, whole prompt, and the box's own
// `overflow: hidden` at exactly `liveH` does the trimming.
const PREVIEW = 'Line 1 of a pasted stack trace that the reader has not finished reading yet.'
const UNREAD = 'Line 30 of a pasted stack trace that the reader has not finished reading yet.'
const FULL = [PREVIEW, 'Line 2 ...', UNREAD].join('\n')

function paragraphOf(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
  const { unmount } = render(
    <PinnedPrompt
      text={PREVIEW}
      fullText={FULL}
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
  const p = screen.getByTestId('pinned-prompt').querySelector('p')
  if (!p) throw new Error('pinned card rendered no paragraph')
  return { p, unmount }
}

// What these assert, and what they do not. jsdom does not record `-webkit-line-clamp`
// at all — React sets it, and both `style.webkitLineClamp` and
// `getPropertyValue('-webkit-line-clamp')` come back empty — so asserting the clamp
// VALUE here would pass whether or not the clamp is applied, which is a guard that
// only looks like one. The two things jsdom does expose are the rendered TEXT and the
// wrapping class, and the text is the harm itself: unread lines either reach the DOM
// while the card is grown, or they do not. The pixel claim (an 816px card filled with
// the prompt rather than one ellipsized line) is held by the capture harness instead.
describe('PinnedPrompt — the fold-grown card must not hide unread lines', () => {
  it('shows only the preview at rest', () => {
    const { p } = paragraphOf()
    expect(p.textContent).toContain(PREVIEW)
    expect(p.textContent).not.toContain(UNREAD)
    expect(p.className).not.toContain('whitespace-pre-wrap')
  })

  it('lets the text wrap while the fold holds the card grown', () => {
    const { p } = paragraphOf({ liveH: 816 })
    expect(p.className).toContain('whitespace-pre-wrap')
  })

  it('renders the unread body while folding, so the grown box is not empty', () => {
    const { p } = paragraphOf({ liveH: 816 })
    expect(p.textContent).toContain(UNREAD)
  })

  it('fills the box at every fold height, not only the tallest', () => {
    for (const liveH of [120, 400, 816]) {
      const { p, unmount } = paragraphOf({ liveH })
      expect(p.textContent, `liveH=${liveH}`).toContain(UNREAD)
      expect(p.className, `liveH=${liveH}`).toContain('whitespace-pre-wrap')
      unmount()
    }
  })

  it('returns to the preview once the fold closes', () => {
    const { p } = paragraphOf({ liveH: undefined })
    expect(p.textContent).not.toContain(UNREAD)
    expect(p.className).not.toContain('whitespace-pre-wrap')
  })

  it('hides the chevron while folding, because the fold already shows the full prompt', () => {
    paragraphOf({ liveH: 816 })
    // Not a reduction in reach: while folding the card renders `fullText`, so the
    // control offers nothing that is not already on screen, and its click would land
    // with no visible effect until the fold ended.
    expect(screen.queryByLabelText(/expand/i)).toBeNull()
  })

  it('brings the chevron back once the fold ends', () => {
    paragraphOf({ liveH: undefined })
    expect(screen.getByLabelText(/expand/i)).toBeTruthy()
  })
})

// The chevron stays clickable while the fold runs, so `expanded` and the fold can be
// true at the same time. `expanded` caps the text at 40vh and scrolls it, which is
// right for a card sized to its own content — but during the fold the BOX height is
// the pinned row's remaining height, so a 40vh cap inside an 816px box leaves opaque
// empty card over the unread lines. That is the hole this fold exists to close,
// reached through the button the card deliberately keeps live. So the fold outranks
// `expanded` for as long as it runs.
describe('PinnedPrompt — expanding mid-fold does not re-open the hole', () => {
  it('keeps the text filling the box instead of capping it at 40vh', () => {
    const { p } = paragraphOf({ liveH: 816, expanded: true })
    expect(p.className).not.toContain('max-h-[40vh]')
    expect(p.className).not.toContain('overflow-y-auto')
    expect(p.className).toContain('overflow-hidden')
    expect(p.textContent).toContain(UNREAD)
  })

  it('restores the expanded cap once the fold ends', () => {
    const { p } = paragraphOf({ liveH: undefined, expanded: true })
    expect(p.className).toContain('max-h-[40vh]')
    expect(p.className).toContain('overflow-y-auto')
  })

  it('caps at every fold height, not only the tallest', () => {
    for (const liveH of [120, 400, 816]) {
      const { p, unmount } = paragraphOf({ liveH, expanded: true })
      expect(p.className, `liveH=${liveH}`).not.toContain('max-h-[40vh]')
      unmount()
    }
  })
})

// The prompt text sits inside the jump button. At rest that is one line, so the button
// is a small deliberate target. While the fold holds the card at the pinned row's
// height the same button can cover most of the viewport as ordinary-looking text, and
// a click meant to place a caret would scroll the transcript away from the place the
// reader is holding -- the harm this fold exists to prevent, delivered by the fold's
// own growth.
describe('PinnedPrompt — the grown card is not one viewport-sized jump button', () => {
  function jumpButtonOf(over: Partial<Parameters<typeof PinnedPrompt>[0]> = {}) {
    const onJump = vi.fn()
    render(
      <PinnedPrompt
        text={PREVIEW}
        fullText={FULL}
        images={[]}
        bodyBeyondPreview
        pushUp={0}
        bannerH={40}
        expanded={false}
        onToggleExpanded={() => {}}
        onJump={onJump}
        onCollapsedHeight={() => {}}
        {...over}
      />,
    )
    const p = screen.getByTestId('pinned-prompt').querySelector('p')
    if (!p) throw new Error('pinned card rendered no paragraph')
    const button = p.closest('button')
    if (!button) throw new Error('paragraph is not inside a button')
    return { button, onJump }
  }

  it('does not jump when the grown card text is clicked', () => {
    const { button, onJump } = jumpButtonOf({ liveH: 816 })
    fireEvent.click(button)
    expect(onJump).not.toHaveBeenCalled()
    expect(button.className).not.toContain('cursor-pointer')
    expect(button.getAttribute('aria-disabled')).toBe('true')
    // Keyboard focus is withheld too, so nobody lands on a control that does nothing.
    expect(button.getAttribute('tabindex')).toBe('-1')
  })

  it('jumps again once the fold ends', () => {
    const { button, onJump } = jumpButtonOf({ liveH: undefined })
    fireEvent.click(button)
    expect(onJump).toHaveBeenCalledTimes(1)
    expect(button.className).toContain('cursor-pointer')
    expect(button.getAttribute('aria-disabled')).toBeNull()
  })
})
