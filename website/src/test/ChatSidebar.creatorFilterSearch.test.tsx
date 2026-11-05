/** A creator filter keeps the search field on screen and follows catalog changes. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation } from 'react-router-dom'

import ChatSidebar from '../pages/ChatSidebar'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import type { ChatSlot, HistorySession } from '../types'
import { triggerRefresh } from '../store/dashboardSlice'
import { sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'

const SLOT = 'chat-1'

vi.mock('../pages/chat-sidebar/CreatorFilter', async importOriginal => ({
  ...(await importOriginal<typeof import('../pages/chat-sidebar/CreatorFilter')>()),
  useSharedCrewListView: () => ({ filterAvailable: true, filter: 'mine', setFilter: vi.fn(), bylines: { hidden: false, viewerId: 'viewer' } }),
}))

beforeEach(() => {
  vi.spyOn(api, 'sharedCrewSessions').mockResolvedValue({
    schema_version: 1, total_matching: 4, hidden_by_filter: 0, next_cursor: '',
    items: [
      { key: 'chat-2', title: 'open-row' },
      { key: 'dashboard_chat-2', title: 'prefixed-open-row' },
      { key: 'dashboard_member-helper', title: 'member-row' },
      { key: 'chat-9', title: 'Closed session' },
    ],
  })
})

afterEach(() => { vi.restoreAllMocks() })

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}{location.search}</output>
}

function mount(onSelectSlot?: (key: string) => void, extraSlots: ChatSlot[] = [], openKey = 'chat-2') {
  const slots = [
    { key: SLOT, title: 'A session', running: false, tags: [], created: '', last_ts: '' },
    { key: openKey, title: 'Another session', running: false, tags: [], created: '', last_ts: '' },
  ] as unknown as ChatSlot[]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: [...slots, ...extraSlots], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon',
      sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { ...chatReducer(undefined, { type: '@@test/init' }), activeSlot: SLOT },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const tree = (localSlots = slots, history: HistorySession[] = []) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <LocationProbe />
            <ChatSidebar
              slots={localSlots} activeSlot={SLOT} unreadSlots={[]}
              history={history} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onSelectSlot={onSelectSlot}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  return { ...render(tree()), tree, slots, qc, store }
}

describe('sidebar creator filter', () => {
  it('keeps the search field beside the filtered list', () => {
    mount()
    expect(screen.getByTestId('creator-filtered-sessions')).toBeInTheDocument()
    expect(screen.getByTestId('list-dock')).toBeInTheDocument()
    expect(screen.getByTestId('search-field-glass')).toBeInTheDocument()
  })

  it.each(['slots', 'history', 'refresh'] as const)('refreshes active and cached creator pages on %s changes', async change => {
    const { qc, store, tree, slots, rerender } = mount()
    await screen.findByTestId('creator-filter-row-chat-2')
    const key = [...sharedCrewKeys.sessions('mine', 'cached'), '']
    const other = [...sharedCrewKeys.sessions('unattributed', 'search'), 'page-2']
    qc.setQueryData(sharedCrewKeys.people, { people: [] })
    qc.setQueryData(key, { items: ['old'] })
    qc.setQueryData(other, { items: ['cached'] })
    const read = vi.fn().mockResolvedValue({ items: ['new'] })
    const observer = new QueryObserver(qc, { queryKey: key, queryFn: read, staleTime: Infinity })
    const unsubscribe = observer.subscribe(() => {})
    try {
      expect(read).not.toHaveBeenCalled()
      act(() => {
        if (change === 'slots') rerender(tree([...slots, { ...slots[0], key: 'chat-2' }]))
        else if (change === 'history') rerender(tree(slots, [{ key: 'persisted' } as HistorySession]))
        else store.dispatch(triggerRefresh())
      })
      await waitFor(() => expect(qc.getQueryData(key)).toEqual({ items: ['new'] }))
      expect(read).toHaveBeenCalledTimes(1)
      expect(qc.getQueryState(other)?.isInvalidated).toBe(true)
      expect(qc.getQueryState(sharedCrewKeys.people)?.isInvalidated).toBe(true)
    } finally {
      unsubscribe()
      qc.clear()
    }
  })

  it('opens an open filtered row through onSelectSlot, as a lane row does', async () => {
    const onSelectSlot = vi.fn()
    const resume = vi.spyOn(api, 'resumeChatSlot')
    mount(onSelectSlot)
    fireEvent.click(await screen.findByTestId('creator-filter-row-chat-2'))
    expect(onSelectSlot).toHaveBeenCalledWith('chat-2')
    expect(resume).not.toHaveBeenCalled()
  })

  it('opens a creator-filtered dashboard_member row on its live crewmate surface without resuming', async () => {
    const onSelectSlot = vi.fn()
    const resume = vi.spyOn(api, 'resumeChatSlot').mockResolvedValue(
      { ok: true, key: 'member-helper', mode: 'member', surface: 'member', messages: [] } as unknown as Awaited<ReturnType<typeof api.resumeChatSlot>>,
    )
    const { store } = mount(onSelectSlot, [
      { key: 'member-helper', agent: 'helper', mode: 'member', surface: 'member', running: false } as ChatSlot,
    ])
    fireEvent.click(await screen.findByTestId('creator-filter-row-dashboard_member-helper'))
    expect(screen.getByTestId('location')).toHaveTextContent('/members?member=helper')
    expect(resume).not.toHaveBeenCalled()
    expect(onSelectSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe(SLOT)
  })

  it.each(['chat-2', 'dashboard_chat-2'])('switches a prefixed catalog row using the actual live key %s', async openKey => {
    const onSelectSlot = vi.fn()
    const resume = vi.spyOn(api, 'resumeChatSlot')
    const detail = vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [] } as unknown as Awaited<ReturnType<typeof api.chatSlotDetail>>)
    const { store } = mount(onSelectSlot, [], openKey)
    fireEvent.click(await screen.findByTestId('creator-filter-row-dashboard_chat-2'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(openKey))
    expect(detail.mock.calls[0]?.[0]).toBe(openKey)
    expect(onSelectSlot).toHaveBeenCalledWith(openKey)
    expect(resume).not.toHaveBeenCalled()
  })

  it('resumes a closed filtered row, then tells the host through onSelectSlot', async () => {
    const onSelectSlot = vi.fn()
    const resume = vi.spyOn(api, 'resumeChatSlot').mockResolvedValue(
      { ok: true, key: 'chat-9', mode: '', surface: '', messages: [] } as unknown as Awaited<ReturnType<typeof api.resumeChatSlot>>,
    )
    const { store } = mount(onSelectSlot)
    fireEvent.click(await screen.findByTestId('creator-filter-row-chat-9'))
    await waitFor(() => expect(onSelectSlot).toHaveBeenCalledWith('chat-9'))
    expect(resume).toHaveBeenCalledWith('chat-9', 'Closed session')
    expect(store.getState().chat.activeSlot).toBe('chat-9')
  })
})
