/**
 * A contributed side-panel tab is BODY-OWNING: its kind must be intercepted
 * before the `VIEW_KINDS` branch and mounted through `AppHost`, never handed to
 * `ActivityViewer`.
 *
 * That is the half of the seam a resolver test cannot reach. `ActivityViewer`
 * multiplexes a CLOSED `ViewKind` union, so an `app:` kind that fell through to it
 * would render nothing at all — a tab that opens onto an empty panel, with no type
 * error and no console warning to say why. These tests pin the dispatch and the two
 * ways the tab is opened (the `+` menu and the empty-panel launcher).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

// Heavy bodies are not what this drives. `ActivityViewer` and `AppHost` render
// distinguishable markers so a test can say WHICH branch took the kind.
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => <div data-testid="activity-viewer" /> }))
vi.mock('../components/AppHost', () => ({
  default: ({ entry, active }: { entry?: string; active?: boolean }) => (
    <div data-testid="app-host" data-entry={entry} data-active={String(active)} />
  ),
}))
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

const DESCRIPTOR = {
  kind: 'app:pippin:browser' as const,
  appName: 'pippin',
  tabId: 'browser',
  title: 'Pippin',
  menuLabel: 'Open Pippin',
  menuDescription: 'Browse and sync docs',
  icon: 'BookOpen',
  entry: 'ui/panel.mjs',
}

// Only the descriptor hook is mocked; the real pure helpers (`panelTabKind`,
// `isPanelTabKind`, `panelTabDescriptor`) stay in play so the dispatch under test
// is the real one.
vi.mock('../hooks/panelTabRegistry', async (orig) => {
  const actual = await orig<typeof import('../hooks/panelTabRegistry')>()
  return { ...actual, usePanelTabDescriptors: () => [DESCRIPTOR] }
})

// `AppPanelTabBody` looks the app up in the shared ['apps'] query to hand `AppHost`
// its record, so the installed app has to exist for the body to mount.
vi.mock('../api/client', async (orig) => {
  const actual = await orig<typeof import('../api/client')>()
  return {
    ...actual,
    api: { ...actual.api, listApps: vi.fn(async () => [{ name: 'pippin', enabled: true, manifest: {} }]) },
  }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

function Harness({ emptyStrip = false }: { emptyStrip?: boolean } = {}) {
  const real = usePanelTabs('slot-a')
  // The empty-panel launcher only renders while the strip holds no tab, and
  // SidePanel's own effect pins Changes/Artifacts/Files on mount — so a harness
  // that wants to see the launcher has to keep that pin from happening. Neutering
  // `syncPinned` (rather than asserting against a strip that is never empty) is
  // what makes this a real test of the launcher surface.
  const tabsCtl = emptyStrip
    ? { ...real, syncPinned: (() => {}) as typeof real.syncPinned }
    : real
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      pins={[]}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel(opts: { emptyStrip?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness emptyStrip={opts.emptyStrip} />
      </Provider>
    </QueryClientProvider>,
  )
}

const appTabChip = () => screen.queryByRole('tab', { name: /Pippin/ })

describe('a contributed side-panel tab', () => {
  beforeEach(() => { __resetPanelTabs(); localStorage.clear() })

  it('is absent until opened, and the strip shows no app tab by default', () => {
    renderPanel()
    expect(appTabChip()).toBeNull()
    expect(screen.queryByTestId('app-host')).toBeNull()
  })

  it('opens from the + menu by its manifest menuLabel', async () => {
    renderPanel()
    act(() => {
      fireEvent.pointerDown(
        screen.getByRole('button', { name: 'Open side panel tab' }),
        { button: 0, ctrlKey: false, pointerType: 'mouse' },
      )
    })
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Open Pippin' })) })
    expect(appTabChip()).not.toBeNull()
  })

  it('mounts through AppHost with its entry, NOT through ActivityViewer', async () => {
    renderPanel()
    act(() => {
      fireEvent.pointerDown(
        screen.getByRole('button', { name: 'Open side panel tab' }),
        { button: 0, ctrlKey: false, pointerType: 'mouse' },
      )
    })
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Open Pippin' })) })
    const host = await screen.findByTestId('app-host')
    // The declared entry reached the ESM host, and the kind never reached the
    // closed ViewKind multiplexer.
    expect(host.getAttribute('data-entry')).toBe('ui/panel.mjs')
    expect(host.getAttribute('data-active')).toBe('true')
    expect(screen.queryByTestId('activity-viewer')).toBeNull()
  })

  it('opens from the empty-panel launcher, which also renders its description', () => {
    renderPanel({ emptyStrip: true })
    // The launcher is the empty panel's own surface, so it is present before any
    // tab is opened -- this is the discovery path the + menu does not cover.
    const card = screen.getByRole('button', { name: /Open Pippin/ })
    expect(screen.getByText('Browse and sync docs')).toBeTruthy()
    act(() => { fireEvent.click(card) })
    expect(appTabChip()).not.toBeNull()
  })

  it('dedupes to a single instance when opened twice', () => {
    renderPanel()
    const open = () => {
      act(() => {
        fireEvent.pointerDown(
          screen.getByRole('button', { name: 'Open side panel tab' }),
          { button: 0, ctrlKey: false, pointerType: 'mouse' },
        )
      })
      act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Open Pippin' })) })
    }
    open()
    open()
    expect(screen.getAllByRole('tab', { name: /Pippin/ })).toHaveLength(1)
  })
})
