import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, switchSlot, createSlot } from '../store/chatSlice'
import dashboardReducer, { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import {
  consumeChatHandoff,
  handoffToChat,
  installSoftNavigate,
  sendErrorToChat,
  __resetNavSeamForTests,
} from '../utils/errorReport'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
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

function makeStore(activeSlot: string, slots: { key: string; mode?: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        // connected: true is required for any test that exercises ChatPage.send().
        // send() has a defense-in-depth `if (!connected) return` at its top
        // (covers all 5 call sites: keyboard, follow-up option, reconnect
        // auto-send, widget event, question card). Tests that submit a draft and
        // assert on api.sendChat must opt in explicitly here — dashboardSlice
        // initial state defaults connected to false, which is also the value
        // during a fresh page load before the WS handshake.
        status: null, connected: true, slots: slots.map(s => ({ key: s.key, messages: 1, running: false, mode: s.mode || '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
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
}

async function renderPage(store: ReturnType<typeof makeStore>, mode?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter><ChatPage mode={mode} /></MemoryRouter>
        </ThemeProvider>
      </Provider>
      </QueryClientProvider>,
    )
  })
  return result!
}

async function renderAndWaitForInput(store: ReturnType<typeof makeStore>, mode?: string) {
  const result = await renderPage(store, mode)
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return result
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
  __resetNavSeamForTests()
  installSoftNavigate(() => {})
})

afterEach(() => {
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('ChatPage error handoff', { timeout: 15_000 }, () => {
  it('opens a staged hard-reload handoff in a fresh seeded session', async () => {
    const prompt = 'Diagnose the browser installation failure'
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-a': 'keep this draft' }))
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    const originalMessages = store.getState().chat.messages
    handoffToChat(prompt)

    await renderAndWaitForInput(store)

    const { api } = await import('../api/client')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('new-slot'))
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe(prompt)
    })
    expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')['slot-a']).toBe('keep this draft')
    expect(store.getState().chat.slotMessages['slot-a']).toEqual(originalMessages)
  })

  it('opens an already-mounted handoff in a fresh session without changing the current draft', async () => {
    const prompt = 'Diagnose the failed PR action'
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'question in progress' } })

    act(() => { sendErrorToChat(prompt) })

    const { api } = await import('../api/client')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('new-slot'))
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe(prompt)
    })
    expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')['slot-a']).toBe('question in progress')
  })

  it('re-stages a failed handoff without retrying against the mounted subscriber', async () => {
    const prompt = 'Diagnose the persistent session creation failure'
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    const { api } = await import('../api/client')
    vi.mocked(api.createChatSlot).mockRejectedValueOnce(new Error('gateway unavailable'))

    act(() => { sendErrorToChat(prompt) })

    await waitFor(() => {
      expect(store.getState().notifications.items).toEqual([
        expect.objectContaining({
          kind: 'agent',
          priority: 'critical',
          body: expect.stringContaining('Your message was restored'),
        }),
      ])
    })
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.activeSlot).toBe('slot-a')
    expect(consumeChatHandoff()).toBe(prompt)
  })

  it('re-stages the full FIFO when keyed prefill persistence fails', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    const { api } = await import('../api/client')
    vi.mocked(api.createChatSlot).mockResolvedValueOnce({
      key: 'unseeded-slot', title: 'unseeded-slot', messages: 0, running: false,
    })
    const originalSetItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, value) {
      if (this === sessionStorage && key === PREFILL_STORAGE_KEY) {
        throw new DOMException('quota exceeded', 'QuotaExceededError')
      }
      return originalSetItem.call(this, key, value)
    })

    act(() => {
      sendErrorToChat('failed prefill diagnostic')
      sendErrorToChat('queued successor')
    })

    await waitFor(() => expect(store.getState().notifications.items).toHaveLength(1))
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.activeSlot).toBe('slot-a')
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
    expect(consumeChatHandoff()).toBe('failed prefill diagnostic')
    expect(consumeChatHandoff()).toBe('queued successor')
    expect(consumeChatHandoff()).toBeNull()
  })

  it('opens rapid handoffs sequentially and keeps each prompt with its fresh slot', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    const { api } = await import('../api/client')
    vi.mocked(api.createChatSlot)
      .mockResolvedValueOnce({ key: 'error-one', title: 'error-one', messages: 0, running: false })
      .mockResolvedValueOnce({ key: 'error-two', title: 'error-two', messages: 0, running: false })

    act(() => {
      sendErrorToChat('first diagnostic')
      sendErrorToChat('second diagnostic')
    })

    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('error-two'))
    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('second diagnostic')
    })
    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(drafts['error-one']).toBe('first diagnostic')
    })
    expect(consumeChatHandoff()).toBeNull()
  })

  it('does not overwrite edits typed while the fresh slot detail is loading', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    const { api } = await import('../api/client')
    let resolveDetail!: (detail: Awaited<ReturnType<typeof api.chatSlotDetail>>) => void
    const pendingDetail = new Promise<Awaited<ReturnType<typeof api.chatSlotDetail>>>(resolve => {
      resolveDetail = resolve
    })
    vi.mocked(api.createChatSlot).mockResolvedValueOnce({
      key: 'slow-error-slot', title: 'slow-error-slot', messages: 0, running: false,
    })
    vi.mocked(api.chatSlotDetail).mockImplementationOnce(() => pendingDetail)

    act(() => { sendErrorToChat('original diagnostic') })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('slow-error-slot'))
    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('original diagnostic')
    })
    fireEvent.change(screen.getByLabelText('Message input'), {
      target: { value: 'original diagnostic with user edits' },
    })

    await act(async () => {
      resolveDetail({ messages: [], running: false, has_more: false, total: 0 })
      await pendingDetail
    })

    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
      .toBe('original diagnostic with user edits')
  })

  it('re-stages the full FIFO after the first create failure and stops processing', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    const { api } = await import('../api/client')
    let rejectFirst!: (reason?: unknown) => void
    const firstCreate = new Promise<Awaited<ReturnType<typeof api.createChatSlot>>>((_, reject) => {
      rejectFirst = reject
    })
    vi.mocked(api.createChatSlot).mockImplementationOnce(() => firstCreate)

    act(() => {
      sendErrorToChat('first diagnostic')
      sendErrorToChat('second diagnostic')
    })

    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    await act(async () => {
      rejectFirst(new Error('first gateway failure'))
      await firstCreate.catch(() => undefined)
    })
    await waitFor(() => expect(store.getState().notifications.items).toHaveLength(1))
    // The successor stays behind the failed head in one ingress FIFO, and the
    // mounted processor does not immediately retry either item.
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    expect(consumeChatHandoff()).toBe('first diagnostic')
    expect(consumeChatHandoff()).toBe('second diagnostic')
    expect(consumeChatHandoff()).toBeNull()
  })

  it('prevents an unmounted processor from clearing a remounted FIFO or stealing focus', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    const firstPage = await renderAndWaitForInput(store)
    const { api } = await import('../api/client')
    let resolveAbandoned!: (slot: Awaited<ReturnType<typeof api.createChatSlot>>) => void
    const abandonedCreate = new Promise<Awaited<ReturnType<typeof api.createChatSlot>>>(resolve => {
      resolveAbandoned = resolve
    })
    vi.mocked(api.createChatSlot).mockImplementationOnce(() => abandonedCreate)

    act(() => {
      sendErrorToChat('active diagnostic')
      sendErrorToChat('queued diagnostic')
    })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))

    firstPage.unmount()

    let resolveReplacement!: (slot: Awaited<ReturnType<typeof api.createChatSlot>>) => void
    const replacementCreate = new Promise<Awaited<ReturnType<typeof api.createChatSlot>>>(resolve => {
      resolveReplacement = resolve
    })
    vi.mocked(api.createChatSlot).mockImplementationOnce(() => replacementCreate)
    const replacementPage = await renderAndWaitForInput(store)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(2))

    await act(async () => {
      resolveAbandoned({ key: 'abandoned-slot', title: 'abandoned-slot', messages: 0, running: false })
      await abandonedCreate
    })

    // The stale processor neither switches the live view nor erases the newer
    // component's active + queued crash snapshot.
    expect(store.getState().chat.activeSlot).toBe('slot-a')
    expect(JSON.parse(sessionStorage.getItem('kirocrew_error_handoff_claimed') || '[]'))
      .toEqual([{ prompt: 'active diagnostic' }, { prompt: 'queued diagnostic' }])

    replacementPage.unmount()
    expect(consumeChatHandoff()).toBe('active diagnostic')
    expect(consumeChatHandoff()).toBe('queued diagnostic')
    expect(consumeChatHandoff()).toBeNull()

    await act(async () => {
      resolveReplacement({ key: 'replacement-slot', title: 'replacement-slot', messages: 0, running: false })
      await replacementCreate
    })
    expect(store.getState().chat.activeSlot).toBe('slot-a')
  })

  it('re-stages locally claimed handoffs if ChatPage unmounts before reconnect', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    store.dispatch(sseDisconnected())
    handoffToChat('first disconnected diagnostic')
    handoffToChat('second disconnected diagnostic')

    const page = await renderAndWaitForInput(store)
    expect(consumeChatHandoff()).toBeNull()

    page.unmount()

    expect(consumeChatHandoff()).toBe('first disconnected diagnostic')
    expect(consumeChatHandoff()).toBe('second disconnected diagnostic')
    expect(consumeChatHandoff()).toBeNull()
  })

  it('claims a disconnected handoff before its storage TTL and opens it after reconnect', async () => {
    const prompt = 'Diagnose a gateway connection failure'
    const now = Date.now()
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(now)
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    store.dispatch(sseDisconnected())
    handoffToChat(prompt)

    await renderAndWaitForInput(store)

    const { api } = await import('../api/client')
    expect(api.createChatSlot).not.toHaveBeenCalled()
    // Claimed into ChatPage's local FIFO: it is no longer aging in storage.
    expect(consumeChatHandoff()).toBeNull()
    nowSpy.mockReturnValue(now + 10 * 60_000)

    act(() => { store.dispatch(sseConnected()) })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('new-slot'))
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe(prompt)
    })
  })
})

// the per-slot draft fix relies on a load-bearing effect ORDER --
// ALL THREE per-composer persist effects (text, files, pastes) must be declared
// before the effect that advances composerSlotRef.current. React runs effects
// in declaration order, so if the advance ran first a persist effect batched
// with a slot switch would see the already-advanced ref and smear the outgoing
// slot's value onto the incoming one. A behavioral test can't reach this (RTL
// flushes effects between a keystroke and a dispatch, so the two never share a
// commit); this static source-order assertion does, and goes red the instant
// someone reorders the effects or moves the advance up. All three persist
// writes are asserted (not just text) because the advance now guards all three.
describe('ChatPage composerSlotRef effect ordering', () => {
  it('declares all three composer-persist effects before advancing composerSlotRef', () => {
    // Deliberately brittle: this matches exact code substrings from ChatPage.tsx
    // to lock a load-bearing effect-declaration order. An innocuous rename/reformat
    // will trip it. The fix is to UPDATE the substrings below to the new form,
    // never to delete the guard (the ordering invariant it protects is real).
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
    const textIdx = src.indexOf('setDraft(drafts.current, s, input)')
    const fileIdx = src.indexOf('setFileDraft(fileDrafts.current, s, pendingFiles)')
    const pasteIdx = src.indexOf('setPasteDraft(pasteDrafts.current, s, pasteBlocks)')
    const advanceIdx = src.indexOf('composerSlotRef.current = activeSlot')
    expect(textIdx, 'text-persist effect (setDraft off composerSlotRef) not found').toBeGreaterThan(-1)
    expect(fileIdx, 'file-persist effect (setFileDraft off composerSlotRef) not found').toBeGreaterThan(-1)
    expect(pasteIdx, 'paste-persist effect (setPasteDraft off composerSlotRef) not found').toBeGreaterThan(-1)
    expect(advanceIdx, 'composerSlotRef advance not found').toBeGreaterThan(-1)
    const order = 'persist effect must be declared BEFORE the composerSlotRef advance (draft-smear guard). If effects moved, UPDATE the substrings; do not delete this guard.'
    expect(textIdx, order).toBeLessThan(advanceIdx)
    expect(fileIdx, order).toBeLessThan(advanceIdx)
    expect(pasteIdx, order).toBeLessThan(advanceIdx)
  })

  // Symptom B (send routing to the slot the user already left) can't be covered
  // behaviorally: the ref-vs-closure divergence it fixes is a same-tick race
  // between the reducer's activeSlot flip and send()'s re-memoization, and RTL
  // flushes a render between any dispatch and the Enter event, so the closure
  // and activeSlotRef never disagree in a test. Guard the fix statically
  // instead: send() must resolve its target from uiSlot (= activeSlotRef.current),
  // never the bare closure activeSlot. Goes red if someone reverts `?? uiSlot`.
  it('sends to uiSlot (activeSlotRef), not the stale closure activeSlot (Symptom B)', () => {
    // Same brittle-by-design string match: if the send-target lines are renamed,
    // UPDATE the substrings to the new form; do not delete this guard.
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
    expect(src, 'uiSlot must be read from the activeSlot ref').toContain('const uiSlot = activeSlotRef.current')
    expect(src, 'send target must resolve from uiSlot').toContain('let slot = targetSlot ?? uiSlot')
    expect(src, 'send target must NOT fall back to the stale closure activeSlot').not.toContain('let slot = targetSlot ?? activeSlot')
  })
})

describe('ChatPage draft persistence', { timeout: 15_000 }, () => {
  it('preserves draft when switching sessions', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'draft for A' } })

    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    const saved = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
    expect(saved['slot-a']).toBe('draft for A')

    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('draft for A')
  })

  it('persists draft to localStorage on every keystroke', async () => {
    const store = makeStore('slot-x', [{ key: 'slot-x' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'live' } })

    await waitFor(() => {
      const saved = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(saved['slot-x']).toBe('live')
    })
  })

  it('removes draft when input is cleared', async () => {
    const store = makeStore('slot-x', [{ key: 'slot-x' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'temp' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts')!)['slot-x']).toBe('temp')
    })

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: '' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts')!)['slot-x']).toBeUndefined()
    })
  })

  it('keeps drafts for multiple sessions independently', async () => {
    const store = makeStore('s1', [{ key: 's1' }, { key: 's2' }, { key: 's3' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'one' } })

    act(() => { store.dispatch(setActiveSlot('s2')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'two' } })

    act(() => { store.dispatch(setActiveSlot('s3')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'three' } })

    const saved = await waitFor(() => {
      const s = JSON.parse(localStorage.getItem('mc-chat-drafts')!)
      expect(s['s3']).toBe('three')
      return s
    })
    expect(saved['s1']).toBe('one')
    expect(saved['s2']).toBe('two')
    expect(saved['s3']).toBe('three')

    act(() => { store.dispatch(setActiveSlot('s1')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('one')
  })

  it('does not overwrite target draft with source input on slot switch (race condition)', async () => {
    // Pre-seed a draft for slot-b
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-b': 'B draft' }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'A text' } })

    // Switch to slot-b — should restore "B draft", NOT "A text"
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('B draft')

    // Verify slot-a draft was saved correctly
    const saved = JSON.parse(localStorage.getItem('mc-chat-drafts')!)
    expect(saved['slot-a']).toBe('A text')
  })

  it('localStorage rehydration does not clobber in-memory draft (regression)', async () => {
    // Scenario: type in slot-a, localStorage is stale (doesn't have the draft yet),
    // switch to slot-b — the in-memory draft for slot-a must survive rehydration.
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'fresh text' } })

    // Simulate stale localStorage (e.g. another tab wrote an older version)
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-a': 'stale' }))

    // Switch to slot-b
    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Switch back to slot-a — should have 'fresh text', not 'stale'
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('fresh text')
  })

  it('draft survives round-trip through three slots', async () => {
    const store = makeStore('a', [{ key: 'a' }, { key: 'b' }, { key: 'c' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'alpha' } })

    act(() => { store.dispatch(setActiveSlot('b')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'beta' } })

    act(() => { store.dispatch(setActiveSlot('c')) })
    // Don't type anything in c

    act(() => { store.dispatch(setActiveSlot('a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('alpha')

    act(() => { store.dispatch(setActiveSlot('b')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('beta')

    act(() => { store.dispatch(setActiveSlot('c')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
  })

  it('pre-seeded per-slot file drafts survive slot switches without cross-leak', async () => {
    // Regression guard for screenshot-leak bug: pendingFiles was a single shared
    // useState, so files attached in slot-a appeared in slot-b's compose box
    // when the user switched tabs before sending.
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({
      'slot-a': ['/tmp/screenshot-a.png'],
      'slot-b': ['/tmp/screenshot-b1.png', '/tmp/screenshot-b2.png'],
    }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Switch to slot-b, then back to slot-a. The slot-switch effect flushes
    // fileDrafts on each transition; the pre-seeded per-slot entries must
    // round-trip unchanged (no cross-leak, no reset-to-empty).
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    act(() => { store.dispatch(setActiveSlot('slot-a')) })

    await waitFor(() => {
      const saved = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts')!)
      expect(saved['slot-a']).toEqual(['/tmp/screenshot-a.png'])
      expect(saved['slot-b']).toEqual(['/tmp/screenshot-b1.png', '/tmp/screenshot-b2.png'])
    })
  })

  it('async upload resolving after slot switch lands in the request slot', async () => {
    // Regression guard for the async-upload race:
    // user starts an upload in slot-a, switches to slot-b before the promise
    // resolves, and the uploaded file must land in slot-a's persisted draft —
    // not silently appear in slot-b's live state.
    const { api } = await import('../api/client')
    let resolveUpload!: (v: { paths: string[] }) => void
    const deferred = new Promise<{ paths: string[] }>(r => { resolveUpload = r })
    vi.mocked(api.uploadFiles).mockReturnValueOnce(deferred)

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Fire a drop event on the chat input area to trigger uploadFiles.
    const input = screen.getByLabelText('Message input')
    const dropTarget = input.closest('div') as HTMLElement
    const file = new File(['x'], 'test.png', { type: 'image/png' })
    await act(async () => {
      fireEvent.drop(dropTarget, { dataTransfer: { files: [file], types: ['Files'] } })
    })

    // Switch to slot-b while the upload is still pending.
    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Now resolve the upload — the file must be diverted to slot-a.
    await act(async () => {
      resolveUpload({ paths: ['/tmp/uploaded.png'] })
      await deferred
    })

    await waitFor(() => {
      const saved = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts') || '{}')
      expect(saved['slot-a']).toEqual(['/tmp/uploaded.png'])
      expect(saved['slot-b']).toBeUndefined()
    })
  })

  it('collapsed paste survives slot switch and sends expanded, not literal token', async () => {
    // Regression for the dead-token bug: a collapsed paste becomes a
    // `[ Paste #N · M lines ]` chip backed by an in-memory PasteBlock. Switching
    // slots used to clear the blocks while the token text was restored from the
    // text draft, so the chip went dead and the literal token was sent.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockClear()

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    const pasted = 'line1\nline2\nline3\nline4\nline5'  // >= PASTE_THRESHOLD_LINES

    // Fire a text paste — ChatInput collapses it into a token + PasteBlock.
    await act(async () => {
      fireEvent.paste(input, {
        clipboardData: { items: [], getData: (t: string) => (t === 'text' ? pasted : '') },
      })
    })
    // The textarea now holds the token, not the raw content.
    await waitFor(() => expect(input.value).toMatch(/\[ Paste #1 · 5 lines \]/))

    // Switch away and back WITHOUT sending.
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    act(() => { store.dispatch(setActiveSlot('slot-a')) })

    // Token text is restored AND still backed by its block.
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toMatch(/\[ Paste #1 · 5 lines \]/))

    // Send — the LLM must receive the EXPANDED content, never the literal token.
    await act(async () => { fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' }) })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    const llmText = vi.mocked(api.sendChat).mock.calls[0][0] as string
    expect(llmText).toContain('line1\nline2\nline3\nline4\nline5')
    expect(llmText).not.toContain('[ Paste #1 · 5 lines ]')
  })

  it('restores paste blocks to the active slot when the server refuses', async () => {
    // Driven by a REFUSAL, the arm that still restores. The token text comes back,
    // so the backing blocks must too, or the restored draft shows a dead token.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) } as unknown as Response)

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    const pasted = 'alpha\nbeta\ngamma\ndelta'
    await act(async () => {
      fireEvent.paste(input, {
        clipboardData: { items: [], getData: (t: string) => (t === 'text' ? pasted : '') },
      })
    })
    await waitFor(() => expect(input.value).toMatch(/\[ Paste #1 · 4 lines \]/))

    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    // After the failed send, the paste draft must be persisted for the slot so a
    // subsequent reload/switch can re-pair the token (not just left in the text).
    await waitFor(() => {
      const pasteDrafts = JSON.parse(localStorage.getItem('mc-chat-paste-drafts') || '{}')
      expect(pasteDrafts['slot-a']).toBeTruthy()
      expect(pasteDrafts['slot-a'][0].content).toBe(pasted)
    })
  })

  it('slow New Chat that resolves after a slot switch does not steal the typed text', async () => {
    // Symptom A: memory is high, user clicks New Chat, the create backend call
    // hangs. User switches to slot-b and types. When the slow create finally
    // resolves it must NOT hijack the view and drag slot-b's text into the new
    // chat. The text stays in slot-b, and the new chat opens empty.
    const { api } = await import('../api/client')
    let resolveCreate!: (v: { key: string; title: string; messages: number; running: boolean }) => void
    const deferred = new Promise<{ key: string; title: string; messages: number; running: boolean }>(r => { resolveCreate = r })
    vi.mocked(api.createChatSlot).mockReturnValueOnce(deferred as ReturnType<typeof api.createChatSlot>)

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Kick off a slow New Chat (stays pending).
    let createPromise: Promise<unknown>
    act(() => { createPromise = store.dispatch(createSlot(undefined)) })

    // User gives up waiting, switches to slot-b, and types there.
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'text meant for slot-b' } })

    // The slow create finally resolves.
    await act(async () => {
      resolveCreate({ key: 'new-slot', title: 'new-slot', messages: 0, running: false })
      await createPromise
    })

    // The view must still be on slot-b with the typed text intact...
    expect(store.getState().chat.activeSlot).toBe('slot-b')
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('text meant for slot-b')

    // ...and once the debounced draft save flushes, the text must be keyed to
    // slot-b, never leaked into the new chat's draft.
    const saved = await waitFor(() => {
      const s = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(s['slot-b']).toBe('text meant for slot-b')
      return s
    })
    expect(saved['new-slot']).toBeUndefined()
  })

  it('restores draft to localStorage when the server refuses', async () => {
    // A refusal means nothing was sent, so the draft is the only surviving copy.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) } as unknown as Response)

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(input, { target: { value: 'precious prompt' } }) })

    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    // Draft should be restored to localStorage after error
    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(drafts['slot-a']).toBe('precious prompt')
    })
  })

  it('a TRANSPORT rejection persists the draft even though a bubble was appended', async () => {
    // The bubble is store-only and this send never reached the server, so after a
    // reload it is gone -- the persisted draft is the only copy that can survive.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockRejectedValueOnce(new TypeError('Failed to fetch'))

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(input, { target: { value: 'survives a reload' } }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    // Precondition: the slot was idle, so a bubble WAS appended. That is the case
    // the old gate skipped the restore for, so this cannot pass by the busy route.
    await waitFor(() => {
      expect(store.getState().chat.messages.some(m => m.role === 'user')).toBe(true)
    })
    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(drafts['slot-a']).toBe('survives a reload')
    })
  })

  it('restores a staged session reference as a chip, not as raw link text', async () => {
    // Restores what the user TYPED plus the staged references, not the
    // link-appended text, so a retry cannot append each link a second time.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) } as unknown as Response)

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    sessionStorage.setItem('mc-chat-session-ref-drafts', JSON.stringify({
      'slot-a': [{ key: 'chat-ref-1', title: 'Release notes' }],
    }))
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(input, { target: { value: 'compare these' } }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      const restored = drafts['slot-a'] ?? ''
      // The typed text came back WITHOUT the serialized link spliced into it.
      expect(restored).toContain('compare these')
      expect(restored).not.toContain('sid=chat-ref-1')
      // The reference came back as a staged ref instead.
      const refs = JSON.parse(sessionStorage.getItem('mc-chat-session-ref-drafts') || '{}')
      expect((refs['slot-a'] ?? []).map((r: { key: string }) => r.key)).toContain('chat-ref-1')
    })
  })

  it('a REJECTED response (403, not a transport error) also restores the composer', async () => {
    // The two failure shapes lose the same thing and must recover the same way.
    // Previously only the transport branch restored, so a dropped connection kept
    // the user's message while a rejected response threw it away. `sendChat`
    // RESOLVES here with a non-ok body — it does not reject — which is why this
    // path needs its own test rather than being covered by the one above.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockResolvedValueOnce({
      json: async () => ({ ok: false, error: 'forbidden' }),
    } as unknown as Response)

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    sessionStorage.setItem('mc-chat-session-ref-drafts', JSON.stringify({
      'slot-a': [{ key: 'chat-ref-9', title: 'Release notes' }],
    }))
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(input, { target: { value: 'do not lose me' } }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(drafts['slot-a']).toContain('do not lose me')
      const refs = JSON.parse(sessionStorage.getItem('mc-chat-session-ref-drafts') || '{}')
      expect((refs['slot-a'] ?? []).map((r: { key: string }) => r.key)).toContain('chat-ref-9')
    })
  })

  it('does not clobber a newer draft typed while the send was in flight', async () => {
    // The send is in flight for up to 10s and the user can type a fresh message.
    // Recovery must MERGE, not overwrite — otherwise it loses newer work to
    // recover older. The failed payload is appended after the newer text.
    const { api } = await import('../api/client')
    let settleSend: (v: unknown) => void = () => {}
    vi.mocked(api.sendChat).mockImplementationOnce(
      () => new Promise((res) => { settleSend = res }) as unknown as Promise<Response>,
    )

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement

    await act(async () => { fireEvent.change(input, { target: { value: 'first message' } }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    // Composer cleared on send; the user types something new while it is pending.
    await act(async () => { fireEvent.change(input, { target: { value: 'second thought' } }) })
    await act(async () => { settleSend({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) }) })

    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      const restored = drafts['slot-a'] ?? ''
      expect(restored).toContain('second thought')   // newer work survived
      expect(restored).toContain('first message')    // failed payload recovered
      expect(restored.indexOf('second thought')).toBeLessThan(restored.indexOf('first message'))
    })
  })
})
