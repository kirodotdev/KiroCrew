/**
 * The pinned views (Changes / Artifacts / Files) show their name as visible
 * text on every chip, active or not.
 *
 * A first-time user in the GUI user test could not tell which icon was Files
 * and opened Artifacts instead: an icon-only chip names itself only to a
 * screen reader or a hover, and neither helps someone scanning the strip. So
 * each pinned chip carries its word, in the compact 12px chip style the
 * labelled tabs already use. On a strip too narrow for three labels the
 * inactive ones drop to their icon so all three stay on screen.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  return <SidePanel tabsCtl={tabsCtl} slot="slot-a" onFileSave={async () => {}} onClose={() => {}} />
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

describe('pinned side-panel tabs carry a visible label', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('shows each pinned name as visible text, including on inactive chips', () => {
    renderPanel()
    const chips = screen.getAllByRole('tab')
    // At least one of the three is inactive on a fresh panel, which is the
    // chip the user could not identify.
    expect(chips.some(c => c.getAttribute('aria-selected') === 'false')).toBe(true)
    // Visible text, not just the aria-label: textContent is what is painted.
    expect(chips.map(c => c.textContent)).toEqual(['Changes', 'Artifacts', 'Files'])
  })

  it('keeps the accessible name and hover tooltip on every pinned chip', () => {
    renderPanel()
    const files = screen.getByRole('tab', { name: 'Files' })
    expect(files).toHaveAttribute('title', 'Files')
    expect(files).toHaveTextContent('Files')
  })

  it('marks only inactive pinned chips for the narrow-strip icon fallback', () => {
    // happy-dom does not evaluate container queries, so this pins the wiring
    // the index.css `@container` rule keys on; the browser capture shows the
    // rule itself at 320px.
    renderPanel()
    for (const chip of screen.getAllByRole('tab')) {
      const idle = chip.getAttribute('aria-selected') === 'false'
      expect(chip.classList.contains('side-tab-pinned-idle')).toBe(idle)
    }
    expect(screen.getByTestId('side-panel-fixed-tabs').closest('.side-panel-strip-cq')).not.toBeNull()
  })
})
