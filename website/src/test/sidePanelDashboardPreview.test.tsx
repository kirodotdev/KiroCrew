import { useEffect } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

/* An artifact tab whose reference names a crewmate's staged dashboard renders the
 * staged page beside the chat; any other artifact tab is still the artifact. */
const previewFails = { on: false }
vi.mock('../pages/chat/DashboardPreviewPanel', () => ({
  default: ({ target }: { target: { kind: string; slug?: string; slot?: string } }) => {
    const slug = target.slug
    if (previewFails.on) throw new Error('chunk load failed')
    return <div data-testid="dashboard-preview-stub" data-slug={slug} />
  },
}))
vi.mock('../components/ArtifactPanel', () => ({
  default: ({ slug }: { slug: string }) => <div data-testid="artifact-panel-stub" data-slug={slug} />,
}))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
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
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import { dashboardPreviewRef } from '../utils/dashboardPreview'

/** The body sits behind React.lazy: the wait covers the dynamic import, then the
 *  roster read, then the dashboard read. A loaded runner can take past the 1s default. */
const LAZY = { timeout: 5000 }

function Harness({ slug }: { slug: string }) {
  const tabsCtl = usePanelTabs('slot-a')
  const openArtifact = tabsCtl.openArtifact
  useEffect(() => { openArtifact({ slug, kind: 'html', title: 'atlas (preview)' }, '', 'slot-a') }, [openArtifact, slug])
  return <SidePanel tabsCtl={tabsCtl} slot="slot-a" projectDir="/repo" onFileOpen={vi.fn()} onFileSave={async () => {}} onClose={() => {}} />
}

function renderPanel(slug: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness slug={slug} />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('a staged-dashboard tab in the side panel', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
  })

  it('renders the staged page, not the artifact viewer', async () => {
    renderPanel(dashboardPreviewRef('atlas'))
    expect((await screen.findByTestId('dashboard-preview-stub', undefined, LAZY)).dataset.slug).toBe('atlas')
    expect(screen.queryByTestId('artifact-panel-stub')).toBeNull()
  })

  it('contains a failed preview body inside the panel, so the host page stays mounted', async () => {
    previewFails.on = true
    vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      renderPanel(dashboardPreviewRef('atlas'))
      expect(await screen.findByRole('button', { name: /retry|try again/i }, LAZY)).toBeInTheDocument()
    } finally {
      previewFails.on = false
    }
  })

  it('leaves an ordinary artifact tab to the artifact viewer', async () => {
    renderPanel('my-report')
    expect((await screen.findByTestId('artifact-panel-stub', undefined, LAZY)).dataset.slug).toBe('my-report')
    expect(screen.queryByTestId('dashboard-preview-stub')).toBeNull()
  })
})
