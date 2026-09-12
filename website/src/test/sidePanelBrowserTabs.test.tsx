/** Workspace tab navigation and caption clearance. Visual geometry is shared with terminal tabs. */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
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
import { WorkspaceFullscreenContext } from '../components/WorkspacePanelContext'

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

/** The panel inside the chat workspace, where fullscreen exists. */
function renderWorkspacePanel(fullscreen = false) {
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

const workspaceActions = () => document.querySelector('[data-panel-controls-host="workspace"]') as HTMLElement
const actionButtons = () => Array.from(workspaceActions().querySelectorAll(':scope > button'))

const source = (file: string) => readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', file), 'utf8')

describe('side panel tab strip', () => {
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
    // Its layout slot remains stable while the selection changes.
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }))
    const after = screen.getByTestId('strip-divider')
    expect(after.className).toContain('bg-transparent')
    expect(after.className).not.toContain('bg-border')
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

  it('keeps the shell first row above a fullscreen side panel and its controls', () => {
    const css = source('index.css')
    const app = source('App.tsx')
    const fullscreenRule = css.match(/\[data-workspace-fullscreen\] #activity-bar-slot\s*\{[^}]+\}/)?.[0]
    const fullscreenHostRule = css.match(/\[data-workspace-fullscreen\] #activity-bar-slot \[data-workspace-panel-host\]\s*\{[^}]+\}/)?.[0]
    const fixedHostRule = css.match(/\[data-workspace-fullscreen\] \[data-workspace-panel-host\]\.fixed\s*\{[^}]+\}/)?.[0]
    expect(fullscreenRule).toContain('grid-area: 2 / 1 / -1 / -1 !important')
    expect(fullscreenRule).not.toContain('grid-area: auto')
    expect(fullscreenHostRule).toContain('height: 100% !important')
    expect(fixedHostRule).toContain('height: auto !important')
    expect(fixedHostRule).not.toContain('top:')
    expect(app).toContain("style={{ gridArea: '2 / 1 / 3 / -1' }}")
    expect(app).not.toContain("panelFullscreen ? '1 / 1 / 2 / -1'")
  })
})

// Fullscreen is the workspace panel's own action, so it renders in the panel's
// action group rather than in the shell's fixed toggles. That group is capped
// at two controls (`max-two-buttons-per-row`): the ⋯ menu plus either the
// fullscreen button or the close X, never both.
describe('workspace fullscreen control placement', () => {
  it('right-docked: ⋯ + fullscreen button, and the X yields to the fixed toggles', () => {
    setSidePanelDock('right')
    const { controls } = renderWorkspacePanel()
    const buttons = actionButtons()
    expect(buttons).toHaveLength(2)
    const fullscreen = screen.getByTestId('workspace-fullscreen-toggle')
    expect(buttons[1]).toBe(fullscreen)
    expect(fullscreen).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByRole('button', { name: 'Close panel' })).not.toBeInTheDocument()
    fireEvent.click(fullscreen)
    expect(controls.toggle).toHaveBeenCalledOnce()
  })

  it('fullscreen: the same slot shows the exit control, pressed', () => {
    setSidePanelDock('right')
    renderWorkspacePanel(true)
    const fullscreen = screen.getByTestId('workspace-fullscreen-toggle')
    expect(fullscreen).toHaveAttribute('aria-pressed', 'true')
    expect(fullscreen).toHaveAccessibleName('Exit full screen')
    expect(actionButtons()).toHaveLength(2)
  })

  it('bottom-docked: the X keeps its slot and fullscreen enters from the ⋯ menu', async () => {
    setSidePanelDock('bottom')
    try {
      const { controls } = renderWorkspacePanel()
      expect(actionButtons()).toHaveLength(2)
      expect(screen.queryByTestId('workspace-fullscreen-toggle')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Close panel' })).toBeInTheDocument()
      fireEvent.pointerDown(screen.getByRole('button', { name: 'More options' }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
      const item = await screen.findByRole('menuitem', { name: 'Full screen' })
      fireEvent.click(item)
      expect(controls.toggle).toHaveBeenCalledOnce()
    } finally {
      setSidePanelDock('right')
    }
  })

  it('outside the chat workspace there is no fullscreen control and the X stays', () => {
    setSidePanelDock('right')
    renderPanel()
    expect(screen.queryByTestId('workspace-fullscreen-toggle')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close panel' })).toBeInTheDocument()
    expect(actionButtons()).toHaveLength(2)
  })
})
