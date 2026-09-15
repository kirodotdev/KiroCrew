/**
 * Regression test: the header rename editor belongs to ONE session.
 *
 * Opening the editor stores the current title in a draft; the commit runs on
 * blur. If the open flag were a bare boolean, switching sessions would leave the
 * editor open holding the PREVIOUS session's text while the commit resolved its
 * target from the live `activeSlot` — so a blur renamed the session now in front
 * to the previous one's title, leaving two tabs with one name.
 *
 * `editingTitleSlot` pins the editor to the slot it opened on and it renders
 * only while that slot is active, so a switch closes it and drops the draft.
 * Deliberate renames are unaffected: tapping away inside one session blurs (and
 * commits) before any switch, which the last case pins down.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// The non-editing title renders through TypewriterText; keep the text visible
// (and clickable) so the rename can be opened the way a user opens it.
vi.mock('../components/TypewriterText', async () => {
  const React = await import('react')
  return { default: ({ text }: { text: string }) => React.createElement('span', { 'data-testid': 'header-title' }, text) }
})

vi.mock('../pages/chat', () => ({
  ChatFooter: () => null,
  McpInfoButton: () => null,
  UserMessage: () => null,
  AssistantMessage: () => null,
}))
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: () => ({
    virtualItems: [], isAtBottom: true, getFollow: () => true, scrollToBottom: vi.fn(), mountIndex: vi.fn(), measureRef: () => () => {},
    farmIsMeasured: () => true,
    farmRecord: () => true,
    topSentinelRef: { current: null }, bottomSentinelRef: { current: null },
    offsetBefore: 0, offsetAfter: 0,
  }),
}))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
        )
      }
      return apiMocks[prop]
    },
  }),
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, text: () => Promise.resolve(''), json: () => Promise.resolve({}) }) as never

import ChatPage from '../pages/ChatPage'

const TITLE_A = 'Alpha session'
const TITLE_B = 'Beta session'
const mkSlot = (key: string, title: string) =>
  ({ key, title, messages: 0, running: false, mode: '', created: '', last_ts: '' })

const renderChatPage = () => {
  const slots = [mkSlot('chat-a', TITLE_A), mkSlot('chat-b', TITLE_B)]
  apiMocks.chatSlots = vi.fn().mockResolvedValue(slots)
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages: [], has_more: false, total: 0 })
  apiMocks.renameSlot = vi.fn().mockResolvedValue({})
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false,
      slots, slotsLoaded: true, approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'chat-a',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat/chat-a']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

const openRename = async () => {
  const label = await screen.findByTestId('header-title')
  act(() => { fireEvent.click(label) })
  return screen.getByDisplayValue(TITLE_A) as HTMLInputElement
}

const switchToB = (store: ReturnType<typeof createTestStore>) => {
  act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-b' }) })
}

describe('ChatPage – header rename is pinned to the session it opened on', () => {
  beforeEach(() => { Object.keys(apiMocks).forEach(k => delete apiMocks[k]) })

  it('tapping the title opens an editor seeded with that session title', async () => {
    renderChatPage()
    const input = await openRename()
    expect(input.value).toBe(TITLE_A)
  })

  it('switching sessions closes the editor instead of carrying the old draft over', async () => {
    const store = renderChatPage()
    await openRename()
    switchToB(store)
    expect(screen.queryByDisplayValue(TITLE_A)).toBeNull()
  })

  it('a commit arriving after a session switch renames nothing', async () => {
    const store = renderChatPage()
    const input = await openRename()
    switchToB(store)
    // Blur whatever survived the switch. With the editor pinned there is nothing
    // left to blur, so the stale draft never reaches the newly active session.
    act(() => { fireEvent.blur(input) })
    expect(apiMocks.renameSlot).not.toHaveBeenCalled()
  })

  it('renaming within one session still commits on blur', async () => {
    renderChatPage()
    const input = await openRename()
    act(() => { fireEvent.change(input, { target: { value: 'Renamed alpha' } }) })
    act(() => { fireEvent.blur(input) })
    expect(apiMocks.renameSlot).toHaveBeenCalledWith('chat-a', 'Renamed alpha')
  })

  it('returning to the session does not revive the abandoned draft', async () => {
    const store = renderChatPage()
    await openRename()
    switchToB(store)
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-a' }) })
    expect(screen.queryByDisplayValue(TITLE_A)).toBeNull()
  })

  it('an abandoned draft cannot overwrite a title that changed while away', async () => {
    const store = renderChatPage()
    await openRename()
    switchToB(store)
    // The session renames itself while the user is elsewhere — a generated title,
    // or another client. The abandoned draft still holds the title it replaced.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: 'chat-a', title: 'Alpha regenerated' } }) })
    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-a' }) })
    // Blur whatever the return actually mounted, not the handle from before the
    // switch: a detached node absorbs the event and proves nothing.
    const revived = screen.queryByDisplayValue(TITLE_A)
    if (revived) act(() => { fireEvent.blur(revived) })
    expect(apiMocks.renameSlot).not.toHaveBeenCalled()
  })
})

/**
 * Regression for #10203 (the header-side mirror of #10151): a server-refused
 * header rename dispatched the optimistic `sseSlotTitle` and then only reported
 * the failure via `showActionError` -- the refused title stayed in the store
 * (header AND sidebar read it) until an unrelated slot refetch or a reload.
 * The `.catch` now re-reads the server truth and applies ONLY this slot's
 * title, guarded on the store still holding the refused value -- never the
 * whole snapshot, whose late fulfillment could clobber a newer concurrent
 * write (the residual both review lanes flagged on a fetchSlots-based shape).
 * When the re-read itself fails too, the catch falls back to a guarded local
 * revert to the pre-rename title.
 */
describe('ChatPage - a refused header rename reverts the optimistic title (#10203)', () => {
  beforeEach(() => { Object.keys(apiMocks).forEach(k => delete apiMocks[k]) })

  it('snaps the store title back to the server truth when renameSlot rejects', async () => {
    const store = renderChatPage()
    const input = await openRename()
    apiMocks.renameSlot = vi.fn().mockRejectedValue(new Error('refused'))
    act(() => { fireEvent.change(input, { target: { value: 'Refused title' } }) })
    act(() => { fireEvent.blur(input) })
    // The optimistic title lands first (no microtask has run between the
    // synchronous blur commit and this assertion)...
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe('Refused title')
    // ...then the catch re-reads the server truth and the keyed apply
    // restores the value delivered by api.chatSlots.
    await waitFor(() => {
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe(TITLE_A)
    })
  })

  it('falls back to a local revert when the recovery re-read also fails', async () => {
    const store = renderChatPage()
    const input = await openRename()
    // Transport/auth failures take renameSlot and chatSlots down together, so
    // the catch must revert locally to the pre-rename title.
    apiMocks.renameSlot = vi.fn().mockRejectedValue(new Error('gateway down'))
    apiMocks.chatSlots = vi.fn().mockRejectedValue(new Error('gateway down'))
    act(() => { fireEvent.change(input, { target: { value: 'Refused title' } }) })
    act(() => { fireEvent.blur(input) })
    await waitFor(() => {
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe(TITLE_A)
    })
  })

  it('a late recovery never clobbers a newer concurrent title write', async () => {    const store = renderChatPage()
    const input = await openRename()
    // The rename is refused; while the recovery re-read is in flight, a newer
    // server-persisted title for the same slot lands (another client, or a
    // generated title). The guard must see the store no longer holds the
    // refused value and apply nothing.
    apiMocks.renameSlot = vi.fn().mockRejectedValue(new Error('refused'))
    let releaseSlots!: (v: unknown) => void
    apiMocks.chatSlots = vi.fn().mockReturnValue(new Promise(r => { releaseSlots = r }))
    act(() => { fireEvent.change(input, { target: { value: 'Refused title' } }) })
    act(() => { fireEvent.blur(input) })
    // Let the catch start the re-read, then land the newer write before the
    // stale snapshot resolves.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: 'chat-a', title: 'Newer concurrent title' } }) })
    act(() => { releaseSlots([mkSlot('chat-a', TITLE_A), mkSlot('chat-b', TITLE_B)]) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe('Newer concurrent title')
  })

  it('overlapping refused renames revert to the confirmed title, not to each other', async () => {
    const store = renderChatPage()
    // First rename is refused and the recovery re-read is held pending, so the
    // store keeps the first refused optimistic value on screen. The re-read is
    // deduped through queryClient.fetchQuery, so a second attempt joins this
    // same in-flight request.
    const input = await openRename()
    apiMocks.renameSlot = vi.fn().mockRejectedValue(new Error('refused'))
    let rejectSlots!: (e: unknown) => void
    apiMocks.chatSlots = vi.fn().mockReturnValue(new Promise((_r, rej) => { rejectSlots = rej }))
    act(() => { fireEvent.change(input, { target: { value: 'Refused B' } }) })
    act(() => { fireEvent.blur(input) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    // Second rename while the first recovery is pending: also refused. Its
    // re-read joins the shared in-flight request, which then fails outright ->
    // local fallback. It must restore the confirmed pre-rename baseline, not
    // the first attempt's refused value.
    const label = await screen.findByTestId('header-title')
    act(() => { fireEvent.click(label) })
    const second = screen.getByDisplayValue('Refused B') as HTMLInputElement
    act(() => { fireEvent.change(second, { target: { value: 'Refused C' } }) })
    act(() => { fireEvent.blur(second) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    act(() => { rejectSlots(new Error('gateway down')) })
    await waitFor(() => {
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe(TITLE_A)
    })
  })

  it('a pending failure never drags a confirmed external rename back to the baseline', async () => {
    const store = renderChatPage()
    // First rename is refused with its re-read held pending (baseline = TITLE_A).
    const input = await openRename()
    apiMocks.renameSlot = vi.fn().mockRejectedValue(new Error('refused'))
    let rejectSlots!: (e: unknown) => void
    apiMocks.chatSlots = vi.fn().mockReturnValue(new Promise((_r, rej) => { rejectSlots = rej }))
    act(() => { fireEvent.change(input, { target: { value: 'Refused B' } }) })
    act(() => { fireEvent.blur(input) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    // Another client's CONFIRMED rename lands over SSE while B is pending.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: 'chat-a', title: 'Confirmed external' } }) })
    // A second refused rename commits on top of the confirmed value; the shared
    // re-read then fails outright -> local fallback. The baseline must have
    // been refreshed to the confirmed title at commit time, so the fallback
    // restores 'Confirmed external', never the stale TITLE_A.
    const label = await screen.findByTestId('header-title')
    act(() => { fireEvent.click(label) })
    const second = screen.getByDisplayValue('Confirmed external') as HTMLInputElement
    act(() => { fireEvent.change(second, { target: { value: 'Refused C' } }) })
    act(() => { fireEvent.blur(second) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    act(() => { rejectSlots(new Error('gateway down')) })
    await waitFor(() => {
      expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe('Confirmed external')
    })
  })

  it('a delayed recovery never overwrites a newer confirmed rename to the same title', async () => {
    const store = renderChatPage()
    // Attempt 1: rename to X is refused; its recovery re-read is held pending,
    // so the snapshot it will eventually deliver predates everything below.
    const input = await openRename()
    apiMocks.renameSlot = vi.fn().mockRejectedValue(new Error('refused'))
    let releaseSlots!: (v: unknown) => void
    apiMocks.chatSlots = vi.fn().mockReturnValue(new Promise(r => { releaseSlots = r }))
    act(() => { fireEvent.change(input, { target: { value: 'Title X' } }) })
    act(() => { fireEvent.blur(input) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    // A newer title lands, then a SECOND attempt renames to the IDENTICAL
    // string X and SUCCEEDS. Title equality alone cannot tell this confirmed X
    // from attempt 1's stale optimistic X.
    act(() => { store.dispatch({ type: 'dashboard/sseSlotTitle', payload: { key: 'chat-a', title: 'Title Y' } }) })
    const label = await screen.findByTestId('header-title')
    act(() => { fireEvent.click(label) })
    const second = screen.getByDisplayValue('Title Y') as HTMLInputElement
    apiMocks.renameSlot = vi.fn().mockResolvedValue({})
    act(() => { fireEvent.change(second, { target: { value: 'Title X' } }) })
    act(() => { fireEvent.blur(second) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe('Title X')
    // Attempt 1's delayed re-read now resolves with its STALE pre-rename
    // snapshot (server still says TITLE_A in it). Its generation is stale, so
    // it must apply nothing: the newer confirmed X stays.
    act(() => { releaseSlots([mkSlot('chat-a', TITLE_A), mkSlot('chat-b', TITLE_B)]) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe('Title X')
  })

  it('a successful rename keeps the new title and never refetches slots', async () => {
    const store = renderChatPage()
    const input = await openRename()
    const slotsCallsBefore = apiMocks.chatSlots.mock.calls.length
    act(() => { fireEvent.change(input, { target: { value: 'Renamed alpha' } }) })
    act(() => { fireEvent.blur(input) })
    // Flush a macrotask so a refetch scheduled any number of microtask hops
    // down the resolved renameSlot promise would have landed by now.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-a')?.title).toBe('Renamed alpha')
    expect(apiMocks.chatSlots.mock.calls.length).toBe(slotsCallsBefore)
  })
})
