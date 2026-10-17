/**
 * The desktop side-panel rail (Settings, Customize, Developer) scrolls its tab
 * list inside a fixed-height column, and the overlay scrollbar stays hidden
 * until hovered. Settings has enough rows that a laptop-height window clips the
 * last ones (Developer, Releases, About), and the list then reads as complete.
 * The cue is a "More below" label in its own strip under the list, driven by
 * the shared `useScrollEdgesY` measurement.
 *
 * These tests pin the wiring, and each names what reverting it breaks:
 *
 *   - a list that fits shows no strip (revert symptom: a permanent cue lies
 *     that rows are hidden),
 *   - a clipped list shows the label (revert symptom: no sign that rows sit
 *     below the fold, the original confusion),
 *   - at the end of the list the label hides but the strip keeps its place
 *     (revert symptom: the list grows by the strip's height on the last
 *     scroll and jumps),
 *   - it is a decorative label, not a control: out of the accessibility
 *     tree, with no button inside, while the rows stay real buttons.
 *
 * jsdom does no layout, so scroll geometry is stubbed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const TABS: SidePanelTab[] = [
  { key: 'overview', label: 'Overview', icon: null },
  { key: 'secrets', label: 'Secrets', icon: null, group: 'System' },
  { key: 'developer', label: 'Developer', icon: null, group: 'System' },
]

/** `hidden` px of rows below the fold, `scrolled` px already past the top. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientHeight', 'get').mockReturnValue(400)
  vi.spyOn(proto, 'scrollHeight', 'get').mockReturnValue(400 + hidden)
  vi.spyOn(proto, 'scrollTop', 'get').mockReturnValue(scrolled)
}

function renderRail() {
  return render(
    <MemoryRouter initialEntries={['/settings']}>
      <SidePanelLayout title="Settings" tabs={TABS} rememberKey="test-tab-list-cue">
        {tab => <div data-testid="pane">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

const list = () => screen.getByTestId('side-panel-tab-list')
const more = () => screen.queryByTestId('side-panel-tab-list-more')

describe('side-panel rail: "More below" label on a clipped tab list', () => {
  beforeEach(() => {
    sessionStorage.clear()
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('shows nothing when every row fits', () => {
    stubGeometry({ hidden: 0 })
    renderRail()
    expect(more()).toBeNull()
  })

  it('shows the label when rows sit below the fold', () => {
    stubGeometry({ hidden: 120 })
    renderRail()
    expect(more()).not.toBeNull()
    expect(more()!.className).not.toContain('invisible')
  })

  it('hides the label at the end of the list but keeps the strip, so the list does not jump', () => {
    stubGeometry({ hidden: 120 })
    renderRail()
    vi.spyOn(window.HTMLElement.prototype, 'scrollTop', 'get').mockReturnValue(120)
    act(() => { fireEvent.scroll(list()) })
    expect(more()).not.toBeNull()
    expect(more()!.className).toContain('invisible')
  })

  it('is a decorative label, not a control', () => {
    stubGeometry({ hidden: 120 })
    renderRail()
    const cue = more()!
    expect(cue.tagName).toBe('DIV')
    expect(cue).toHaveAttribute('aria-hidden', 'true')
    expect(cue.querySelector('button, a, [tabindex]')).toBeNull()
    // The rows it points at are still real, reachable buttons.
    fireEvent.click(screen.getByRole('button', { name: 'Developer' }))
    expect(screen.getByTestId('pane').textContent).toBe('developer')
  })
})
