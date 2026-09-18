import { useEffect } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
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
vi.mock('../pages/chat/FileBrowserRail', () => ({
  default: () => null,
  useTreeAvailable: () => true,
  useTreeState: () => 'ready',
}))
vi.mock('../components/MarkdownPanel', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    default: React.forwardRef(function MockMarkdownPanel() {
      return <div data-testid="markdown-panel" />
    }),
  }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'

type TabsCtl = ReturnType<typeof usePanelTabs>
let tabsCtl: TabsCtl | undefined

// A deferred `/api/file-read` fetch: each call records its url + abort signal
// and hands back a handle to settle it, so a test drives the tab's own
// hydration read exactly like the network would. Every OTHER fetch the panel
// makes (installed apps, tree, etc.) resolves to an inert empty payload so it
// cannot hang or pollute the recorded file-read calls.
interface FetchCall {
  url: string
  signal?: AbortSignal
  settle: (init: { ok: boolean; status?: number; text?: string; binary?: boolean }) => void
}
let fetchCalls: FetchCall[] = []

function response({ ok, status = ok ? 200 : 500, text = '', binary = false }: { ok: boolean; status?: number; text?: string; binary?: boolean }) {
  return {
    ok,
    status,
    headers: { get: (k: string) => (k === 'X-File-Binary' ? (binary ? 'true' : null) : null) },
    text: async () => text,
    json: async () => { try { return JSON.parse(text || 'null') } catch { return null } },
  } as unknown as Response
}

function installFetch() {
  globalThis.fetch = vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    const href = String(url)
    if (!href.includes('/api/file-read')) {
      // Inert stub for the panel's unrelated queries.
      return Promise.resolve(response({ ok: true, text: '[]' }))
    }
    let resolve!: (r: Response) => void
    const promise = new Promise<Response>((res) => { resolve = res })
    fetchCalls.push({
      url: href,
      signal: init?.signal ?? undefined,
      settle: (out) => resolve(response(out)),
    })
    return promise
  }) as never
}

function Harness() {
  tabsCtl = usePanelTabs('slot-a')
  const openFile = tabsCtl.openFile
  const patchTab = tabsCtl.patchTab
  useEffect(() => {
    openFile('/repo/README.md', '# README', 'slot-a')
    // Strip the buffer + type verdict so the tab is COLD, exactly as a
    // metadata-only persisted tab arrives after a reload.
    patchTab('file:/repo/README.md', {
      content: undefined,
      savedContent: undefined,
      binary: undefined,
    })
  }, [openFile, patchTab])
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

let queryClient: QueryClient | undefined

function renderPanel() {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('cold file tab hydration', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    tabsCtl = undefined
    queryClient = undefined
    fetchCalls = []
    installFetch()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('hydrates itself: reads the file and swaps the placeholder for the editor', async () => {
    renderPanel()

    // The tab hydrates itself — the skeleton is up and the read has fired.
    expect(await screen.findByTestId('file-tab-hydrating')).toBeInTheDocument()
    expect(screen.queryByTestId('markdown-panel')).toBeNull()
    await waitFor(() => expect(fetchCalls.length).toBe(1))
    expect(fetchCalls[0].url).toContain('/api/file-read')

    act(() => fetchCalls[0].settle({ ok: true, text: '# README' }))

    expect(await screen.findByTestId('markdown-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('file-tab-hydrating')).toBeNull()
  })

  it('shows the failure instead of an endless skeleton when the read fails', async () => {
    renderPanel()

    await waitFor(() => expect(fetchCalls.length).toBe(1))
    act(() => fetchCalls[0].settle({ ok: false, status: 500 }))

    const failed = await screen.findByTestId('file-tab-hydration-failed')
    // A human sentence naming the file, with the status as its detail -- not a
    // bare status code.
    expect(failed).toHaveTextContent(/Could not read \/repo\/README\.md/)
    expect(failed).toHaveTextContent(/HTTP 500/)
    expect(screen.getByTestId('file-tab-hydration-error')).toBeInTheDocument()
    expect(screen.queryByTestId('file-tab-hydrating')).toBeNull()
    expect(screen.queryByTestId('markdown-panel')).toBeNull()
  })

  it('opens on the not-found placeholder when the read 404s', async () => {
    renderPanel()

    await waitFor(() => expect(fetchCalls.length).toBe(1))
    act(() => fetchCalls[0].settle({ ok: false, status: 404 }))

    expect(await screen.findByTestId('markdown-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('file-tab-hydrating')).toBeNull()
    expect(screen.queryByTestId('file-tab-hydration-failed')).toBeNull()
    const tab = tabsCtl?.tabs.find(t => t.id === 'file:/repo/README.md')
    expect(tab?.content).toContain('File not found on disk')
  })

  it('reads through the shared cache entry, so the page does not fetch the file twice', async () => {
    renderPanel()

    await waitFor(() => expect(fetchCalls.length).toBe(1))
    act(() => fetchCalls[0].settle({ ok: true, text: '# README', binary: false }))
    expect(await screen.findByTestId('markdown-panel')).toBeInTheDocument()

    // The answer lives under the key ChatPage's cold-tab query and a chip click
    // use, so a second consumer inside the freshness window is served from it:
    // one restored tab, one GET.
    const cached = queryClient?.getQueryData(['file-read', '/repo/README.md'])
    expect(cached).toEqual({ text: '# README', ok: true, status: 200, binary: false })
    const before = fetchCalls.length
    await queryClient?.fetchQuery({
      queryKey: ['file-read', '/repo/README.md'],
      queryFn: async () => { throw new Error('must not refetch') },
      staleTime: 10_000,
    })
    expect(fetchCalls.length).toBe(before)
  })

  it('applies nothing after unmount, and leaves the shared read alone', async () => {
    const { unmount } = renderPanel()

    await waitFor(() => expect(fetchCalls.length).toBe(1))
    expect(() => unmount()).not.toThrow()

    // The read belongs to the shared cache entry, not to this tab, so unmounting
    // does not cancel it -- another consumer of the same path may still want the
    // answer. What unmounting does is withdraw THIS consumer: the answer that
    // lands afterwards is applied to no tab.
    const tabBefore = tabsCtl?.tabs.find(t => t.id === 'file:/repo/README.md')
    expect(tabBefore?.content).toBeUndefined()
    act(() => fetchCalls[0].settle({ ok: true, text: '# README' }))
    await waitFor(() => expect(queryClient?.getQueryData(['file-read', '/repo/README.md'])).toBeTruthy())
    const tabAfter = tabsCtl?.tabs.find(t => t.id === 'file:/repo/README.md')
    expect(tabAfter?.content).toBeUndefined()
  })
})
