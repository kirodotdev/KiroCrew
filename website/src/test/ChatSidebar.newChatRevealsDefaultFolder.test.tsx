/**
 * ChatSidebar → a chat created into the default folder is revealed.
 *
 * Header New, the caret menu's New incognito / New temporary rows and every
 * other plain New entry point send `default_folder: true` and learn the folder
 * only from the create response. The folder "+" path expands the destination's
 * collapsed ancestors before it creates; the default-folder paths cannot,
 * because they learn the folder only from the response, so the new chat must
 * be revealed after it. These pin that an activating default-folder create opens every
 * collapsed folder on the row's path, and that a create the user switched away
 * from, or one with no folder, leaves the sidebar alone.
 *
 * The real `createSlot` thunk runs against a mocked `api.createChatSlot`, and
 * the sidebar reads its slot list from the store the thunk writes, as
 * ChatPage does.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { useAppSelector, type RootState } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import type { ChatSlot, ChatFolder } from '../types'

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), chatFolders: vi.fn(), updateChatFolder: vi.fn() }))
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

const ORIGIN = 's1'
const NEW_KEY = 'chat-new-1'
const SLOTS = [
  { key: ORIGIN, title: 'Session 1', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
  // A fixed future instant keeps it out of the stale-session collapse without
  // reading the wall clock.
  { key: 's2', title: 'Other chat', messages: 1, running: false, mode: '', created: '', last_ts: '2099-01-01T00:00:00Z' },
] as ChatSlot[]
const folder = (id: string, collapsed: boolean, parent_id: string | null = null) =>
  ({ id, name: `Folder ${id}`, parent_id, collapsed }) as unknown as ChatFolder

/** Passes the store's slot list down, so the created slot reaches the sidebar
 *  in the same commit as the activation, as on the chat page. */
function StoreBackedSidebar({ onOpenSlotInNewTab }: { onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void }) {
  const slots = useAppSelector(s => s.dashboard.slots)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  return (
    <ChatSidebar
      slots={slots} activeSlot={activeSlot} unreadSlots={[]}
      history={[]} historyHasMore={false} defaultAgent="kirocrew" installedAgents={[]}
      onOpenSlotInNewTab={onOpenSlotInNewTab}
    />
  )
}

function renderSidebar(folders: ChatFolder[], opts: { withTabs?: boolean } = {}) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'linux' }, connected: true, slots: SLOTS, slotsLoaded: true,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: ORIGIN, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0, pendingInput: null, slotContextPct: {},
      voicePlaying: false, voiceAudio: null, subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  // Stands in for the chat page's tab strip: a foreground open switches to the
  // new tab, a background one leaves the user where they are.
  const openInTab = (key: string, o?: { background?: boolean }) => { if (!o?.background) store.dispatch(setActiveSlot(key)) }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  mocks.chatFolders.mockResolvedValue(folders)
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <StoreBackedSidebar onOpenSlotInNewTab={opts.withTabs ? openInTab : undefined} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

const newButton = () => screen.getByRole('button', { name: /new chat session/i })
const expandedIds = () => mocks.updateChatFolder.mock.calls
  .filter(([, body]) => (body as { collapsed?: boolean })?.collapsed === false)
  .map(([id]) => id)

describe('ChatSidebar – a default-folder create reveals its row', () => {
  beforeEach(() => {
    localStorage.clear()
    mocks.createChatSlot.mockReset()
    mocks.chatFolders.mockReset().mockResolvedValue([])
    mocks.updateChatFolder.mockReset().mockResolvedValue({})
  })

  it('header New into a collapsed default folder expands that folder', async () => {
    mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY, folder_id: 'fD', messages: 0, running: false })
    const store = renderSidebar([folder('fD', true)])
    fireEvent.click(newButton())
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
  })

  it('header New into an expanded default folder inside a collapsed parent expands the parent', async () => {
    mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY, folder_id: 'fC', messages: 0, running: false })
    const store = renderSidebar([folder('fP', true), folder('fC', false, 'fP')])
    fireEvent.click(newButton())
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    await waitFor(() => expect(expandedIds()).toEqual(['fP']))
  })

  it('New incognito into a collapsed default folder expands that folder', async () => {
    mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY, folder_id: 'fD', messages: 0, running: false, memory_mode: 'incognito' })
    const store = renderSidebar([folder('fD', true)])
    fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('new-incognito-chat'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
  })

  it('keeps the user\'s search while it opens the folder', async () => {
    // A create is not a reveal request: the search the user typed stays, even
    // though it hides the new row, and the folder still opens so the row shows
    // once they clear it.
    mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY, title: 'Brand new', folder_id: 'fD', messages: 0, running: false })
    const store = renderSidebar([folder('fD', true)])
    const search = screen.getByPlaceholderText('Search sessions…') as HTMLInputElement
    await waitFor(() => expect(screen.queryByText('Other chat')).not.toBeNull())
    fireEvent.change(search, { target: { value: 'Session 1' } })
    // Precondition: the search is in effect and drops rows that do not match.
    await waitFor(() => expect(screen.queryByText('Other chat')).toBeNull())
    fireEvent.click(newButton())
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
    expect(search.value).toBe('Session 1')
  })

  it('a create the user switched away from leaves the folder collapsed', async () => {
    let resolve: (v: unknown) => void = () => {}
    mocks.createChatSlot.mockReturnValue(new Promise(r => { resolve = r }))
    const store = renderSidebar([folder('fD', true)])
    fireEvent.click(newButton())
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    store.dispatch(setActiveSlot('s-other'))
    resolve({ key: NEW_KEY, folder_id: 'fD', messages: 0, running: false })
    await waitFor(() => expect(store.getState().dashboard.slots.some(s => s.key === NEW_KEY)).toBe(true))
    expect(store.getState().chat.activeSlot).toBe('s-other')
    expect(store.getState().chat.revealRequest).toBeFalsy()
    expect(expandedIds()).toEqual([])
  })

  it('Ctrl+Shift-click New into a collapsed default folder expands that folder', async () => {
    // The foreground tab gesture creates with `activate: false`, then switches
    // to the new tab, so the chat is in view and its row must show too.
    mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY, folder_id: 'fD', messages: 0, running: false })
    const store = renderSidebar([folder('fD', true)], { withTabs: true })
    fireEvent.click(newButton(), { ctrlKey: true, shiftKey: true })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    await waitFor(() => expect(expandedIds()).toEqual(['fD']))
  })

  it('Ctrl-click New into a collapsed default folder leaves it collapsed', async () => {
    // A background tab keeps the user on their session, so nothing new is in view.
    mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY, folder_id: 'fD', messages: 0, running: false })
    const store = renderSidebar([folder('fD', true)], { withTabs: true })
    fireEvent.click(newButton(), { ctrlKey: true })
    await waitFor(() => expect(store.getState().dashboard.slots.some(s => s.key === NEW_KEY)).toBe(true))
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
    expect(store.getState().chat.revealRequest).toBeFalsy()
    expect(expandedIds()).toEqual([])
  })

  it('a create with no default folder asks for no reveal', async () => {
    mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY, messages: 0, running: false })
    const store = renderSidebar([folder('fD', true)])
    fireEvent.click(newButton())
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
    expect(store.getState().chat.revealRequest).toBeFalsy()
    expect(expandedIds()).toEqual([])
  })
})
