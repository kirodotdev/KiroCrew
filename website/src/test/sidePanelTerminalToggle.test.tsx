import { useEffect, useSyncExternalStore, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { api } from '../api/client'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'
import {
  DEFAULT_PANEL_TOGGLE_BINDINGS,
  PANEL_TOGGLES_SKIPPING_SHELL,
  TERMINAL_PANEL_TOGGLE_IDS,
  TOGGLE_SIDE_TERMINAL_EVENT,
  matchPanelToggleEvent,
} from '../lib/panelToggleShortcuts'
import { useMessageSearch } from '../hooks/useMessageSearch'
import { useMessageActions } from '../pages/chat/page/messageActions'
import { useAppSelector } from '../store'
import { openActivityPanel } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import { createTestStore } from './helpers'

const killed = vi.hoisted(() => ({ deletes: [] as string[], disposes: [] as string[] }))
// The restored-terminal ruling, driven by the test: while pending, the panel
// hides every terminal tab.
const ruling = vi.hoisted(() => {
  const subs = new Set<() => void>()
  return { pending: false, subs, set(v: boolean) { this.pending = v; subs.forEach(cb => cb()) } }
})
vi.mock('../hooks/usePanelTabs', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/usePanelTabs')>()),
  usePanelTerminalsPending: () => useSyncExternalStore(
    cb => { ruling.subs.add(cb); return () => { ruling.subs.delete(cb) } }, () => ruling.pending),
}))

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/FilesHomePanel', () => ({ default: () => <div data-testid="gateway-files" /> }))
vi.mock('../pages/chat/RemoteFilesPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => <div data-testid="terminal-body" />,
  disposeTerminalSession: (id: string) => { killed.disposes.push(id) },
  useDeleteTerminalSession: () => ({ mutate: (id: string) => { killed.deletes.push(id) } }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import SidePanel from '../pages/chat/SidePanel'

const SLOT = 'side-terminal-slot'
let ctl: ReturnType<typeof usePanelTabs> | null = null
const clients: QueryClient[] = []

function Harness({ onClose }: { onClose: () => void }) {
  const controller = usePanelTabs(SLOT)
  ctl = controller
  const openView = controller.openView
  useEffect(() => { openView('files') }, [openView])
  return <SidePanel slot={SLOT} projectDir="/gateway/project" tabsCtl={controller} onClose={onClose} onFileSave={async () => {}} />
}

function mount() {
  const onClose = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  render(<QueryClientProvider client={client}><Provider store={createTestStore()}>
    <Harness onClose={onClose} />
  </Provider></QueryClientProvider>)
  return onClose
}

function mountMessageActions(activityOpen = false) {
  const store = createTestStore()
  if (activityOpen) store.dispatch(openActivityPanel())
  const messages: ChatMessage[] = []
  const base = {
    dispatch: store.dispatch, navigate: vi.fn(), filteredSlotsRef: { current: [] }, connected: true,
    activeSlot: SLOT, messages, messagesRef: { current: messages }, slotRunning: false,
    activeSlotRef: { current: SLOT }, activeSlotRemoteBound: false, showActionError: vi.fn(),
    handleFileOpen: vi.fn(), handleFolderOpen: vi.fn(), sessionTitles: new Map<string, string>(),
    selectSessionTab: vi.fn(), linkPreviewsOn: false, planTaskId: '', setSidebarPinned: vi.fn(),
    sidebarAutoHidden: { current: null }, sidebarWidth: 280, setRegenerating: vi.fn(),
  }
  const hook = renderHook(() => {
    ctl = usePanelTabs(SLOT)
    const search = useMessageSearch(messages, SLOT)
    const activityOpen = useAppSelector(s => s.chat.activityOpen)
    useMessageActions({ ...base, tabsCtl: ctl, search, activityOpen })
    return { search, activityOpen }
  }, { wrapper: ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider> })
  act(() => { ctl!.openView('files') })
  return hook
}

const press = () => act(() => { window.dispatchEvent(new Event(TOGGLE_SIDE_TERMINAL_EVENT)) })
const terminals = () => ctl!.tabs.filter(t => t.kind === 'terminal')
const activeKind = () => ctl!.tabs.find(t => t.id === ctl!.activeId)?.kind

beforeEach(() => {
  localStorage.clear(); __resetPanelTabs()
  killed.deletes = []; killed.disposes = []; ruling.pending = false
  vi.spyOn(api, 'listApps').mockResolvedValue([])
  vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ profile: null, profiles: [] })
})
afterEach(() => { cleanup(); clients.splice(0).forEach(c => c.clear()); vi.restoreAllMocks() })

describe('side-terminal shortcut', () => {
  it('ships with no default key, so nothing changes until a user binds it', () => {
    expect(DEFAULT_PANEL_TOGGLE_BINDINGS['side-terminal']).toBeNull()
    expect(matchPanelToggleEvent({ code: 'Backquote', key: '`', metaKey: false, ctrlKey: true, altKey: false, shiftKey: false }, {}, false))
      .toBe('terminal')
    expect(PANEL_TOGGLES_SKIPPING_SHELL.has('side-terminal')).toBe(true)
    expect(TERMINAL_PANEL_TOGGLE_IDS.has('side-terminal')).toBe(true)
  })

  it('first press shows a terminal; second press hides the panel and keeps the shell', async () => {
    const onClose = mount()
    await screen.findByTestId('gateway-files', {}, { timeout: 3000 })
    press()
    await waitFor(() => expect(activeKind()).toBe('terminal'))
    expect(terminals()).toHaveLength(1)
    const sessionId = terminals()[0].sessionId
    press()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(terminals().map(t => t.sessionId)).toEqual([sessionId])
    expect(killed.deletes).toEqual([])
    expect(killed.disposes).toEqual([])
  })

  it('focuses the existing terminal instead of opening a second one', async () => {
    mount()
    await screen.findByTestId('gateway-files', {}, { timeout: 3000 })
    press()
    await waitFor(() => expect(activeKind()).toBe('terminal'))
    act(() => { ctl!.setActive('files') })
    press()
    await waitFor(() => expect(activeKind()).toBe('terminal'))
    expect(terminals()).toHaveLength(1)
  })

  it.each([
    { name: 'closed panel with terminals', count: 2, pending: false, searchOpen: false },
    { name: 'closed panel without terminals', count: 0, pending: false, searchOpen: false },
    { name: 'closed panel while terminals restore', count: 1, pending: true, searchOpen: false },
    { name: 'closed panel with no terminals while restoring', count: 0, pending: true, searchOpen: false },
    { name: 'search-covered panel with terminals', count: 2, pending: false, searchOpen: true },
  ])('$name: reveals without creating a shell', ({ count, pending, searchOpen }) => {
    const { result } = mountMessageActions(searchOpen)
    act(() => {
      for (let i = 0; i < count; i++) ctl!.openTerminal({ cwd: '/gateway/project' })
      ctl!.setActive('files')
      if (searchOpen) result.current.search.open()
      ruling.set(pending)
    })
    const ids = terminals().map(t => t.id)
    expect(result.current.activityOpen).toBe(searchOpen)
    press()
    expect(result.current.search.isOpen).toBe(false)
    expect(result.current.activityOpen).toBe(true)
    expect(ctl!.activeId).toBe(!pending && count ? ids.at(-1) : 'files')
    expect(terminals().map(t => t.id)).toEqual(ids)
    act(() => { ruling.set(false) })
    expect(ctl!.activeId).toBe(!pending && count ? ids.at(-1) : 'files')
    expect(terminals().map(t => t.id)).toEqual(ids)
  })

  it('skips a withheld local terminal in a remote chat and reaches the workspace terminal', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      profile: 'dev', profiles: [{ id: 'dev', name: 'Verified workspace', provider: 'coder', workdir: '/workspace/app' }],
    })
    const onClose = mount()
    act(() => { ctl!.openTerminal({ cwd: '/gateway/project' }) })
    await screen.findByTestId('remote-gateway-terminal-withheld', {}, { timeout: 3000 })
    expect(activeKind()).toBe('terminal')
    press()
    await waitFor(() => expect(ctl!.tabs.find(t => t.id === ctl!.activeId)?.remoteSlot).toBe(SLOT))
    expect(onClose).not.toHaveBeenCalled()
    press()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('ignores a press while terminals are pending, without replaying it after restoration', async () => {
    mount()
    await screen.findByTestId('gateway-files', {}, { timeout: 3000 })
    press()
    await waitFor(() => expect(activeKind()).toBe('terminal'))
    const ids = terminals().map(t => t.id)
    act(() => { ctl!.setActive('files'); ruling.set(true) })
    press()
    expect(terminals().map(t => t.id)).toEqual(ids)
    expect(ctl!.activeId).toBe('files')
    act(() => { ruling.set(false) })
    expect(ctl!.activeId).toBe('files')
    expect(terminals().map(t => t.id)).toEqual(ids)
  })
})
