// The dictation note on the main chat's send path (`stt.dictation_note`). The
// Voice atom records which session's composer holds spoken words (covered in
// chat-core/composer/useComposerVoiceDictationNote.test.tsx); these tests stand
// in for it with `markDictated` and pin what the main composer does with the
// record: one note at the end of a dictated message when the setting is on,
// none otherwise, and the record following the text through a failed send, an
// emptied composer and a slot switch.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { createSlot, setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { store as appStore, type RootState } from '../store'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    createChatSlot: vi.fn(),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
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
import { api } from '../api/client'
import { queryClient } from '../api/queryClient'
import { DICTATION_NOTE, _resetDictationProvenance, isDictated, markDictated } from '../chat-core/composer/dictationProvenance'

function makeStore(activeSlot: string, slots: string[], running = false) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true, slots: slots.map(key => ({ key, messages: 1, running, subagents_running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: running, slotStopping: false, slotState: 'idle',
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
  // The steer receipt reads the app singleton.
  vi.spyOn(appStore, 'getState').mockImplementation(store.getState)
  // The slot load must agree, or it settles the turn as ended.
  vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running, has_more: false, total: 1 } as never)
  return store
}

async function renderPage(store: ReturnType<typeof makeStore>, waitForInput = true) {
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
  if (!waitForInput) return null
  return waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
}

async function typeAndSend(ta: HTMLTextAreaElement, text: string) {
  fireEvent.change(ta, { target: { value: text } })
  await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
}

const sentMessages = () => vi.mocked(api.sendChat).mock.calls.map(c => c[0] as string)

function setNote(on: boolean) {
  queryClient.setQueryData(['sttConfig'], { enabled: true, available: true, provider: 'local', dictation_note: on })
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.sendChat).mockClear()
  // A create no test asked for fails, as it did before this mock existed.
  vi.mocked(api.createChatSlot).mockReset().mockRejectedValue(new Error('no create in this test'))
  _resetDictationProvenance()
  setNote(true)
})
afterEach(() => { vi.restoreAllMocks() })

describe('ChatPage dictation note', { timeout: 15_000 }, () => {
  it('ends a dictated message with exactly one note when the setting is on', async () => {
    const ta = await renderPage(makeStore('slot-a', ['slot-a']))
    markDictated('slot-a')

    await typeAndSend(ta, 'turn on the lights')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).toBe(`turn on the lights\n\n${DICTATION_NOTE}`)
  })

  it('sends a typed-only message unchanged, even with the setting on', async () => {
    const ta = await renderPage(makeStore('slot-a', ['slot-a']))

    await typeAndSend(ta, 'typed by hand')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).toBe('typed by hand')
  })

  it('adds nothing with the setting off, the default', async () => {
    setNote(false)
    const ta = await renderPage(makeStore('slot-a', ['slot-a']))
    markDictated('slot-a')

    await typeAndSend(ta, 'spoken words')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).toBe('spoken words')
  })

  it('does not carry the mark to the next message typed in the same chat', async () => {
    const ta = await renderPage(makeStore('slot-a', ['slot-a']))
    markDictated('slot-a')
    await typeAndSend(ta, 'spoken first')
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))

    await typeAndSend(ta, 'typed second')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    expect(sentMessages()[1]).toBe('typed second')
  })

  it('a refused send hands the words back without the note and the retry carries it once', async () => {
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, status: 403, json: () => Promise.resolve({ error: 'nope' }) } as unknown as Response)
    const ta = await renderPage(makeStore('slot-a', ['slot-a']))
    markDictated('slot-a')

    await typeAndSend(ta, 'please retry me')

    await waitFor(() => expect(ta.value).toBe('please retry me'))
    expect(isDictated('slot-a')).toBe(true)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    expect(sentMessages()[1]).toBe(`please retry me\n\n${DICTATION_NOTE}`)
  })

  it('emptying the composer forgets the dictation, so a fresh typed message gets no note', async () => {
    const ta = await renderPage(makeStore('slot-a', ['slot-a']))
    markDictated('slot-a')
    fireEvent.change(ta, { target: { value: 'spoken then deleted' } })
    fireEvent.change(ta, { target: { value: '' } })

    await typeAndSend(ta, 'typed instead')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).toBe('typed instead')
  })

  it('a steer into the running turn ends with the note', async () => {
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) } as unknown as Response)
    const ta = await renderPage(makeStore('slot-a', ['slot-a'], true))
    markDictated('slot-a')

    await typeAndSend(ta, 'use the staging bucket')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.sendChat).mock.calls[0][5]).toBe(true)
    expect(sentMessages()[0]).toBe(`use the staging bucket\n\n${DICTATION_NOTE}`)
  })

  it('a refused steer hands the words back without the note, and steering again adds it once', async () => {
    vi.mocked(api.sendChat)
      .mockResolvedValueOnce({ ok: false, status: 409, json: () => Promise.resolve({ error: 'busy' }) } as unknown as Response)
      .mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) } as unknown as Response)
    const ta = await renderPage(makeStore('slot-a', ['slot-a'], true))
    markDictated('slot-a')

    await typeAndSend(ta, 'stop and rerun')

    await waitFor(() => expect(ta.value).toBe('stop and rerun'))
    expect(vi.mocked(api.sendChat).mock.calls[0][5]).toBe(true)
    expect(isDictated('slot-a')).toBe(true)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    expect(sentMessages()[1]).toBe(`stop and rerun\n\n${DICTATION_NOTE}`)
  })

  it('a refused steer of TYPED text that happens to end with the note line hands it back whole', async () => {
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, status: 409, json: () => Promise.resolve({ error: 'busy' }) } as unknown as Response)
    const ta = await renderPage(makeStore('slot-a', ['slot-a'], true))
    const typed = `quoting the setting:\n\n${DICTATION_NOTE}`

    await typeAndSend(ta, typed)

    await waitFor(() => expect(ta.value).toBe(typed))
    expect(isDictated('slot-a')).toBe(false)
  })

  it('a command sent from a dictated composer goes out without the note, so its arguments stay as typed', async () => {
    const ta = await renderPage(makeStore('slot-a', ['slot-a']))
    markDictated('slot-a')

    await typeAndSend(ta, '/goal clear')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).toBe('/goal clear')
  })

  it('a composer with no chat yet drops its dictation record once it shows a chat', async () => {
    // No chat yet: the composer is the welcome state's.
    const store = makeStore(null as unknown as string, [])
    await renderPage(store, false)
    expect(store.getState().chat.activeSlot).toBeNull()
    markDictated(null)

    await act(async () => { store.dispatch(setActiveSlot('slot-a')) })
    await waitFor(() => expect(isDictated(null)).toBe(false))
  })

  // A new chat created from a composer takes over only what was entered while
  // the create was pending. The real `createSlot` thunk runs against a deferred
  // `api.createChatSlot`, so the text is entered inside that window.
  function deferredCreate(key: string) {
    let resolve!: (v: unknown) => void
    vi.mocked(api.createChatSlot).mockReturnValue(new Promise(r => { resolve = r }) as never)
    return () => resolve({ key, title: key, messages: 0, running: false })
  }

  it('a new chat that takes over only typed words gets no note, and the dictated draft keeps its own', async () => {
    const store = makeStore('slot-a', ['slot-a'])
    const ta = await renderPage(store)
    fireEvent.change(ta, { target: { value: 'spoken words' } })
    markDictated('slot-a')

    const land = deferredCreate('slot-new')
    let created!: Promise<unknown>
    await act(async () => { created = store.dispatch(createSlot({ memory_mode: 'persistent' })) as unknown as Promise<unknown> })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    fireEvent.change(ta, { target: { value: 'spoken words hello' } })
    await act(async () => { land(); await created })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-new'))
    await waitFor(() => expect(ta.value.trim()).toBe('hello'))
    expect(isDictated('slot-new')).toBe(false)

    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).not.toContain(DICTATION_NOTE)

    await act(async () => { store.dispatch(setActiveSlot('slot-a')) })
    await waitFor(() => expect(ta.value).toBe('spoken words'))
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    expect(sentMessages()[1]).toBe(`spoken words\n\n${DICTATION_NOTE}`)
  })

  it('words dictated while a new chat is created move to it unmarked, and the typed draft left behind is unmarked too', async () => {
    const store = makeStore('slot-a', ['slot-a'])
    const ta = await renderPage(store)
    fireEvent.change(ta, { target: { value: 'typed first' } })

    const land = deferredCreate('slot-new')
    let created!: Promise<unknown>
    await act(async () => { created = store.dispatch(createSlot({ memory_mode: 'persistent' })) as unknown as Promise<unknown> })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    // The Voice atom writes into the composer it is on, still the origin's.
    fireEvent.change(ta, { target: { value: 'typed first then spoken' } })
    markDictated('slot-a')
    await act(async () => { land(); await created })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slot-new'))
    await waitFor(() => expect(ta.value.trim()).toBe('then spoken'))
    // The record never moves with a carry: a short window of spoken words goes
    // out without the note rather than any typed message going out with it.
    expect(isDictated('slot-new')).toBe(false)
    expect(isDictated('slot-a')).toBe(false)

    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).not.toContain(DICTATION_NOTE)

    await act(async () => { store.dispatch(setActiveSlot('slot-a')) })
    await waitFor(() => expect(ta.value).toBe('typed first'))
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    expect(sentMessages()[1]).toBe('typed first')
  })

  it('keeps the mark with the chat it was dictated into across a slot switch', async () => {
    const store = makeStore('slot-a', ['slot-a', 'slot-b'])
    const ta = await renderPage(store)
    markDictated('slot-a')

    await act(async () => { store.dispatch(setActiveSlot('slot-b')) })
    await typeAndSend(ta, 'typed in b')
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentMessages()[0]).toBe('typed in b')

    await act(async () => { store.dispatch(setActiveSlot('slot-a')) })
    await typeAndSend(ta, 'spoken in a')
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    expect(sentMessages()[1]).toBe(`spoken in a\n\n${DICTATION_NOTE}`)
  })
})
