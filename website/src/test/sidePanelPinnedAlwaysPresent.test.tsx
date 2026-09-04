/**
 * The pinned block (Changes / Artifacts / Files) is PERMANENT, not content-driven.
 *
 * This exists because the contract is easy to read backwards. `syncPinned` is
 * parameterised and its removal arm is genuinely reachable through its argument,
 * so the hook on its own looks content-gated. It is not: the ONE production
 * caller -- `SidePanel`'s `syncPinned(PINNED_VIEWS)`, unconditional in an effect
 * keyed only on the callback -- always passes the whole list, so no pinned view
 * is ever removed. A comment can state that and still be read the other way; a
 * test cannot.
 *
 * Rendered with NO content of any kind -- no pins, no sources, no issues, no
 * artifacts -- which is the exact state a content-gated strip would leave empty.
 * So a regression to content-driven pinning fails here.
 *
 * `ChatPage.persist.integration.test.tsx` already pins `changes` surviving an
 * empty source set, but through a different mechanism (a source-reconcile effect
 * that must not auto-close it) and for one of the three. This covers all three
 * at the render level, which is the layer the contract is about.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

// Heavy tab bodies are not what this drives -- only the strip's composition.
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
import { PINNED_VIEWS, usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

/** The strip label each pinned kind renders with, in English. */
const LABEL: Record<string, string> = { changes: 'Changes', artifacts: 'Artifacts', files: 'Files' }

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderEmptyPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

/** Every tab chip, addressed by role rather than by any label this test asserts. */
const chips = () => screen.getAllByRole('tab')
/** A chip's accessible name -- pinned chips are icon-only, so it is the aria-label. */
const nameOf = (el: HTMLElement) => el.getAttribute('aria-label')

describe('the pinned block is permanent, not content-driven', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('declares the block as three views in a fixed order', () => {
    // The declaration IS the contract the strip renders from ("Order here =
    // strip order"), so a silent reorder or a fourth member is loud here before
    // it reaches the DOM assertions below.
    expect(PINNED_VIEWS).toEqual(['changes', 'artifacts', 'files'])
  })

  it('shows all three on a session with NO content, and nothing else', () => {
    renderEmptyPanel()
    // Cardinality first: exactly three chips. Without this the name assertions
    // below would pass just as well on a strip that had gained a stray tab, and
    // "all three are present" would stop meaning "and only those three".
    const rendered = chips()
    expect(rendered).toHaveLength(3)
    expect(rendered.map(nameOf)).toEqual([LABEL.changes, LABEL.artifacts, LABEL.files])
  })

  it('renders them in the declared order, pinned ahead of the dynamic area', () => {
    renderEmptyPanel()
    // Read the order out of the DOM and compare it to the declaration, rather
    // than looking each chip up by the name being asserted -- a per-name lookup
    // would pass on a shuffled strip.
    expect(chips().map(nameOf)).toEqual(PINNED_VIEWS.map(k => LABEL[k]))
  })

  it('gives none of them a close control', () => {
    renderEmptyPanel()
    // Located by identity (the chip), asserted on structure (no nested button),
    // so this does not depend on the close control's label string.
    for (const chip of chips()) {
      expect(chip.querySelectorAll('button')).toHaveLength(0)
    }
  })
})
