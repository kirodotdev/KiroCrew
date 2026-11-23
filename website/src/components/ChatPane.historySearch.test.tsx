/**
 * A Bot's chat pane and its history search: ⌘F / Ctrl+F opens the search in
 * the host's side panel, the pane's rows survive the pane starting and
 * stopping to answer the key, and the list scopes rows only when its host asks.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { api } from '../api/client'
import ChatPane from './ChatPane'
import ChatMessageList, { type VirtualTranscriptHandle } from '../app-sdk/ChatMessageList'
import type { MessageRenderer } from '../app-sdk/messageRenderers'
import SearchHighlightContext, { useCurrentOcc } from '../hooks/SearchHighlightContext'
import type { ChatMessage } from '../types'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

afterEach(() => { cleanup() })

const press = (init: KeyboardEventInit): KeyboardEvent => {
  const event = new KeyboardEvent('keydown', { key: 'f', cancelable: true, ...init })
  act(() => { document.dispatchEvent(event) })
  return event
}

/** Prints what `useCurrentOcc` reads for the row it is drawn in. */
function OccProbe({ text }: { text: string }) {
  return <span data-testid={text}>{String(useCurrentOcc())}</span>
}
const probeRenderer: MessageRenderer = {
  id: 'user',
  roles: ['user', 'assistant'],
  render: (m) => <OccProbe key={m.ts} text={m.content} />,
}

function List({ searchRows }: { searchRows: boolean }) {
  const ref = useRef<VirtualTranscriptHandle | null>(null)
  const messages: ChatMessage[] = [
    { role: 'user', content: 'first', cls: '', ts: 'a' },
    { role: 'user', content: 'second', cls: '', ts: 'b' },
  ]
  return (
    <SearchHighlightContext.Provider value={{ term: 'x', caseSensitive: false, currentMessageIdx: 1, currentOccurrenceIdx: 0 }}>
      <ChatMessageList ref={ref} messages={messages} running={false} renderers={[probeRenderer]} searchRows={searchRows} />
    </SearchHighlightContext.Provider>
  )
}

describe('ChatMessageList searchRows', () => {
  it('tells each row its message index, so only the current match row is current', () => {
    render(<List searchRows />)
    expect(screen.getByTestId('first').textContent).toBe('-1')
    expect(screen.getByTestId('second').textContent).toBe('0')
  })

  it('leaves rows unscoped when the host runs no find of its own', () => {
    render(<List searchRows={false} />)
    expect(screen.getByTestId('second').textContent).toBe('-1')
  })
})

interface PaneProps { findInChat: boolean; topInset?: number; historySearchHost?: HTMLElement | null; onHistorySearchOpenChange?: (open: boolean) => void }

function renderPane(slot: string, content: string, props: PaneProps) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  })
  vi.mocked(api.chatSlotDetail).mockResolvedValue({
    messages: [{ role: 'user', content, cls: '', ts: '2026-11-20T10:00:00Z' }],
    running: false, has_more: false, total: 1,
  } as unknown as Awaited<ReturnType<typeof api.chatSlotDetail>>)
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slot, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const pane = (p: PaneProps) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slot} frameless findInChat={p.findInChat} topInset={p.topInset} historySearchHost={p.historySearchHost} onHistorySearchOpenChange={p.onHistorySearchOpenChange} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(pane(props))
  return { ...view, rerenderPane: (p: PaneProps) => view.rerender(pane(p)) }
}

describe('ChatPane rows across findInChat', () => {
  it('keeps every mounted row when the pane stops and starts answering the search key', async () => {
    const view = renderPane('member-bot-rows', 'keep this row mounted', { findInChat: true })
    const row = await waitFor(() => screen.getByText('keep this row mounted'))
    // A side panel takes focus: the pane stops answering ⌘F. The row's DOM
    // node must survive; a remount would drop its state (an open menu, a
    // half-selected quote, an expanded block).
    view.rerenderPane({ findInChat: false })
    expect(screen.getByText('keep this row mounted')).toBe(row)
    view.rerenderPane({ findInChat: true })
    expect(screen.getByText('keep this row mounted')).toBe(row)
  })
})

describe('ChatPane history search and pinned prompt', () => {
  it('opens the history search in the host\'s panel, not over the transcript, and leaves the pinned-prompt band in place', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const onOpenChange = vi.fn()
    const view = renderPane('member-bot-pin', 'a prompt', { findInChat: true, topInset: 40, historySearchHost: host, onHistorySearchOpenChange: onOpenChange })
    await waitFor(() => screen.getByText('a prompt'))
    // The band is the parent of the zero-height fold sentinel the banner sticks to.
    const band = () => view.container.querySelector('div[aria-hidden].h-0')!.parentElement as HTMLElement
    expect(band().style.top).toBe('40px')
    press({ ctrlKey: true })
    // Wait for ChatPaneHistorySearch's lazy ChatHistorySearch chunk.
    expect(await screen.findByRole('textbox', { name: 'Search this chat' }, { timeout: 5000 })).toBeTruthy()
    expect(host.contains(screen.getByTestId('chat-pane-history-search'))).toBe(true)
    expect(view.container.contains(screen.getByTestId('chat-pane-history-search'))).toBe(false)
    expect(onOpenChange).toHaveBeenLastCalledWith(true)
    expect(band().style.top).toBe('40px')
    press({ key: 'Escape' })
    expect(screen.queryByTestId('chat-pane-history-search')).toBeNull()
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
    host.remove()
  })
})
