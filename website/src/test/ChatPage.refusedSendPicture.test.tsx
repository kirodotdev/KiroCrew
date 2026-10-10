/**
 * A refused PLAIN send (slot idle, so no steer) must hand the staged picture
 * back with the text.
 *
 * The plain send's wire text carries the `![image](dest)` lines
 * prepareSendPayload puts in front of what was typed, and the staged files are
 * cleared at send time. Restoring that text as-is leaves the marker in the
 * composer and no chip: the retry then ships no `meta.images`, and since the
 * gateway builds image blocks ONLY from that list (never from the text), the
 * model would never see the picture -- while the bubble still renders it. The
 * steer path already hands both back through restoreQueuedContent; this pins
 * the same hand-back on the plain path.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import type { RootState } from '../store'
import { store as appStore } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { api } from '../api/client'
import { DRAFTS_KEY } from '../utils/chatDrafts'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const sendChat = vi.fn()
const slotRow = () => ({
  key: 'slot-a', messages: 1, running: false, mode: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
  subagents_running: false,
})
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockImplementation(() => Promise.resolve([slotRow()])),
    chatSlotDetail: vi.fn().mockImplementation(() => Promise.resolve({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 })),
    sendChat: (...a: unknown[]) => sendChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    suggestions: vi.fn().mockResolvedValue({ suggestions: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

const TYPED_TEXT = 'what does this stack trace mean?'

function makeStore(extraSlots: Array<ReturnType<typeof slotRow>> = []) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [slotRow(), ...extraSlots],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: true, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
  // Receipt handlers read the singleton; rendered selectors read Provider.
  // They must see the same transcript, as they do in the running app.
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  return store
}

describe('ChatPage refused plain send with a picture', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    sendChat.mockReset()
  })
  afterEach(() => {
    localStorage.removeItem(DRAFTS_KEY)
    vi.restoreAllMocks()
  })

  it('a refused plain send keeps a typed image line as text on retry', async () => {
    const typed = '![image](/tmp/private.png)'
    const store = makeStore()
    sendChat.mockResolvedValueOnce({ ok: false, status: 409, json: () => Promise.resolve({ ok: false, error: 'slot agent mismatch' }) })
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={store}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })
    const input = await screen.findByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: typed } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await waitFor(() => expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(true))
    expect(sendChat).toHaveBeenCalledTimes(1)
    expect(sendChat.mock.calls[0][0]).toBe(typed)
    expect(sendChat.mock.calls[0][4]).not.toHaveProperty('images')
    expect(sendChat.mock.calls[0][5]).toBeFalsy()
    expect(input.value).toBe(typed)
    expect(screen.queryByRole('group', { name: '/tmp/private.png' })).not.toBeInTheDocument()

    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
    expect(sendChat.mock.calls[1][0]).toBe(typed)
    expect(sendChat.mock.calls[1][4]).not.toHaveProperty('images')
    expect(sendChat.mock.calls[1][5]).toBeFalsy()
  })

  it('a refused plain send hands the staged picture back with the text, and the send carried it as meta.images', async () => {
    const store = makeStore()
    vi.mocked(api.uploadFiles).mockResolvedValueOnce({ paths: ['/tmp/uploads/shot.png'] })
    sendChat.mockResolvedValue({ ok: false, status: 409, json: () => Promise.resolve({ ok: false, error: 'slot agent mismatch' }) })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={store}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })
    const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'shot.png', { type: 'image/png' })] })
    fireEvent.change(fileInput)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    // An image chip is a group named by its path (and an <img alt=path>).
    expect(await screen.findByRole('group', { name: '/tmp/uploads/shot.png' })).toBeInTheDocument()
    fireEvent.change(input, { target: { value: TYPED_TEXT } })
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    const [wireText, , , , meta, steer] = sendChat.mock.calls[0]
    // Not a steer: the slot is idle, so this is the plain send path.
    expect(steer).toBeFalsy()
    expect(wireText).toBe(`![image](/tmp/uploads/shot.png)\n\n${TYPED_TEXT}`)
    expect(meta).toEqual(expect.objectContaining({ images: ['/tmp/uploads/shot.png'] }))
    // Text WITHOUT the marker line, and the chip back: the retry ships the
    // picture as meta.images again instead of a marker the gateway reads as prose.
    await waitFor(() => expect(input.value).toBe(TYPED_TEXT))
    expect(await screen.findByRole('group', { name: '/tmp/uploads/shot.png' })).toBeInTheDocument()
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
    expect(sendChat.mock.calls[1][0]).toBe(wireText)
    expect(sendChat.mock.calls[1][4]).toEqual(expect.objectContaining({ images: ['/tmp/uploads/shot.png'] }))
    expect(sendChat.mock.calls[1][5]).toBeFalsy()
  })

  it('a refused mixed send hands back the picture chip, the file chip and the text when the file was @-mentioned first', async () => {
    const store = makeStore()
    vi.mocked(api.uploadFiles)
      .mockResolvedValueOnce({ paths: ['/tmp/uploads/shot.png'] })
      .mockResolvedValueOnce({ paths: ['/tmp/uploads/notes.txt'] })
    sendChat.mockResolvedValue({ ok: false, status: 409, json: () => Promise.resolve({ ok: false, error: 'slot agent mismatch' }) })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    await act(async () => {
      render(
        <QueryClientProvider client={qc}>
          <Provider store={store}>
            <ThemeProvider>
              <MemoryRouter><ChatPage /></MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
    })
    const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'shot.png', { type: 'image/png' })], configurable: true })
    fireEvent.change(fileInput)
    await screen.findByRole('group', { name: '/tmp/uploads/shot.png' })
    Object.defineProperty(fileInput, 'files', { value: [new File(['y'], 'notes.txt', { type: 'text/plain' })], configurable: true })
    fireEvent.change(fileInput)
    await screen.findByRole('group', { name: '/tmp/uploads/notes.txt' })
    fireEvent.change(input, { target: { value: '@notes.txt\ncompare this picture' } })
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    const [wireText, , , , meta] = sendChat.mock.calls[0]
    expect(wireText).toBe('![image](/tmp/uploads/shot.png)\n\n[attached_file 1] /tmp/uploads/notes.txt\ncompare this picture')
    expect(meta).toEqual(expect.objectContaining({ images: ['/tmp/uploads/shot.png'], files: ['/tmp/uploads/notes.txt'] }))
    // The draft comes back as typed, with both chips: the mention is a token
    // again rather than a marker left as prose the gateway cannot read.
    await waitFor(() => expect(input.value).toBe('@notes.txt\ncompare this picture'))
    expect(await screen.findByRole('group', { name: '/tmp/uploads/shot.png' })).toBeInTheDocument()
    expect(await screen.findByRole('group', { name: '/tmp/uploads/notes.txt' })).toBeInTheDocument()
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
    expect(sendChat.mock.calls[1][0]).toBe(wireText)
    expect(sendChat.mock.calls[1][4]).toEqual(expect.objectContaining({ images: ['/tmp/uploads/shot.png'], files: ['/tmp/uploads/notes.txt'] }))
  })
})
