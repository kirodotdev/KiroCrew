/**
 * Regression for #4074: the fargate turn-URL field hides its scrollbar
 * (`scrollbar-none`) and holds a single long URL, so once the URL overflows
 * there was NO signal that it continues past the clipped edge — the same
 * defect the WebhooksPage CopyField had before this change. The fix wires the
 * shared `useScrollEdges` hook inline (identical treatment to CopyField) to
 * paint a gradient over the clipped edge.
 *
 * Each spec names what reverting the fix breaks:
 *   - a field that fits shows no cue (a permanent fade would lie that the URL
 *     is clipped),
 *   - a clipped field cues the hidden side only (no cue at all is the original
 *     defect),
 *   - the cues follow the field as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read).
 *
 * jsdom does no layout, so scroll geometry is stubbed — mirroring
 * EmbedTabStrip.scrollEdges.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
import { TurnUrlField } from '../pages/settings/RemoteCrewPanel'

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(300)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(300 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

const URL = 'http://127.0.0.1:49721/crew/example/turn?token=' + 'a'.repeat(80)

function renderField() {
  // Plain render: TurnUrlField's only app dependency is the global `i18nT`
  // (no provider), and the shared `renderWithProviders` ThemeProvider does an
  // async mount update that trips the act-warning gate here. Mirrors
  // AppDetailPage.screenshotScrollEdges.test.tsx.
  return render(<TurnUrlField url={URL} crewName="example" />)
}

const leftCue = () => screen.queryByTestId('turn-url-cue-left')
const rightCue = () => screen.queryByTestId('turn-url-cue-right')
const scroller = () => document.querySelector('.overflow-x-auto') as HTMLElement

describe('RemoteCrewPanel turn-URL scroll-edge cues (#4074)', () => {
  beforeEach(() => {
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => {
    vi.restoreAllMocks()
    cleanup()
  })

  it('shows no cue when the URL fits', () => {
    stubGeometry({ hidden: 0 })
    renderField()
    act(() => { scroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the clipped (right) edge at the start of an overflowing URL', () => {
    stubGeometry({ hidden: 400, scrolled: 0 })
    renderField()
    act(() => { scroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).toBeNull()
    expect(rightCue()).not.toBeNull()
  })

  it('cues both edges when scrolled into the middle', () => {
    stubGeometry({ hidden: 400, scrolled: 200 })
    renderField()
    act(() => { scroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).not.toBeNull()
    expect(rightCue()).not.toBeNull()
  })
})
