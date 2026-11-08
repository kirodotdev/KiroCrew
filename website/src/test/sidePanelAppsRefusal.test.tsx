/**
 * #32031: the side panel reports a failed app list at its top edge, so a reader
 * knows why no contributed tab appears. A relay teammate is refused that list
 * (403 `relay_apps_teammate`) and has no contributed tabs; that is the answer,
 * not a failure, so no notice shows. A real failure still does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ApiError } from '../api/apiError'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/AppHost', () => ({ default: () => null }))
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
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const listApps = vi.hoisted(() => vi.fn())
vi.mock('../api/client', async (orig) => {
  const actual = await orig<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, listApps } }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  return <SidePanel tabsCtl={tabsCtl} slot="slot-a" pins={[]} onFileSave={async () => {}} onClose={() => {}} />
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
  return qc
}

const relayRefusal = () => new ApiError(403, "Apps aren't available to teammates through the relay.",
  JSON.stringify({ error: "Apps aren't available to teammates through the relay.", code: 'relay_apps_teammate' }))

describe('side panel app-list notice', () => {
  beforeEach(() => { __resetPanelTabs(); localStorage.clear(); listApps.mockReset() })

  it('shows nothing when the relay refuses the app list to a teammate', async () => {
    listApps.mockRejectedValue(relayRefusal())
    const qc = renderPanel()
    await waitFor(() => expect(qc.getQueryState(['apps'])?.status).toBe('error'))
    expect(screen.queryByText(/Apps aren't available to teammates/)).toBeNull()
  })

  it('still reports a real failure of the app list', async () => {
    listApps.mockRejectedValue(new ApiError(500, 'apps scan failed', JSON.stringify({ error: 'apps scan failed' })))
    renderPanel()
    expect(await screen.findByText(/apps scan failed/)).toBeTruthy()
  })
})
