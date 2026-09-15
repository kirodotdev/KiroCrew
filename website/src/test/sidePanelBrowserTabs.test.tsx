/**
 * The side panel's tab strip renders as BROWSER TABS: the active chip shares the
 * panel body's background (`--bg`), drops its bottom radius, and carries the
 * `side-tab-active` hook whose ::before/::after paint Chrome-style inverted
 * corners (index.css). Inactive chips are quiet text.
 *
 * Two of these pins guard clipping regressions that are invisible in jsdom and
 * were caught only by screenshot review:
 *  - the Reorder.Group scrolls (`overflow-x-auto`), so the FIRST tab's left
 *    corner piece (8px outside the chip) is clipped unless the group reserves
 *    room with padding (`px-2`) — and ONLY padding: a negative margin widens
 *    the clip box into the strip gaps, letting scrolled chips paint over the
 *    divider and the + button;
 *  - the corner pieces exist at all only through the index.css mask rules, so
 *    their presence is asserted against the stylesheet source.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, openPanelView } from '../hooks/usePanelTabs'
import { setSidePanelDock } from '../hooks/useSidePanelDock'

function Harness() {
  const tabsCtl = usePanelTabs('slot-tabs')
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-tabs"
      pins={[]}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

const cssSource = () => {
  const here = dirname(fileURLToPath(import.meta.url))
  return readFileSync(join(here, '..', 'index.css'), 'utf8')
}

describe('side panel browser-tab strip', () => {
  it('marks the active chip with side-tab-active + the body background, not inactive ones', () => {
    renderPanel()
    act(() => {
      openPanelView('slot-tabs', 'issues')
      openPanelView('slot-tabs', 'browser')
    })
    const tabs = screen.getAllByRole('tab')
    const active = tabs.filter(t => t.getAttribute('aria-selected') === 'true')
    const inactive = tabs.filter(t => t.getAttribute('aria-selected') !== 'true')
    expect(active.length).toBeGreaterThan(0)
    expect(inactive.length).toBeGreaterThan(0)
    for (const t of active) {
      expect(t.className).toContain('side-tab-active')
      // Word-boundary match: inactive chips carry hover:bg-bg-hover, which a
      // bare substring check would also match.
      expect(t.className).toMatch(/(^|\s)bg-bg(\s|$)/)
      // Browser-tab shape: top corners only. A bottom radius would re-open the
      // seam between the tab and the panel body it fuses into.
      expect(t.className).toContain('rounded-t-md')
      expect(t.className).toContain('rounded-b-none')
      // Theme-independent silhouette: a custom theme may set --bg equal to the
      // strip's --bg-elevated, so the active tab carries a top/side hairline.
      expect(t.className).toContain('border-t-border')
      // Bottom edge is transparent, not width-0: a border-b-0 box is 1px
      // asymmetric and sits the glyphs half a pixel low.
      expect(t.className).toContain('border-b-transparent')
    }
    for (const t of inactive) {
      expect(t.className).not.toContain('side-tab-active')
      expect(t.className).not.toMatch(/(^|\s)bg-bg(\s|$)/)
      // The inset hover wash depends on BOTH hooks: side-tab-inactive carries
      // the :hover::before rule, and isolate keeps its z-index:-1 inside the
      // chip's own stacking context (without it the wash paints behind the
      // strip and silently disappears).
      expect(t.className).toContain('side-tab-inactive')
      expect(t.className).toContain('isolate')
    }
    // The fuse itself: every chip carries the 1px border width (bottom edge
    // painted transparent on the active chip) and the strip bottom-aligns its
    // chips with no bottom padding, so the active chip's background runs into
    // the panel body. Restoring items-center or pb-2 re-opens the seam with
    // every class-pin above still green.
    for (const t of tabs) expect(t.className).toMatch(/(^|\s)border(\s|$)/)
    const strip = document.querySelector('.side-panel-strip')
    expect(strip).not.toBeNull()
    expect(strip!.className).toContain('items-end')
    expect(strip!.className).toContain('pb-0')
  })

  it('reserves corner room inside the scrollable tab group (px-2, no -mx)', () => {
    renderPanel()
    act(() => {
      openPanelView('slot-tabs', 'browser')
    })
    const group = screen.getByRole('tablist')
    // overflow-x-auto clips the first tab's left corner piece (::before at
    // left:-8px) without this padding/negative-margin pair.
    expect(group.className).toContain('overflow-x-auto')
    expect(group.className).toContain('px-2')
    // Deliberately NO negative margin: the scrollport's clip box must not
    // extend into the strip gaps, or scrolled chips paint over the divider
    // and the + button.
    expect(group.className).not.toContain('-mx-2')
  })

  it('draws the seam hairline the corner arcs land on (strip border-b, rows dropped 1px)', () => {
    renderPanel()
    act(() => {
      openPanelView('slot-tabs', 'browser')
    })
    // The flare arc ends tangent-horizontal at the seam; without a line to
    // continue into, the 1px stroke truncates mid-air. The strip's border-b is
    // that line, and each chip row drops one pixel over the border row so the
    // active chip's opaque background interrupts it across its own span.
    const strip = document.querySelector('.side-panel-strip')!
    expect(strip.className).toContain('border-b')
    expect(strip.className).toContain('border-border')
    const group = screen.getByRole('tablist')
    expect(group.className).toContain('-mb-px')
    // The drop lives on the GROUP containers, not the chips: the tablist
    // scrolls, and a chip's own negative margin would be clipped away.
    expect(group.parentElement!.querySelector(':scope > div.shrink-0')!.className).toContain('-mb-px')
  })

  it('goes transparent on the pinned↔dynamic divider when an adjacent tab is active', () => {
    renderPanel()
    act(() => {
      openPanelView('slot-tabs', 'files')   // pinned view
      openPanelView('slot-tabs', 'issues')  // first dynamic
      openPanelView('slot-tabs', 'browser') // last dynamic, becomes active
    })
    // Active (browser) is NOT adjacent to the divider: hairline paints.
    const divider = screen.getByTestId('strip-divider')
    expect(divider.className).toContain('bg-border')
    expect(divider.className).not.toContain('bg-transparent')
    // Activate the pinned Files tab (adjacent): the hairline goes transparent
    // (never unmounted — removing its layout slot would shift the row), so the
    // active chip's corner piece cannot be sliced by it.
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }))
    const after = screen.getByTestId('strip-divider')
    expect(after.className).toContain('bg-transparent')
    expect(after.className).not.toContain('bg-border')
  })

  it('index.css paints both inverted corner pieces for the active chip', () => {
    const css = cssSource()
    // Both sides, both mask spellings. The LEFT piece is the one a scroll
    // container clipped in review — assert each side separately so losing one
    // cannot pass.
    expect(css).toMatch(/\.side-tab-active::before[\s\S]{0,600}left:-9px/)
    expect(css).toMatch(/\.side-tab-active::after[\s\S]{0,600}right:-9px/)
    // Each piece's gradient carries a 1px --border arc between the transparent
    // concave region and the --bg wedge: it is what makes the side hairline
    // FOLLOW the curve instead of ending in a straight stub. Assert per side —
    // losing one arc leaves that side's outline broken with the other green.
    expect(css).toMatch(/\.side-tab-active::before\{[\s\S]{0,300}var\(--border\) 7px 8px,var\(--bg\) 8px/)
    expect(css).toMatch(/\.side-tab-active::after\{[\s\S]{0,300}var\(--border\) 7px 8px,var\(--bg\) 8px/)
    // The inactive hover wash must stay behind the chip's children but above
    // the strip: z-index:-1 paired with the chip's isolate class.
    expect(css).toMatch(/\.side-tab-inactive:hover::before\{[\s\S]{0,300}z-index:-1/)
  })

  // Focus mode takes the dashboard header out of flow, and that header's own
  // right inset is the ONLY thing clearing the native caption buttons on Windows
  // and frameless Linux. Right-docked, this strip is what then owns the window's
  // top-trailing corner, so its trailing controls (⋯, Close) end up under those
  // buttons — covered, and unclickable because the OS strip hit-tests above web
  // content (#6509). The class is only the opt-in; index.css picks the width per
  // platform and applies nothing outside focus mode.
  it('opts the right-docked strip into the focus-mode caption reserve', () => {
    setSidePanelDock('right')
    renderPanel()
    expect(document.querySelector('.side-panel-strip')!.className).toContain('focus-caption-reserve')
  })

  it('leaves the bottom-docked strip edge-to-edge', () => {
    // Bottom-docked the strip is pinned under the chat, nowhere near that
    // corner: padding there would be a gap with nothing behind it, and focus
    // mode exists to give the window's edges back.
    setSidePanelDock('bottom')
    try {
      renderPanel()
      expect(document.querySelector('.side-panel-strip')!.className).not.toContain('focus-caption-reserve')
    } finally {
      setSidePanelDock('right')
    }
  })
})
