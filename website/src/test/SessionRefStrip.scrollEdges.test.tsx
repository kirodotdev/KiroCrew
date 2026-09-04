/**
 * The staged-session-reference strip scrolls horizontally, and on macOS/iOS
 * the overlay scrollbar leaves no idle trace, so the strip reads as complete
 * while chips sit off-screen. The cue is the shared `useScrollEdges`
 * measurement painting a gradient over the clipped edge — the same treatment
 * the sibling strips (FollowUpBar's scroll row, SidePanelLayout's tab strip) ship.
 *
 * These tests pin the wiring, and each names what reverting it breaks:
 *
 *   - a strip that fits shows no cue (revert symptom: a permanent fade lies
 *     that content is hidden),
 *   - a clipped strip cues the hidden side only (revert symptom: no signal at
 *     all — the original defect),
 *   - the cues follow the strip as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read),
 *   - staging another reference remeasures without any scroll or resize event
 *     (needs the remeasure effect keyed on the refs list; the scroller keeps
 *     its own box when children change, so no observer fires).
 *
 * jsdom does no layout, so scroll geometry is stubbed — the stub is what makes
 * the derivation testable, mirroring FollowUpBar.scrollEdges.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import SessionRefStrip from '../components/SessionRefStrip'
import type { SessionRef } from '../utils/sessionRefs'

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

const ref = (key: string, title: string): SessionRef => ({ key, title })

const REFS = [ref('a', 'Release notes'), ref('b', 'Auth refactor'), ref('c', 'Design review')]

const leftCue = () => screen.queryByTestId('session-ref-strip-cue-left')
const rightCue = () => screen.queryByTestId('session-ref-strip-cue-right')

describe('SessionRefStrip scroll-edge cues', () => {
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
    render(<SessionRefStrip refs={REFS} />)
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the side hiding content when the strip overflows', () => {
    stubGeometry({ hidden: 240 })
    render(<SessionRefStrip refs={REFS} />)
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
    render(<SessionRefStrip refs={REFS} />)
    expect(leftCue()).toBeNull()

    // Scrolled to the far end: the hidden side flips.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(screen.getByTestId('session-ref-strip'))
    expect(leftCue()).toBeTruthy()
    expect(rightCue()).toBeNull()
  })

  it('remeasures when a reference is staged, without any scroll or resize event', () => {
    // The strip fits, then a drop stages a reference and the content now
    // clips. The scroller's own box never changed, so neither the
    // ResizeObserver nor a scroll event reports it — only the remeasure keyed
    // on the refs list can update the cue.
    stubGeometry({ hidden: 0 })
    const { rerender } = render(<SessionRefStrip refs={REFS} />)
    expect(rightCue()).toBeNull()

    vi.restoreAllMocks()
    stubGeometry({ hidden: 240 })
    rerender(<SessionRefStrip refs={[...REFS, ref('d', 'One more session')]} />)
    expect(rightCue()).toBeTruthy()
  })
})
