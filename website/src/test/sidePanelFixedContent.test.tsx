/**
 * Per-tab scroll containment in SidePanelLayout.
 *
 * A Settings tab is normally one long form and the page scrolls it. One tab —
 * Releases — puts a version rail beside the notes, and letting the page scroll
 * carried the tab header and that rail off the top while the reader was still
 * inside one release's notes. `SidePanelTab.fixedContent` bounds that pane
 * instead, so its own `overflow-y-auto` children do the scrolling.
 *
 * The classes ARE the contract here: containment is expressed entirely in
 * layout CSS (`overflow-hidden` on the column, `flex-1 min-h-0 flex flex-col`
 * on the pane wrapper), and jsdom computes no geometry, so there is nothing
 * else to assert. Whether it actually pins the header under a real wheel
 * gesture is measured in website/scripts/capture-releases-scroll.mjs.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))

const TABS: SidePanelTab[] = [
  { key: 'form', label: 'Form', icon: null },
  { key: 'archive', label: 'Archive', icon: null, fixedContent: true },
]

/** The scrolling column, and the wrapper the pane is handed. */
function layout() {
  const header = screen.getByTestId('side-panel-header')
  const column = header.parentElement!
  const wrapper = screen.getByTestId('pane').parentElement!
  return { column, wrapper }
}

function renderPage(opts: { url?: string; fixedContent?: boolean; tabs?: SidePanelTab[] } = {}) {
  return render(
    <MemoryRouter initialEntries={[opts.url ?? '/page']}>
      <SidePanelLayout title="Test" tabs={opts.tabs ?? TABS} fixedContent={opts.fixedContent}>
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

describe('SidePanelLayout per-tab scroll containment', () => {
  beforeEach(() => { mobile.value = false })

  it('lets the page scroll a tab that does not ask to be contained', () => {
    renderPage()
    const { column, wrapper } = layout()
    expect(column.className).toContain('overflow-y-auto')
    expect(column.className).not.toContain('overflow-hidden')
    // The bottom padding only makes sense on a growing pane: a contained one
    // ends at the window edge and pads inside its own scroller instead.
    expect(wrapper.className).toContain('pb-8')
    expect(wrapper.className).not.toContain('min-h-0')
  })

  it('contains the pane of a tab that declares fixedContent', () => {
    renderPage({ url: '/page?tab=archive' })
    const { column, wrapper } = layout()
    expect(column.className).toContain('overflow-hidden')
    expect(column.className).not.toContain('overflow-y-auto')
    // `min-h-0` is the load-bearing half: without it the flex child refuses to
    // shrink below its content and the inner scrollers never engage, which is
    // exactly the state that shipped.
    expect(wrapper.className).toContain('min-h-0')
    expect(wrapper.className).toContain('flex-1')
    expect(wrapper.className).not.toContain('pb-8')
  })

  it('switches containment with the tab rather than latching on', () => {
    renderPage({ url: '/page?tab=archive' })
    expect(layout().column.className).toContain('overflow-hidden')
    fireEvent.click(screen.getByRole('button', { name: 'Form' }))
    expect(screen.getByTestId('pane').textContent).toBe('form')
    expect(layout().column.className).toContain('overflow-y-auto')
  })

  it('still honours the page-level prop for every tab', () => {
    renderPage({ fixedContent: true, tabs: [{ key: 'form', label: 'Form', icon: null }] })
    expect(layout().column.className).toContain('overflow-hidden')
  })

  it('ignores the per-tab flag on mobile, where the pane has no width to keep', () => {
    // The rail and the notes would get ~150px each on a phone, so containing
    // them hands the reader two thumb-sized scrollers instead of one page.
    mobile.value = true
    renderPage({ url: '/page?tab=archive' })
    const wrapper = screen.getByTestId('pane').parentElement!
    expect(wrapper.className).toContain('pb-8')
    expect(wrapper.className).not.toContain('min-h-0')
  })

  it('honours the page-level prop on mobile, which is not a per-tab guess', () => {
    mobile.value = true
    renderPage({ fixedContent: true, url: '/page?tab=archive' })
    const wrapper = screen.getByTestId('pane').parentElement!
    expect(wrapper.className).toContain('min-h-0')
    expect(wrapper.className).not.toContain('pb-8')
  })
})
