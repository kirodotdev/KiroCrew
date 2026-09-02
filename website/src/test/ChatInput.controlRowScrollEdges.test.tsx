/**
 * The composer's control row (auto-nudge loop chip + approval-mode picker)
 * scrolls horizontally at narrow widths, and on macOS/iOS the overlay
 * scrollbar leaves no idle trace, so the row reads as complete while controls
 * sit off-screen. The cue is the shared `useScrollEdges` measurement painting
 * a gradient over the clipped edge — the same treatment the sibling strips
 * (FollowUpBar's scroll row, SidePanelLayout's tab strip) ship.
 *
 * These tests pin the wiring, and each names what reverting it breaks:
 *
 *   - a row that fits shows no cue (revert symptom: a permanent fade lies
 *     that a control is hidden),
 *   - a clipped row cues the hidden side only (revert symptom: no signal at
 *     all — the original defect),
 *   - the cues follow the row as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read),
 *   - a control appearing remeasures without any scroll or resize event
 *     (needs the remeasure effect keyed on the row's prop-driven content; the
 *     scroller keeps its own box when children change, so no observer fires).
 *
 * jsdom does no layout, so scroll geometry is stubbed — the stub is what makes
 * the derivation testable, mirroring FollowUpBar.scrollEdges.test.tsx.
 */
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

const leftCue = () => screen.queryByTestId('control-row-cue-left')
const rightCue = () => screen.queryByTestId('control-row-cue-right')
const controlRow = () => screen.getByTestId('composer-control-row')

describe('ChatInput control-row scroll-edge cues', () => {
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

  it('shows no cue when every control fits', () => {
    stubGeometry({ hidden: 0 })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the side hiding content when the row overflows', () => {
    stubGeometry({ hidden: 240 })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(rightCue()).toBeTruthy()
    // The cue is paint, not surface: it sits over the edge controls, so
    // letting it catch clicks would put a dead zone on the picker underneath,
    // and it must stay silent to assistive tech.
    expect(rightCue()).toHaveClass('pointer-events-none')
    expect(rightCue()).toHaveAttribute('aria-hidden', 'true')
    // Nothing is hidden to the left at offset 0; a cue there would point at
    // content that does not exist.
    expect(leftCue()).toBeNull()
  })

  it('follows the row as it scrolls', () => {
    stubGeometry({ hidden: 240 })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(leftCue()).toBeNull()

    // Scrolled to the far end: the hidden side flips.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(controlRow())
    expect(leftCue()).toBeTruthy()
    expect(rightCue()).toBeNull()
  })

  it('remeasures when a control appears, without any scroll or resize event', () => {
    // The row fits, then the approval-mode picker mounts (the slot's mode
    // arrives) and the content now clips. The scroller's own box never
    // changed, so neither the ResizeObserver nor a scroll event reports it —
    // only the remeasure keyed on the row's prop-driven content can update
    // the cue. Reverting that effect leaves this row cue-less.
    stubGeometry({ hidden: 0 })
    const { rerender } = renderWithProviders(<ChatInput {...defaultProps} />)
    expect(rightCue()).toBeNull()

    vi.restoreAllMocks()
    stubGeometry({ hidden: 240 })
    rerender(<ChatInput {...defaultProps} approvalMode="default" />)
    expect(rightCue()).toBeTruthy()
  })
})
