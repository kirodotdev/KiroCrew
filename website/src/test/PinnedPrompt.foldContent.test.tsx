import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
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

  it('keeps the chevron while folding, so the reader keeps the way to the full prompt', () => {
    paragraphOf({ liveH: 816 })
    expect(screen.getByLabelText(/expand/i)).toBeTruthy()
  })
})
