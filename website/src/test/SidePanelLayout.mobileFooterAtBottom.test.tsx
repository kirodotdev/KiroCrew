/**
 * The side panel's footer sits at the BOTTOM on a phone, not in the header block.
 *
 * Settings is the only caller that passes a `footer`, and what it passes is
 * `KiroCrew v<version>`. On desktop that lands where a reader expects it: `mt-auto`
 * at the foot of the 200px nav rail. The narrow branch used to render the same node
 * inside the mobile header block, immediately under the scrolling tab strip — so on
 * a phone the version read as a subtitle of the strip, and a reader looking for it
 * where desktop puts it concluded the phone showed no version at all. It was
 * present and in the wrong place, which is indistinguishable from missing.
 *
 * What is asserted here is DOM ORDER rather than pixels, because jsdom computes no
 * geometry and order is the actual contract: the footer must follow the content
 * pane, and must not be a descendant of the header block. The last assertion guards
 * a specific trap — the footer must NOT be `position: fixed`. A fixed bar would
 * spend a phone's scarce vertical space on a version string, and in this shell it
 * resolves against a transformed ancestor rather than the viewport (the same bug
 * that put an app's tab bar mid-screen until it was portalled to `document.body`),
 * so it would not even land where it claims to.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'

const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))

const TABS: SidePanelTab[] = [
  { key: 'form', label: 'Form', icon: null },
  { key: 'archive', label: 'Archive', icon: null },
]

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={['/page']}>
      <SidePanelLayout
        title="Test"
        tabs={TABS}
        footer={<span>KiroCrew v9.9.9</span>}
      >
        {tab => <div data-testid="pane-body">{tab}</div>}
      </SidePanelLayout>
    </MemoryRouter>,
  )
}

describe('SidePanelLayout footer placement', () => {
  beforeEach(() => { mobile.value = false })

  it('renders the footer after the content pane on a phone', () => {
    mobile.value = true
    renderPanel()

    const footer = screen.getByTestId('side-panel-footer')
    const pane = screen.getByTestId('side-panel-pane')
    expect(footer).toHaveTextContent('KiroCrew v9.9.9')
    // Node.DOCUMENT_POSITION_FOLLOWING (4): the footer comes after the pane.
    expect(pane.compareDocumentPosition(footer) & 4).toBe(4)
  })

  it('keeps the footer out of the header block that holds the tab strip', () => {
    mobile.value = true
    renderPanel()

    const footer = screen.getByTestId('side-panel-footer')
    const header = screen.getByTestId('side-panel-mobile-header')
    // The header block is where the version used to live, reading as a subtitle of
    // the pills. Asserting against the block itself — not some div near a pill —
    // is what makes this fail if the footer moves back into it.
    expect(header).toContainElement(screen.getByRole('button', { name: 'Form' }))
    expect(header.contains(footer)).toBe(false)
  })

  it('never positions the footer fixed, so it cannot miss the viewport', () => {
    mobile.value = true
    renderPanel()

    const cls = screen.getByTestId('side-panel-footer').className
    expect(cls).not.toMatch(/(?:^|\s)(?:[a-z-]+:)?fixed(?:\s|$)/)
    expect(cls).not.toMatch(/(?:^|\s)(?:[a-z-]+:)?absolute(?:\s|$)/)
  })

  it('leaves the desktop footer in the nav rail, pushed down by mt-auto', () => {
    renderPanel()

    // Desktop keeps its own footer node inside the rail; the narrow one is absent.
    expect(screen.queryByTestId('side-panel-footer')).toBeNull()
    const nav = document.querySelector('nav')
    expect(nav?.textContent).toContain('KiroCrew v9.9.9')
    expect(nav?.querySelector('.mt-auto')?.textContent).toContain('KiroCrew v9.9.9')
  })
})
