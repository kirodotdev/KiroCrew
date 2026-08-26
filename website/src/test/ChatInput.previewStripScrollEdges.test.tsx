/**
 * The staged-attachment preview strip scrolls horizontally, and on macOS/iOS
 * the overlay scrollbar leaves no idle trace, so the strip reads as complete
 * while chips sit off-screen. The cue is the shared `useScrollEdges`
 * measurement painting a gradient over the clipped edge — the same treatment
 * the sibling strips (FollowUpBar's scroll row, SidePanelLayout's tab strip,
 * the file-explorer TabStrip) already ship.
 *
 * These tests pin the wiring, and each names what reverting it breaks:
 *
 *   - a strip that fits shows no cue (revert symptom: a permanent fade lies
 *     that content is hidden),
 *   - a clipped strip cues the hidden side only (revert symptom: no signal at
 *     all — the original defect),
 *   - the cues follow the strip as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read),
 *   - staging another chip remeasures without any scroll or resize event
 *     (needs the remeasure effect keyed on the file list; the scroller keeps
 *     its own box when children change, so no observer fires).
 *
 * jsdom does no layout, so scroll geometry is stubbed — the stub is what makes
 * the derivation testable, mirroring FollowUpBar.scrollEdges.test.tsx.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

// Non-image staged files keep the chips text-only, so no /api/file-raw
// thumbnail requests are involved in the geometry under test.
const FILES = ['/tmp/a.txt', '/tmp/b.txt', '/tmp/c.txt', '/tmp/d.txt']

const leftCue = () => screen.queryByTestId('preview-strip-cue-left')
const rightCue = () => screen.queryByTestId('preview-strip-cue-right')
const scrollerOf = (container: HTMLElement) =>
  container.querySelector('[data-image-scope]') as HTMLElement

describe('FilePreviewStrip scroll-edge cues', () => {
  beforeEach(() => {
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('shows no cue when every chip fits', () => {
    stubGeometry({ hidden: 0 })
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={FILES} />)
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the side hiding content when the strip overflows', () => {
    stubGeometry({ hidden: 240 })
    renderWithProviders(<ChatInput {...defaultProps} pendingFiles={FILES} />)
    expect(rightCue()).toBeTruthy()
    // The cue is paint, not surface: it sits over the edge chips, so letting
    // it catch clicks would put a dead zone on the remove button underneath,
    // and it must stay silent to assistive tech.
    expect(rightCue()).toHaveClass('pointer-events-none')
    expect(rightCue()).toHaveAttribute('aria-hidden', 'true')
    // Nothing is hidden to the left at offset 0; a cue there would point at
    // content that does not exist.
    expect(leftCue()).toBeNull()
  })

  it('follows the strip as it scrolls', () => {
    stubGeometry({ hidden: 240 })
    const { container } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={FILES} />)
    expect(leftCue()).toBeNull()

    // Scrolled to the far end: the hidden side flips.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(scrollerOf(container))
    expect(leftCue()).toBeTruthy()
    expect(rightCue()).toBeNull()
  })

  it('remeasures when a chip is staged, without any scroll or resize event', () => {
    // The strip fits, then a paste adds a chip and the content now clips. The
    // scroller's own box never changed, so neither the ResizeObserver nor a
    // scroll event reports it — only the remeasure keyed on the staged list
    // can update the cue. Reverting that effect leaves this strip cue-less.
    stubGeometry({ hidden: 0 })
    const { rerender } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={FILES} />)
    expect(rightCue()).toBeNull()

    vi.restoreAllMocks()
    stubGeometry({ hidden: 240 })
    rerender(<ChatInput {...defaultProps} pendingFiles={[...FILES, '/tmp/e.txt']} />)
    expect(rightCue()).toBeTruthy()
  })

  it('remeasures when a thumbnail finishes loading', () => {
    // Image chips size themselves from intrinsic ratio at h-16, so the strip's
    // content widens when the bytes arrive — after the list-keyed remeasure
    // already ran. Nothing observes that growth (the scroller's own box is
    // unchanged); only the img load signal can refresh the cue. Reverting the
    // onLoad wiring leaves a strip full of freshly loaded thumbnails cue-less,
    // which is how the defect was reproduced against the real built SPA.
    stubGeometry({ hidden: 0 })
    const { container } = renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={['/tmp/a.png', '/tmp/b.png']} />,
    )
    expect(rightCue()).toBeNull()

    vi.restoreAllMocks()
    stubGeometry({ hidden: 240 })
    fireEvent.load(container.querySelector('[data-image-scope] img') as HTMLElement)
    expect(rightCue()).toBeTruthy()
  })
})
