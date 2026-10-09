import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { WorkspaceFullscreenContext } from '../components/WorkspacePanelContext'
import { PanelBottomLight } from '../components/icons/panels'

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
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useBottomTerminal', () => ({
  useBottomTerminalOpen: () => false,
  toggleBottomTerminal: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  useTerminalPoppedOut: () => false,
  focusPopout: vi.fn(),
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs } from '../hooks/usePanelTabs'
import { setSidePanelDock } from '../hooks/useSidePanelDock'

function Harness() {
  const tabsCtl = usePanelTabs('slot-controls')
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-controls"
      pins={[]}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel(fullscreen = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const controls = { fullscreen, exit: vi.fn(), toggle: vi.fn() }
  const view = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <WorkspaceFullscreenContext.Provider value={controls}>
          <Harness />
        </WorkspaceFullscreenContext.Provider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, controls }
}

function orderedBefore(left: Element, right: Element): boolean {
  return Boolean(left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING)
}

describe('SidePanel workspace controls and retained browser-tab frame', () => {
  it('keeps the browser-tab strip on the aligned half-card header', () => {
    renderPanel()
    const panel = screen.getByRole('region', { name: 'Side panel' })
    const strip = document.querySelector('.side-panel-strip') as HTMLElement
    expect(panel.className).toContain('rounded-l-xl')
    expect(panel.className).toContain('border-t')
    expect(panel.className).toContain('border-b')
    expect(strip.className).toContain('items-end')
    expect(strip.className).toContain('pb-0')
    expect(strip.className).toContain('mt-0.5')
    expect(strip.className).toContain('h-10')
    expect(strip.className).toContain('pt-px')
    expect(strip.className).toContain('pr-[9px]')
    expect(strip.querySelector('[data-testid="panel-controls-panel-shape"]')).not.toBeNull()
    expect(strip.querySelector('[data-testid="panel-controls-workspace-visibility"]')).not.toBeNull()
    expect(screen.getAllByRole('tab').some(tab => tab.className.includes('side-tab-active'))).toBe(true)
  })

  it('separates panel-shape actions from workspace visibility toggles', () => {
    renderPanel()
    const fullscreen = screen.getByRole('button', { name: 'Full screen' })
    const terminal = screen.getByRole('button', { name: 'Show terminal' })
    const side = screen.getByRole('button', { name: 'Hide side panel' })
    const shapeRegion = fullscreen.closest('[data-testid="panel-controls-panel-shape"]') as HTMLElement
    const visibilityRegion = terminal.closest('[data-testid="panel-controls-workspace-visibility"]') as HTMLElement
    expect(orderedBefore(fullscreen, terminal)).toBe(true)
    expect(orderedBefore(terminal, side)).toBe(true)
    expect(shapeRegion.querySelectorAll('button')).toHaveLength(2)
    expect(visibilityRegion.querySelectorAll('button')).toHaveLength(2)
    expect(visibilityRegion.className).toContain('border-l')
    expect(visibilityRegion.className).toContain('pl-2')
    for (const control of [fullscreen, terminal, side]) {
      expect(control.className).toContain('w-7')
      expect(control.className).toContain('h-7')
      // All three are the shared Btn primitive, so they share its pressed feedback.
      expect(control.className).toContain('active:scale-[0.97]')
    }
    expect(fullscreen.querySelector('svg')?.getAttribute('class') ?? '').toContain('w-3.5')
    for (const control of [terminal, side]) {
      expect(control.querySelector('svg')).toHaveAttribute('width', '14')
      expect(control.querySelector('svg')).toHaveAttribute('height', '14')
    }
    expect(fullscreen.closest('[data-testid="panel-controls-fullscreen"]')?.querySelectorAll('button')).toHaveLength(1)
    expect(terminal.closest('[data-panel-toggles]')?.querySelectorAll('button')).toHaveLength(2)
  })

  it('keeps fullscreen as a direct button while the workspace is bottom-docked', () => {
    setSidePanelDock('bottom')
    try {
      renderPanel()
      const fullscreen = screen.getByRole('button', { name: 'Full screen' })
      expect(fullscreen).toBeVisible()
      expect(fullscreen.closest('[data-testid="panel-controls-fullscreen"]')).not.toBeNull()
      expect(fullscreen.closest('[role="menu"]')).toBeNull()
    } finally {
      setSidePanelDock('right')
    }
  })

  it('uses the same control to enter and leave workspace fullscreen', () => {
    const entered = renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(entered.controls.toggle).toHaveBeenCalledOnce()
    entered.unmount()

    renderPanel(true)
    const exit = screen.getByRole('button', { name: 'Exit full screen' })
    expect(exit).not.toHaveAttribute('aria-pressed')
    expect(screen.getByRole('region', { name: 'Side panel' }).className).toContain('h-full')
  })

  it('drops the dock menu while fullscreen, where a dock choice would show no change', () => {
    const normal = renderPanel()
    expect(screen.getByTestId('panel-controls-dock')).toBeInTheDocument()
    normal.unmount()
    renderPanel(true)
    expect(screen.queryByTestId('panel-controls-dock')).toBeNull()
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()
  })

  it('draws the open panel side toggle for where the panel docks', () => {
    const glyph = (el: Element) => el.querySelector('svg')!.innerHTML
    const bottomLight = render(<PanelBottomLight size={14} />).container.querySelector('svg')!.innerHTML
    const right = renderPanel()
    const rightGlyph = glyph(screen.getByRole('button', { name: 'Hide side panel' }))
    right.unmount()
    setSidePanelDock('bottom')
    try {
      renderPanel()
      const sideGlyph = glyph(screen.getByRole('button', { name: 'Hide side panel' }))
      expect(sideGlyph).toBe(bottomLight)
      expect(sideGlyph).not.toBe(rightGlyph)
    } finally {
      setSidePanelDock('right')
    }
  })
})
