import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

/**
 * "Open in <app>" falling back to the integrated terminal brings that terminal
 * tab to the front, which takes the Files tab out of view. The note saying
 * why must survive that switch, so the side panel holds it, not the Files tab.
 */

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="cli-panel" />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))
vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: () => <div data-testid="file-browser-tree" />,
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

/** Bound for every wait in this suite, so none relies on the 1,000 ms
 *  default. The Files tab opens from an effect. Its "Open in kitty" row sits
 *  behind a chained query: the desktop shell's detection
 *  (`externalTerminalAPI.detect`) must answer "capable" before the config
 *  query (`api.kirocrewConfig`) is enabled, and only that answer's picked app
 *  renders the row. A click on the row runs a second chain: the grant
 *  request, then the shell's launch, then the fallback note and the terminal
 *  tab; the note's Settings link then navigates. The default is a bet on
 *  runner speed; this ceiling turns a lost run into a named failure. */
const EXTERNAL_ROW_WAIT = { timeout: 5000 }

import SidePanel from '../pages/chat/SidePanel'
import { api } from '../api/client'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  const openView = tabsCtl.openView
  useEffect(() => { openView('files') }, [openView])
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      projectDir="/repo"
      onFileOpen={vi.fn()}
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
        <MemoryRouter initialEntries={['/chat']}>
          <Routes>
            <Route path="/chat" element={<Harness />} />
            <Route path="/settings/:tab/:sub" element={<div data-testid="settings-page" />} />
          </Routes>
        </MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('side panel: the external terminal fallback note', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    vi.spyOn(api, 'projectTree').mockResolvedValue({ root: '/repo', paths: [], repo: true })
    vi.spyOn(api, 'projectGitStatus').mockResolvedValue({ repo: true, files: [] })
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ profile: null, profiles: [] } as never)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { terminal: { external_app: 'kitty' } } } as never)
    ;(window as { externalTerminalAPI?: unknown }).externalTerminalAPI = {
      detect: vi.fn().mockResolvedValue({ apps: [{ id: 'kitty', label: 'kitty' }], capable: true }),
      open: vi.fn().mockResolvedValue({ ok: false, error: 'launch-failed' }),
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ grant: 'g'.repeat(43), app: 'kitty' }),
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (window as { externalTerminalAPI?: unknown }).externalTerminalAPI
  })

  it('stays on screen after the integrated terminal takes the Files tab out of view', async () => {
    renderPanel()
    const trigger = await screen.findByLabelText('Project actions', undefined, EXTERNAL_ROW_WAIT)
    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.click(await screen.findByText('Open in kitty (separate window)', undefined, EXTERNAL_ROW_WAIT))

    const note = await screen.findByTestId('side-panel-external-terminal-fallback', undefined, EXTERNAL_ROW_WAIT)
    expect(note).toHaveTextContent("kitty didn't start, so the built-in terminal opened in a new tab instead.")
    // The launch failed, so the note is the shared error surface even though a
    // terminal did open (errors-use-error-notice).
    expect(note).toHaveAttribute('role', 'alert')
    // This note sends the user nowhere, so it carries no Settings link.
    expect(screen.queryByTestId('side-panel-external-terminal-settings')).toBeNull()
    await waitFor(() => expect(screen.getByTestId('cli-panel')).toBeInTheDocument(), EXTERNAL_ROW_WAIT)
    // The Files tab stays mounted behind the terminal, out of view.
    expect(screen.getByLabelText('Project actions')).not.toBeVisible()

    fireEvent.click(within(note).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('side-panel-external-terminal-fallback')).toBeNull()
  })

  it('links a note that sends the user to Settings straight to the terminal app picker', async () => {
    ;(window as { externalTerminalAPI?: { open: ReturnType<typeof vi.fn> } }).externalTerminalAPI!.open
      .mockResolvedValue({ ok: false, error: 'no-app' })
    renderPanel()
    const trigger = await screen.findByLabelText('Project actions', undefined, EXTERNAL_ROW_WAIT)
    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.click(await screen.findByText('Open in kitty (separate window)', undefined, EXTERNAL_ROW_WAIT))

    const note = await screen.findByTestId('side-panel-external-terminal-fallback', undefined, EXTERNAL_ROW_WAIT)
    expect(note).toHaveTextContent('kitty is no longer installed on this computer')
    // The link is the notice's own action, inside the box, not loose text below it.
    const link = within(note).getByTestId('side-panel-external-terminal-settings')
    expect(link).toHaveTextContent('Open Terminal settings')
    expect(link).toHaveAttribute('href', '/settings/display/terminal?highlight=display.external-terminal-app')
    fireEvent.click(link)
    expect(await screen.findByTestId('settings-page', undefined, EXTERNAL_ROW_WAIT)).toBeInTheDocument()
  })
})
