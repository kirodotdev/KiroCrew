/**
 * The per-chat size change must not animate for a reader who asked for reduced
 * motion.
 *
 * The panel eases its width between two chats' remembered sizes on the same
 * curve the dock wrapper opens with, so the edge travels on its own without any
 * pointer input. framer-motion does not consult `prefers-reduced-motion` on our
 * behalf for a plain CSS transition, so `SidePanel` reads the preference itself
 * and omits the transition: the width still changes, it just arrives
 * immediately.
 *
 * This lives in its own file because framer-motion resolves the preference ONCE
 * into module state that a test-local reset does not clear, so the honored case
 * needs a module registry where the stub was in place before the import. The
 * unreduced case is covered in sidePanelWidthPerChat.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef } from 'react'
import { render, screen, act, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: forwardRef(() => null) }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => <div data-testid="web-preview-body" /> }))
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
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

/** Report the reduced-motion preference as set; every other query is false. */
function stubReducedMotion(): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('prefers-reduced-motion'),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

describe('SidePanel per-chat size, reduced motion honored', () => {
  beforeEach(() => {
    localStorage.clear()
    window.innerWidth = 1600
  })
  afterEach(() => { cleanup(); vi.unstubAllGlobals() })

  it('changes the width with no transition when motion is reduced', async () => {
    stubReducedMotion()
    // Imported after the stub: see the file header.
    const { default: SidePanel } = await import('../pages/chat/SidePanel')
    const { usePanelTabs, __resetPanelTabs } = await import('../hooks/usePanelTabs')
    const { SIDE_PANEL_WIDTH_KEY, sidePanelDimKey } = await import('../pages/chat/sidePanelWidth')
    __resetPanelTabs()

    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-a'), '380')
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'chat-b'), '900')

    let ctl: ReturnType<typeof usePanelTabs> | null = null
    function Harness({ slot }: { slot: string }) {
      const tabsCtl = usePanelTabs(slot)
      ctl = tabsCtl
      return <SidePanel tabsCtl={tabsCtl} slot={slot} onFileSave={async () => {}} onClose={() => {}} canDockBottom={false} />
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    const store = createTestStore()
    const tree = (slot: string) => (
      <QueryClientProvider client={queryClient}>
        <Provider store={store}><Harness slot={slot} /></Provider>
      </QueryClientProvider>
    )
    const { rerender } = render(tree('chat-a'))

    const root = () => screen.getByTestId('side-panel-root')
    act(() => { ctl!.openView('git') })
    expect(root().style.width).toBe('380px')
    expect(root().style.transition).toBe('')
    // The behavior itself is unaffected, only the easing is withheld.
    act(() => { rerender(tree('chat-b')) })
    expect(root().style.width).toBe('900px')
    expect(root().style.transition).toBe('')
  })
})
