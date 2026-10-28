/**
 * Gap 8 (frontend): the side panel surfaces a Kiro Agent workflow that needs
 * the user — on the Workflows TAB CHIP and on the pick-a-panel GRID entry, both
 * of which previously carried no such signal.
 *
 * `useKasSidebarActivity` is mocked to return an attention summary, so the test
 * drives the badge decision directly without an engine probe / state poll.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
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

// The one thing under test: this chat's workflow needs a person.
vi.mock('../apps/workflows/kas/useKasWorkflows', async (orig) => ({
  ...(await orig<typeof import('../apps/workflows/kas/useKasWorkflows')>()),
  useKasSidebarActivity: vi.fn(() => ({ count: 1, name: 'My Run', phase: '', attention: true })),
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

let panelController: ReturnType<typeof usePanelTabs>

function Harness({ hiddenViews }: { hiddenViews?: ReadonlySet<string> }) {
  const tabsCtl = usePanelTabs('slot-a')
  panelController = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      hiddenViews={hiddenViews as never}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel(hiddenViews?: ReadonlySet<string>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness hiddenViews={hiddenViews} />
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => { localStorage.clear(); __resetPanelTabs() })
afterEach(() => vi.clearAllMocks())

describe('side panel — a workflow needs you (gap 8)', () => {
  it('badges the Workflows tab chip when a run needs attention', async () => {
    renderPanel()
    act(() => { panelController.openView('workflows') })
    expect(screen.getByTestId('side-tab-workflow-needs-you')).toBeTruthy()
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  })

  it('badges the Workflows entry on the pick-a-panel grid', async () => {
    // Withhold every view EXCEPT Workflows, so the panel has no visible tab and
    // shows its empty-state launcher grid — while the Workflows entry itself
    // stays in the grid (it is not withheld).
    const allButWorkflows = [
      'command-center', 'changes', 'issues', 'links', 'pins', 'files', 'artifacts',
      'subagents', 'logs', 'crewlog', 'context', 'side', 'browser', 'git', 'summary',
      'terminal', 'app',
    ]
    renderPanel(new Set(allButWorkflows))
    expect(screen.getByTestId('side-grid-workflow-badge')).toBeTruthy()
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  })
})
