/**
 * ChatSidebar → an opened fork's folder path is opened.
 *
 * A fork child inherits its source's folder. That folder can be collapsed, or
 * sit inside a collapsed parent, while the user is in the source: they opened
 * it from the command bar, a tab or a link, or another tab collapsed the
 * folder while the fork ran. The fork then becomes the active session with its
 * row hidden. These pin that "Fork conversation from here" and the row's
 * "Fork chat" open every collapsed folder on the child's path, keep the user's
 * search, and use the folder the fork response names even when the sidebar's
 * copy of the row is not filed yet; that a fork made while the mobile drawer is
 * closed still opens the path when the drawer first mounts the sidebar before
 * the folder query answers, or after that query first fails and a retry
 * succeeds; that a folder made after the last folder fetch opens once a
 * refetch lists it, and the request is dropped only when a refetch made after
 * it still lacks the folder; and that a refused fork, or a child at the top
 * level, leaves the folders alone.
 *
 * The real `forkSlot` thunk, `useForkSession` and `useSessionActions` run
 * against a mocked `api.forkChatSlot`, and the sidebar reads its slot list
 * from the store they write, as ChatPage does.
 */
import { useState } from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { requestSlotFolderOpen } from '../store/chatSlice'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { useAppDispatch, useAppSelector, type RootState } from '../store'
import { addSlotOptimistic } from '../store/dashboardSlice'
import { useForkSession } from '../pages/useChatPageSlotSwitches'
import type { ChatSlot, ChatFolder } from '../types'

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  forkChatSlot: vi.fn(), chatFolders: vi.fn(), updateChatFolder: vi.fn(), dashboardConfig: vi.fn(),
}))
const showActionError = vi.fn()
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const SOURCE = 's1'
const CHILD = 'chat-fork-1'
// A fixed future instant keeps the rows out of the stale-session collapse
// without reading the wall clock.
const row = (key: string, title: string, folder_id?: string) =>
  ({ key, title, messages: 2, running: false, mode: '', created: '', last_ts: '2099-01-01T00:00:00Z', ...(folder_id ? { folder_id } : {}) }) as ChatSlot
const folder = (id: string, collapsed: boolean, parent_id: string | null = null) =>
  ({ id, name: `Folder ${id}`, parent_id, collapsed }) as unknown as ChatFolder

/** "Fork conversation from here" on the active session's first message,
 *  through the page's own fork hook. */
function MessageFork() {
  const dispatch = useAppDispatch()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const { handleFork } = useForkSession({ activeSlot, dispatch, showActionError })
  return <button type="button" onClick={() => { void handleFork(0) }}>fork from message</button>
}

/** The mobile drawer: the sidebar is unmounted until the drawer opens. */
function Drawer({ startOpen }: { startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>open drawer</button>
      <button type="button" onClick={() => setOpen(false)}>close drawer</button>
      {open && <StoreBackedSidebar />}
    </>
  )
}

/** Passes the store's slot list down, so the fork's row reaches the sidebar
 *  in the same commit as the activation, as on the chat page. */
function StoreBackedSidebar() {
  const slots = useAppSelector(s => s.dashboard.slots)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  return (
    <ChatSidebar
      slots={slots} activeSlot={activeSlot} unreadSlots={[]}
      history={[]} historyHasMore={false} defaultAgent="kirocrew" installedAgents={[]}
    />
  )
}

/** `coldFolders`: the folder query has no cached data and its fetch waits on
 *  the returned `resolveFolders`, as on a first visit. `drawerClosed`: the
 *  sidebar mounts only on `openDrawer()`. */
function renderPage(folders: ChatFolder[], sourceFolder: string, { coldFolders = false, drawerClosed = false } = {}) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' }, connected: true, slotsLoaded: true,
      slots: [row(SOURCE, 'Source chat', sourceFolder), row('s2', 'Other chat')],
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: SOURCE, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0, pendingInput: null, slotContextPct: {},
      voicePlaying: false, voiceAudio: null, subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  let resolveFolders: (v: ChatFolder[]) => void = () => {}
  let rejectFolders: (e: Error) => void = () => {}
  if (coldFolders) {
    mocks.chatFolders.mockReturnValueOnce(new Promise<ChatFolder[]>((res, rej) => { resolveFolders = res; rejectFolders = rej }))
  } else {
    qc.setQueryData(['chat-folders'], folders)
    mocks.chatFolders.mockResolvedValue(folders)
  }
  qc.setQueryData(['dashboardConfig'], {})
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <MessageFork />
            <Drawer startOpen={!drawerClosed} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  const openDrawer = () => fireEvent.click(screen.getByRole('button', { name: 'open drawer' }))
  return { store, qc, resolveFolders: (v: ChatFolder[]) => resolveFolders(v), rejectFolders: (e: Error) => rejectFolders(e), openDrawer }
}

const messageFork = () => fireEvent.click(screen.getByRole('button', { name: 'fork from message' }))
const sourceRowFork = () => {
  const el = document.querySelector(`[data-session-row="${SOURCE}"]`) as HTMLElement
  const btn = Array.from(el.querySelectorAll('button')).find(b => b.getAttribute('aria-label') === 'Fork chat')
  if (!btn) throw new Error('source row has no Fork chat button')
  fireEvent.click(btn)
}
const expandedIds = () => mocks.updateChatFolder.mock.calls
  .filter(([, body]) => (body as { collapsed?: boolean })?.collapsed === false)
  .map(([id]) => id)
const forked = (folder_id?: string) => ({ ok: true, key: CHILD, title: 'Fork of Source chat', messages: 2, ...(folder_id ? { folder_id } : {}) })

describe('ChatSidebar – an opened fork shows its row', () => {
  beforeEach(() => {
    localStorage.clear()
    showActionError.mockReset()
    mocks.forkChatSlot.mockReset()
    mocks.chatFolders.mockReset().mockResolvedValue([])
    mocks.updateChatFolder.mockReset().mockResolvedValue({})
    mocks.dashboardConfig.mockReset().mockResolvedValue({})
  })

  it('a message fork into a collapsed folder expands that folder', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fD'))
    const { store } = renderPage([folder('fD', true)], 'fD')
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
  })

  it('a message fork into an expanded folder inside a collapsed parent expands the parent', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fC'))
    const { store } = renderPage([folder('fP', true), folder('fC', false, 'fP')], 'fC')
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    await waitFor(() => expect(expandedIds()).toEqual(['fP']))
  })

  it('a message fork into a collapsed chain expands every collapsed folder on it', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fC'))
    const { store } = renderPage([folder('fP', true), folder('fC', true, 'fP')], 'fC')
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    await waitFor(() => expect(expandedIds().sort()).toEqual(['fC', 'fP']))
  })

  it('keeps the user\'s search while it opens the folder', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fD'))
    const { store } = renderPage([folder('fD', true)], 'fD')
    const search = screen.getByPlaceholderText('Search sessions…') as HTMLInputElement
    await waitFor(() => expect(screen.queryByText('Other chat')).not.toBeNull())
    fireEvent.change(search, { target: { value: 'Source' } })
    // Precondition: the search is in effect and drops rows that do not match.
    await waitFor(() => expect(screen.queryByText('Other chat')).toBeNull())
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
    expect(search.value).toBe('Source')
  })

  it('opens the folder the fork response names when the listed row is not filed yet', async () => {
    // A slots frame older than the fork's filing can list the child with no
    // folder, and listing the fork does not overwrite an existing row.
    let resolve: (v: unknown) => void = () => {}
    mocks.forkChatSlot.mockReturnValue(new Promise(r => { resolve = r }))
    const { store } = renderPage([folder('fD', true)], 'fD')
    messageFork()
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledTimes(1))
    act(() => { store.dispatch(addSlotOptimistic(row(CHILD, 'Fork of Source chat'))) })
    await act(async () => { resolve(forked('fD')) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    expect(store.getState().dashboard.slots.find(s => s.key === CHILD)?.folder_id).toBeUndefined()
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
  })

  it('the row\'s Fork chat expands a folder collapsed while the fork ran', async () => {
    let resolve: (v: unknown) => void = () => {}
    mocks.forkChatSlot.mockReturnValue(new Promise(r => { resolve = r }))
    const { store, qc } = renderPage([folder('fD', false)], 'fD')
    sourceRowFork()
    await waitFor(() => expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE))
    // Another tab collapses the folder; its folders refresh reaches this tab.
    qc.setQueryData(['chat-folders'], [folder('fD', true)])
    resolve(forked('fD'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
  })

  // The mounted sidebar clears a request as soon as it consumes it, so these
  // two read `revealNonce`, which only a raised request moves.
  it('a refused fork asks for no reveal', async () => {
    mocks.forkChatSlot.mockResolvedValue({ ok: false, error: 'nope' })
    const { store } = renderPage([folder('fD', true)], 'fD')
    const nonce = store.getState().chat.revealNonce
    messageFork()
    // The failure notice is the fork's last step, so the thunk has settled.
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
    expect(store.getState().chat.activeSlot).toBe(SOURCE)
    expect(store.getState().chat.revealNonce).toBe(nonce)
    expect(expandedIds()).toEqual([])
  })

  it('a fork filed at the top level asks for no reveal', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked())
    const { store } = renderPage([folder('fD', true)], 'fD')
    const nonce = store.getState().chat.revealNonce
    messageFork()
    // The request would be dispatched right after the switch that makes the
    // child active, in the same synchronous step.
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    expect(store.getState().chat.revealNonce).toBe(nonce)
    expect(expandedIds()).toEqual([])
  })

  it('a fork made with the drawer closed opens the folder once the first folder fetch lands', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fD'))
    const { store, resolveFolders, openDrawer } = renderPage([], 'fD', { coldFolders: true, drawerClosed: true })
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    expect(store.getState().chat.revealRequest).toMatchObject({ target: CHILD, folderId: 'fD' })
    // The drawer opens before the folder query has answered: the sidebar sees
    // no folders yet, so it must keep the request rather than drop it.
    openDrawer()
    await waitFor(() => expect(mocks.chatFolders).toHaveBeenCalled())
    expect(store.getState().chat.revealRequest).toMatchObject({ target: CHILD, folderId: 'fD' })
    expect(expandedIds()).toEqual([])
    await act(async () => { resolveFolders([folder('fD', true)]) })
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
    expect(store.getState().chat.revealRequest).toBeNull()
  })

  it('drops the request once the first folder fetch lands without the folder', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fD'))
    const { store, resolveFolders, openDrawer } = renderPage([], 'fD', { coldFolders: true, drawerClosed: true })
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    openDrawer()
    await waitFor(() => expect(mocks.chatFolders).toHaveBeenCalled())
    expect(store.getState().chat.revealRequest).not.toBeNull()
    // The folder was deleted before the list loaded.
    await act(async () => { resolveFolders([folder('fOther', true)]) })
    await waitFor(() => expect(store.getState().chat.revealRequest).toBeNull())
    expect(expandedIds()).toEqual([])
  })

  it('opens a folder made after the last folder fetch once a refetch lists it', async () => {
    // An import from a new sender makes `from <sender>` under a collapsed
    // Imported, and the settled list learns of it only from a later fetch.
    mocks.forkChatSlot.mockResolvedValue(forked('fNew'))
    const { store, qc } = renderPage([folder('fP', true)], 'fP')
    await waitFor(() => expect(qc.getQueryState(['chat-folders'])?.fetchStatus).toBe('idle'))
    const fetches = mocks.chatFolders.mock.calls.length
    mocks.chatFolders.mockResolvedValue([folder('fP', true), folder('fNew', false, 'fP')])
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    await waitFor(() => expect(expandedIds()).toEqual(['fP']))
    // The refetch the reveal asked for; the expand's own settle adds another.
    expect(mocks.chatFolders.mock.calls.length).toBeGreaterThan(fetches)
    expect(store.getState().chat.revealRequest).toBeNull()
  })

  it('keeps the request through an optimistic folder write while its folder read is pending', async () => {
    // A folder toggle mid-read cancels the folder query and writes the cache
    // optimistically. That write is not a server answer, so it must not drop
    // the request; only the read the reveal made can.
    mocks.forkChatSlot.mockResolvedValue(forked('fNew'))
    const { store, qc } = renderPage([folder('fP', true)], 'fP')
    await waitFor(() => expect(qc.getQueryState(['chat-folders'])?.fetchStatus).toBe('idle'))
    const fetches = mocks.chatFolders.mock.calls.length
    let answer: (v: ChatFolder[]) => void = () => {}
    mocks.chatFolders.mockReturnValueOnce(new Promise<ChatFolder[]>(res => { answer = res }))
    messageFork()
    await waitFor(() => expect(mocks.chatFolders.mock.calls.length).toBe(fetches + 1))
    await act(async () => {
      await qc.cancelQueries({ queryKey: ['chat-folders'] })
      qc.setQueryData<ChatFolder[]>(['chat-folders'], [folder('fP', true)])
    })
    expect(store.getState().chat.revealRequest).not.toBeNull()
    expect(expandedIds()).toEqual([])
    const listed = [folder('fP', true), folder('fNew', false, 'fP')]
    mocks.chatFolders.mockResolvedValue(listed)
    await act(async () => { answer(listed) })
    await waitFor(() => expect(expandedIds()).toEqual(['fP']))
    expect(store.getState().chat.revealRequest).toBeNull()
  })

  it('a folder read that answers after the sidebar unmounts leaves a newer request alone', async () => {
    // The drawer closes while the reveal's read for a deleted folder is still
    // out, and a newer folder-open request lands before that read answers.
    mocks.forkChatSlot.mockResolvedValue(forked('fGone'))
    const { store, qc } = renderPage([folder('fP', true)], 'fP')
    await waitFor(() => expect(qc.getQueryState(['chat-folders'])?.fetchStatus).toBe('idle'))
    const fetches = mocks.chatFolders.mock.calls.length
    let answer: (v: ChatFolder[]) => void = () => {}
    mocks.chatFolders.mockReturnValueOnce(new Promise<ChatFolder[]>(res => { answer = res }))
    messageFork()
    await waitFor(() => expect(mocks.chatFolders.mock.calls.length).toBe(fetches + 1))
    fireEvent.click(screen.getByRole('button', { name: 'close drawer' }))
    act(() => { store.dispatch(requestSlotFolderOpen({ key: CHILD, folder_id: 'fP' })) })
    const newer = store.getState().chat.revealRequest
    expect(newer).not.toBeNull()
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(store.getState().chat.revealRequest).toBe(newer)
    await act(async () => { answer([folder('fP', true)]) })
    expect(store.getState().chat.revealRequest).toBe(newer)
  })

  it('drops the request once a refetch made after it still lacks the folder', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fGone'))
    const { store, qc } = renderPage([folder('fP', true)], 'fP')
    await waitFor(() => expect(qc.getQueryState(['chat-folders'])?.fetchStatus).toBe('idle'))
    const fetches = mocks.chatFolders.mock.calls.length
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    await waitFor(() => expect(store.getState().chat.revealRequest).toBeNull())
    expect(mocks.chatFolders.mock.calls.length).toBe(fetches + 1)
    expect(expandedIds()).toEqual([])
  })

  it('keeps the request through a failed first folder fetch and opens the folder on the retry', async () => {
    mocks.forkChatSlot.mockResolvedValue(forked('fD'))
    const { store, qc, rejectFolders, openDrawer } = renderPage([], 'fD', { coldFolders: true, drawerClosed: true })
    messageFork()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(CHILD))
    openDrawer()
    await waitFor(() => expect(mocks.chatFolders).toHaveBeenCalledTimes(1))
    // The first fetch fails, for example while the gateway restarts.
    await act(async () => { rejectFolders(new Error('gateway restarting')) })
    await waitFor(() => expect(qc.getQueryState(['chat-folders'])?.status).toBe('error'))
    // A failed fetch does not show the folder is gone.
    expect(store.getState().chat.revealRequest).toMatchObject({ target: CHILD, folderId: 'fD' })
    expect(expandedIds()).toEqual([])
    mocks.chatFolders.mockResolvedValue([folder('fD', true)])
    await act(async () => { await qc.refetchQueries({ queryKey: ['chat-folders'] }) })
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
    expect(store.getState().chat.revealRequest).toBeNull()
  })
})
