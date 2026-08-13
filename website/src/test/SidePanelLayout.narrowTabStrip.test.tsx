/**
 * The narrow-width tab strip owes the reader two things a plain scroller with a
 * hidden scrollbar does not give:
 *
 *   1. evidence that it is clipped. Settings carries seventeen tabs; measured on
 *      a real build at 390px, 1013px of them sit outside a 358px strip with the
 *      scrollbar hidden, so the row reads as complete and four tabs look like
 *      all of them.
 *   2. the ACTIVE tab inside the visible window. Every entry point that does not
 *      start on the first tab — deep link, command palette, the remembered tab
 *      from the last visit — parked the strip at offset 0, leaving the selected
 *      pill a full screen-width away with nothing on screen saying so.
 *
 * jsdom does no layout, so the scroll geometry is stubbed: `scrollWidth` and
 * `clientWidth` are the two numbers the cue is derived from, and stubbing them
 * is what lets the derivation be tested at all.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

let mobile = true
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  { key: 'security', label: 'Security', icon: null },
  { key: 'about', label: 'About', icon: null },
]

/** Force the strip's geometry: `hidden` px of content beyond the right edge,
 *  `scrolled` px already scrolled past the left one. */
function stubStripGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(358)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(358 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/settings${search}`]}>
      <SidePanelLayout title="Settings" tabs={TABS}>
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

describe('narrow tab strip overflow cues', () => {
  beforeEach(() => {
    mobile = true
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('marks the clipped edge when content is hidden past it', () => {
    stubStripGeometry({ hidden: 1013 })
    renderAt('')
    expect(screen.getByTestId('tab-strip-cue-right')).toBeTruthy()
    // Nothing is hidden to the left at offset 0, so no cue there: a cue on an
    // edge that hides nothing is the same lie in the other direction.
    expect(screen.queryByTestId('tab-strip-cue-left')).toBeNull()
  })

  it('marks both edges once the strip is scrolled into the middle', () => {
    stubStripGeometry({ hidden: 1013, scrolled: 400 })
    renderAt('')
    expect(screen.getByTestId('tab-strip-cue-left')).toBeTruthy()
    expect(screen.getByTestId('tab-strip-cue-right')).toBeTruthy()
  })

  it('marks no edge when every tab fits', () => {
    stubStripGeometry({ hidden: 0 })
    renderAt('')
    expect(screen.queryByTestId('tab-strip-cue-left')).toBeNull()
    expect(screen.queryByTestId('tab-strip-cue-right')).toBeNull()
  })

  it('scrolls the deep-linked active tab into view', () => {
    stubStripGeometry({ hidden: 1013 })
    const scrollIntoView = vi.fn()
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView
    renderAt('?tab=about')

    expect(screen.getByTestId('pane').textContent).toBe('about')
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest', inline: 'center' })
  })

  it('leaves the desktop rail alone — it has no strip to scroll', () => {
    mobile = false
    stubStripGeometry({ hidden: 1013 })
    const scrollIntoView = vi.fn()
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView
    renderAt('?tab=about')

    expect(screen.queryByTestId('tab-strip-cue-right')).toBeNull()
    expect(scrollIntoView).not.toHaveBeenCalled()
  })

  it('tracks scrolling on a strip that mounts after a desktop→mobile resize', () => {
    // The strip exists only in the mobile branch, so on a desktop-first mount
    // there is no node to bind to. A mount-only effect would attach nothing and
    // never run again, freezing the cues for the rest of the session.
    mobile = false
    stubStripGeometry({ hidden: 1013 })
    const { rerender } = renderAt('')
    expect(screen.queryByTestId('tab-strip-cue-right')).toBeNull()

    mobile = true
    rerender(
      <MemoryRouter initialEntries={['/settings']}>
        <SidePanelLayout title="Settings" tabs={TABS}>
          {tab => <div data-testid="pane">{tab}</div>}
        </SidePanelLayout>
      </MemoryRouter>,
    )
    const strip = screen.getByTestId('tab-strip-cue-right').parentElement!.querySelector('div')!

    // Scroll the strip: only a listener bound to the late-mounted node can turn
    // the left cue on.
    expect(screen.queryByTestId('tab-strip-cue-left')).toBeNull()
    stubStripGeometry({ hidden: 1013, scrolled: 400 })
    fireEvent.scroll(strip)
    expect(screen.getByTestId('tab-strip-cue-left')).toBeTruthy()
  })
})
