/**
 * Behavioural coverage for the multiplexed dashboard socket: the frame router's
 * question-card arms -- live cards, the drafts a retired blocking ask restores
 * or discards, and how the pending-questions snapshot reconciles held and
 * stateless cards. The frame-router setup repeats
 * UseWebSocketCoverage.test.tsx's (keep the copies identical); the fakes are in
 * UseWebSocketCoverageTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, renderHook, act, fireEvent, screen, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { setActiveSlot, clearMessages, clearQuestionCard, sseChatMessage, setQuestionCard, setQuestionDraft, setQuestionRequestInFlight, markQuestionSettled, resolveQuestionCard } from '../store/chatSlice'
import PendingQuestionCard from '../components/PendingQuestionCard'
import { __resetForTests, loadDrafts } from '../utils/chatDrafts'
import { registerMainComposer } from '../utils/composerRestore'
import { ACTIVE, WS_INSTANCES, MockWebSocket, MockAudio } from './UseWebSocketCoverageTestUtils'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
    voiceSynthesize: vi.fn().mockResolvedValue({ ok: true }),
    voiceCancel: vi.fn().mockResolvedValue({ ok: true }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

describe('useWebSocket frame router', () => {
  let testStore: ReturnType<typeof createTestStore>
  let rafCbs: FrameRequestCallback[]
  let originalCreateObjectUrl: typeof URL.createObjectURL
  let originalRevokeObjectUrl: typeof URL.revokeObjectURL

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    MockAudio.instances.length = 0
    MockAudio.playResult = () => Promise.resolve()
    rafCbs = []
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    // The production tree dispatches and reads through the same singleton
    // store. This harness intentionally dispatches through a Provider test
    // store, so mirror only production-marked coordinator rows into the
    // singleton read side; unmarked runner rows remain explicit per test.
    const mirroredCoordinatorRows = new Set<string>()
    testStore.subscribe(() => {
      const state = testStore.getState().chat
      const bySlot: Array<[string | null, typeof state.messages]> = [
        [state.activeSlot, state.messages],
        ...Object.entries(state.slotMessages),
      ]
      for (const [slot, messages] of bySlot) {
        if (!slot) continue
        for (const message of messages ?? []) {
          const id = message.meta?.approval_id
          if (message.role !== 'permission'
            || message.meta?.registry !== 'coordinator'
            || typeof id !== 'string') continue
          const key = `${slot}\u0000${id}\u0000${message.ts ?? ''}`
          if (mirroredCoordinatorRows.has(key)) continue
          mirroredCoordinatorRows.add(key)
          globalStore.dispatch(sseChatMessage({
            slot,
            role: message.role,
            content: message.content,
            ts: message.ts,
            cls: message.cls,
            meta: message.meta,
          }))
        }
      }
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
    originalCreateObjectUrl = URL.createObjectURL
    originalRevokeObjectUrl = URL.revokeObjectURL
    let blobSeq = 0
    URL.createObjectURL = vi.fn(() => `blob:voice-${++blobSeq}`)
    URL.revokeObjectURL = vi.fn()
    // Read paths resolve against the singleton store, not the Provider store.
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    URL.createObjectURL = originalCreateObjectUrl
    URL.revokeObjectURL = originalRevokeObjectUrl
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => {
      ws.simulateOpen()
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: ACTIVE, request_id: 'test-voice' } }))
    })
    return { ...hook, ws }
  }

  const chat = () => testStore.getState().chat

  it('stores a live question card under its server identity and clears it on resolution', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'question_card',
        data: { slot: ACTIVE, ask_id: 'ask-live', questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] },
      })
    })
    expect(chat().pendingQuestions[ACTIVE]?.ask_id).toBe('ask-live')

    act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-live' } }) })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('restores a drafted blocking ask retired after its card unmounts', async () => {
    localStorage.clear(); __resetForTests()
    const { ws } = mount()
    const card = setQuestionCard({
      slot: ACTIVE,
      ask_id: 'ask-unmounted',
      questions: [{ question: 'Region?', options: [{ label: 'us-east-1' }] }],
    })
    act(() => {
      globalStore.dispatch(card)
      ws.simulateMessage({ type: 'question_card', data: card.payload })
    })
    const view = render(<Provider store={globalStore}><PendingQuestionCard slotKey={ACTIVE} onFallbackSend={vi.fn()} /></Provider>)
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'eu-west-1' } })
    await waitFor(() => expect(globalStore.getState().chat.pendingQuestions[ACTIVE]?.draftAnswers)
      .toEqual({ 'Region?': 'eu-west-1' }))

    view.unmount()
    act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-unmounted', reason: 'expired' } }) })
    render(<Provider store={testStore}><PendingQuestionCard slotKey={ACTIVE} onFallbackSend={vi.fn()} /></Provider>)

    try {
      await waitFor(() => expect(loadDrafts()[ACTIVE]).toBe('eu-west-1'))
      expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
      // Informational, so a polite status naming the question: nothing failed and the draft is in the composer.
      expect(screen.getByRole('status')).toHaveTextContent(/stopped waiting for "Region\?"/)
      expect(screen.queryByRole('alert')).toBeNull()
    } finally {
      globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-unmounted', settled: true }))
      globalStore.dispatch(clearQuestionCard({ slot: ACTIVE, restored_notice: true }))
    }
  })

  it('discards another window draft when the blocking ask was answered', async () => {
    localStorage.clear(); __resetForTests()
    const restored: string[] = []
    const unregister = registerMainComposer((slot, text) => {
      if (slot !== ACTIVE) return false
      restored.push(text)
      return true
    })
    const { ws } = mount()
    const card = setQuestionCard({
      slot: ACTIVE,
      ask_id: 'ask-mounted',
      questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }],
    })
    act(() => {
      globalStore.dispatch(card)
      ws.simulateMessage({ type: 'question_card', data: card.payload })
    })
    render(<Provider store={testStore}><PendingQuestionCard slotKey={ACTIVE} onFallbackSend={vi.fn()} /></Provider>)
    fireEvent.click(screen.getByText('Yes'))
    await waitFor(() => expect(chat().pendingQuestions[ACTIVE]?.draftAnswers).toEqual({ 'Ship?': 'Yes' }))
    act(() => {
      globalStore.dispatch(setQuestionDraft({ slot: ACTIVE, answers: { 'Ship?': 'Yes' } }))
      ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-mounted', reason: 'answered' } })
    })

    try {
      await waitFor(() => expect(chat().pendingQuestions[ACTIVE]).toBeUndefined())
      expect(restored).toEqual([])
      expect(loadDrafts()[ACTIVE]).toBeUndefined()
      expect(chat().messages.some(message => message.role === 'error')).toBe(false)
      expect(chat().restoredQuestionNotices[ACTIVE]).toBeUndefined()
    } finally {
      unregister()
      globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-mounted', settled: true }))
    }
  })

  it('discards a draft settled by another surface before the resolution broadcast', () => {
    localStorage.clear(); __resetForTests()
    const restored: string[] = []
    const unregister = registerMainComposer((slot, text) => {
      restored.push(`${slot}:${text}`)
      return true
    })
    const { ws } = mount()
    const card = setQuestionCard({
      slot: ACTIVE,
      ask_id: 'ask-cross-surface',
      questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }],
    })
    act(() => {
      globalStore.dispatch(card)
      globalStore.dispatch(setQuestionDraft({ slot: ACTIVE, answers: { 'Ship?': 'Yes' } }))
      ws.simulateMessage({ type: 'question_card', data: card.payload })
      testStore.dispatch(setQuestionDraft({ slot: ACTIVE, answers: { 'Ship?': 'Yes' } }))
      globalStore.dispatch(setQuestionRequestInFlight({ ask_id: 'ask-cross-surface', inFlight: true }))
      globalStore.dispatch(markQuestionSettled({ ask_id: 'ask-cross-surface' }))
      globalStore.dispatch(setQuestionRequestInFlight({ ask_id: 'ask-cross-surface', inFlight: false }))
      ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-cross-surface' } })
    })

    try {
      expect(restored).toEqual([])
      expect(loadDrafts()[ACTIVE]).toBeUndefined()
      expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
      expect(chat().messages.some(message => message.role === 'error')).toBe(false)
      expect(chat().restoredQuestionNotices[ACTIVE]).toBeUndefined()
    } finally {
      unregister()
      globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-cross-surface', settled: true }))
    }
  })

  it.each(['composer', 'queued'])('restores a draft typed during a local %s release race', async (reason) => {
    localStorage.clear(); __resetForTests()
    const askId = `ask-local-${reason}`
    const restored: string[] = []
    const unregister = registerMainComposer((slot, text) => {
      if (slot !== ACTIVE) return false
      restored.push(text)
      return true
    })
    let releaseAnswer!: (value: { ok: boolean }) => void
    const answerPending = new Promise<{ ok: boolean }>(resolve => { releaseAnswer = resolve })
    vi.mocked(api.answerQuestion).mockReturnValueOnce(answerPending)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => createElement(
      Provider,
      { store: globalStore },
      createElement(QueryClientProvider, { client: queryClient }, children),
    )
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => {
      ws.simulateOpen()
      ws.simulateMessage({
        type: 'question_card',
        data: { slot: ACTIVE, ask_id: askId, questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] },
      })
    })
    const { resolveAskAfterSend } = await import('../lib/resolveAskAfterSend')
    const release = resolveAskAfterSend(
      { ok: true, ...(reason === 'queued' ? { queued: true } : {}) },
      askId,
      globalStore.dispatch,
      ACTIVE,
      globalStore.getState,
    )
    await waitFor(() => expect(globalStore.getState().chat.questionRequestsInFlight[askId]).toBe(reason))

    act(() => {
      globalStore.dispatch(setQuestionDraft({ slot: ACTIVE, answers: { 'Ship?': 'Wait for CI' } }))
      ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: askId, reason } })
    })
    expect(globalStore.getState().chat.pendingQuestions[ACTIVE]).toBeUndefined()

    await act(async () => { releaseAnswer({ ok: true }); await answerPending; await release })
    try {
      await waitFor(() => expect(restored).toEqual(['Wait for CI']))
      expect(globalStore.getState().chat.restoredQuestionNotices[ACTIVE]).toEqual({
        message: expect.stringContaining('composer'),
        kind: 'restored',
      })
    } finally {
      unregister()
      hook.unmount()
      globalStore.dispatch(resolveQuestionCard({ ask_id: askId, settled: true }))
      globalStore.dispatch(clearQuestionCard({ slot: ACTIVE, restored_notice: true }))
    }
  })

  it('does not restore a submitted draft when resolution arrives before the answer response', async () => {
    localStorage.clear(); __resetForTests()
    let releaseAnswer!: (value: { ok: boolean }) => void
    const answerPending = new Promise<{ ok: boolean }>(resolve => { releaseAnswer = resolve })
    vi.mocked(api.answerQuestion).mockReturnValueOnce(answerPending)
    const { ws } = mount()
    const card = setQuestionCard({
      slot: ACTIVE,
      ask_id: 'ask-submitting',
      questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }],
    })
    act(() => {
      globalStore.dispatch(card)
      ws.simulateMessage({ type: 'question_card', data: card.payload })
    })
    render(<Provider store={globalStore}><PendingQuestionCard slotKey={ACTIVE} onFallbackSend={vi.fn()} /></Provider>)
    fireEvent.click(screen.getByText('Yes'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(api.answerQuestion).toHaveBeenCalledWith('ask-submitting', { 'Ship?': 'Yes' }))

    try {
      act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-submitting' } }) })
      expect(loadDrafts()[ACTIVE]).toBeUndefined()
      expect(chat().messages.some(message => (message.role === 'notice' || message.role === 'error') && message.content.includes('composer'))).toBe(false)
      expect(chat().restoredQuestionNotices[ACTIVE]).toBeUndefined()
    } finally {
      await act(async () => { releaseAnswer({ ok: true }); await answerPending })
      globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-submitting', settled: true }))
    }
    await waitFor(() => expect(globalStore.getState().chat.pendingQuestions[ACTIVE]).toBeUndefined())
  })

  it.each([
    { outcome: 'request failure', settles: false, reason: undefined, restores: true },
    { outcome: 'answered retirement', settles: false, reason: 'answered', restores: false },
    { outcome: 'settlement', settles: true, reason: undefined, restores: false },
  ])('retains an in-flight retirement until $outcome', async ({ settles, reason, restores }) => {
    localStorage.clear(); __resetForTests()
    const restored: string[] = []
    const unregister = registerMainComposer((slot, text) => {
      if (slot !== ACTIVE) return false
      restored.push(text)
      return true
    })
    let resolveAnswer!: (value: { ok: boolean }) => void
    let rejectAnswer!: (error: Error) => void
    const answerPending = new Promise<{ ok: boolean }>((resolve, reject) => {
      resolveAnswer = resolve
      rejectAnswer = reject
    })
    vi.mocked(api.answerQuestion).mockReturnValueOnce(answerPending)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const globalWrapper = ({ children }: { children: React.ReactNode }) => createElement(
      Provider,
      { store: globalStore },
      createElement(QueryClientProvider, { client: queryClient }, children),
    )
    const hook = renderHook(() => useWebSocket(), { wrapper: globalWrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => {
      ws.simulateMessage({
        type: 'question_card',
        data: { slot: ACTIVE, ask_id: 'ask-retiring', questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] },
      })
    })
    const cardView = render(
      <Provider store={globalStore}>
        <PendingQuestionCard slotKey={ACTIVE} onFallbackSend={vi.fn()} />
      </Provider>,
    )
    fireEvent.click(screen.getByText('Yes'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(globalStore.getState().chat.questionRequestsInFlight['ask-retiring']).toBe(true))

    act(() => { ws.simulateMessage({
      type: 'question_card_resolved',
      data: { ask_id: 'ask-retiring', ...(reason ? { reason } : {}) },
    }) })
    expect(globalStore.getState().chat.pendingQuestions[ACTIVE]).toBeUndefined()

    if (settles) {
      await act(async () => { resolveAnswer({ ok: true }); await answerPending })
      await waitFor(() => expect(globalStore.getState().chat.questionRequestsInFlight['ask-retiring']).toBeUndefined())
    } else {
      await act(async () => { rejectAnswer(new Error('Offline')); await Promise.resolve(); await Promise.resolve() })
    }
    if (restores) {
      await waitFor(() => expect(restored).toEqual(['Yes']))
      // The hand-back is an informational, durable slice notice the chat_done refresh
      // cannot drop; the submit that failed is recorded once, as an error row.
      expect(globalStore.getState().chat.restoredQuestionNotices[ACTIVE]).toEqual({
        message: expect.stringContaining('composer'),
        kind: 'restored',
      })
      expect(globalStore.getState().chat.messages.some(message => message.role === 'notice')).toBe(false)
      expect(globalStore.getState().chat.messages.filter(message => message.role === 'error')).toHaveLength(1)
    } else {
      expect(restored).toEqual([])
      expect(globalStore.getState().chat.restoredQuestionNotices[ACTIVE]).toBeUndefined()
      expect(globalStore.getState().chat.messages.some(message => message.role === 'error')).toBe(false)
    }

    unregister()
    cardView.unmount()
    hook.unmount()
    globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-retiring', settled: true }))
    globalStore.dispatch(clearQuestionCard({ slot: ACTIVE, restored_notice: true }))
  })

  it('restores an in-flight answer exactly once when retirement races a 404', async () => {
    localStorage.clear(); __resetForTests()
    const restored: string[] = []
    const unregister = registerMainComposer((slot, text) => {
      if (slot !== ACTIVE) return false
      restored.push(text)
      return true
    })
    let rejectAnswer!: (error: Error) => void
    const answerPending = new Promise<{ ok: boolean }>((_resolve, reject) => { rejectAnswer = reject })
    vi.mocked(api.answerQuestion).mockReturnValueOnce(answerPending)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: React.ReactNode }) => createElement(
      Provider,
      { store: globalStore },
      createElement(QueryClientProvider, { client: queryClient }, children),
    )
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({
      type: 'question_card',
      data: { slot: ACTIVE, ask_id: 'ask-404-race', questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }] },
    }) })
    const fallback = vi.fn((text: string) => { restored.push(text) })
    const cardView = render(
      <Provider store={globalStore}>
        <PendingQuestionCard slotKey={ACTIVE} onFallbackSend={fallback} />
      </Provider>,
    )
    fireEvent.click(screen.getByText('Yes'))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(globalStore.getState().chat.questionRequestsInFlight['ask-404-race']).toBe(true))

    act(() => { ws.simulateMessage({
      type: 'question_card_resolved',
      data: { ask_id: 'ask-404-race', reason: 'expired' },
    }) })
    const { ApiError } = await import('../api/client')
    const notFound = new ApiError(404, 'gone')
    notFound.status = 404
    await act(async () => { rejectAnswer(notFound); await answerPending.catch(() => undefined) })

    await waitFor(() => expect(globalStore.getState().chat.questionRequestsInFlight['ask-404-race']).toBeUndefined())
    expect(restored).toEqual(['Yes'])
    expect(fallback).not.toHaveBeenCalled()

    unregister()
    cardView.unmount()
    hook.unmount()
    globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-404-race', settled: true }))
  })

  it('records a resolution for a card it never held, so a later reconcile cannot resurrect it', async () => {
    let releaseSnapshot!: (v: unknown) => void
    const snapshot = new Promise(res => { releaseSnapshot = res })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(snapshot)

    const { ws } = mount()
    // The reconnect snapshot is already in flight; the resolution lands mid-flight
    // for an ask this client has no local trace of.
    act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-ghost' } }) })

    await act(async () => {
      releaseSnapshot([{ ask_id: 'ask-ghost', slot: ACTIVE, questions: [{ question: 'Dead?', options: [{ label: 'x' }] }] }])
      await snapshot
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('ignores a resolution with no ask_id and keeps the newest ones after trimming', async () => {
    let releaseSnapshot!: (v: unknown) => void
    const snapshot = new Promise(res => { releaseSnapshot = res })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(snapshot)

    const { ws } = mount()
    // An empty id is not recordable, so it must be dropped outright.
    act(() => { ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: '' } }) })

    // Overflow the 200-entry bound while the rehydration snapshot is in flight.
    // The oldest ids are trimmed, but the newest must still be recognised as
    // dead so the snapshot cannot resurrect a card this client never held.
    act(() => {
      for (let i = 0; i < 204; i++) {
        ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: `ask-${i}` } })
      }
      ws.simulateMessage({ type: 'question_card_resolved', data: { ask_id: 'ask-target' } })
    })

    await act(async () => {
      releaseSnapshot([{ ask_id: 'ask-target', slot: ACTIVE, questions: [{ question: 'Q', options: [{ label: 'x' }] }] }])
      await snapshot
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('adopts a still-pending card the snapshot reports when nothing resolved it', async () => {
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { ask_id: 'ask-alive', slot: ACTIVE, questions: [{ question: 'Q', options: [{ label: 'x' }] }] },
    ])
    mount()
    await act(async () => { await Promise.resolve() })
    expect(chat().pendingQuestions[ACTIVE]?.ask_id).toBe('ask-alive')
  })

  it('rehydrates a STATELESS card (card_id, no ask_id) into an empty slot', async () => {
    // A card is a one-shot broadcast with no transcript row, so after a reload
    // the slot's needs-input status would name a question with nothing on screen
    // to answer or dismiss. The server-held record is the only way back.
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { card_id: 'card-alive', slot: ACTIVE, questions: [{ question: 'Which region?', options: [{ label: 'us-east-1' }] }] },
    ])
    mount()
    await act(async () => { await Promise.resolve() })
    const card = chat().pendingQuestions[ACTIVE]
    expect(card?.ask_id).toBeUndefined()
    expect(card?.serverCardId).toBe('card-alive')
    expect(card?.questions[0].question).toBe('Which region?')
  })

  it('drops a stateless card when the server announces its retirement', async () => {
    // Another window answered or dismissed it. The card must leave this window
    // too, or submitting it appends a duplicate turn.
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({
        type: 'question_card',
        data: { slot: ACTIVE, card_id: 'card-live', questions: [{ question: 'Q', options: [{ label: 'x' }] }] },
      })
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-live')
    // A retirement for a DIFFERENT card must not touch it: a stale announcement
    // for an already-replaced question would otherwise clear the live card.
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { card_id: 'card-other', slot: ACTIVE } })
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-live')
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { card_id: 'card-live', slot: ACTIVE } })
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('does not rehydrate a stateless card retired while the request was in flight', async () => {
    // The response describes the server as it was when the request was served, so
    // it races the retirement. Re-rendering the card would resurrect a dead ask.
    let release: (v: unknown) => void = () => {}
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((res) => { release = res }),
    )
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'question_card_resolved', data: { card_id: 'card-ghost', slot: ACTIVE } })
    })
    await act(async () => {
      release([{ card_id: 'card-ghost', slot: ACTIVE, questions: [{ question: 'Q', options: [{ label: 'x' }] }] }])
      await Promise.resolve()
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('drops a stateless card the server no longer lists (retired while disconnected)', async () => {
    // A tab that missed the retirement broadcast holds card A while the server
    // has moved on. Keeping it lets a submit answer a question the agent is past;
    // the snapshot's silence about A is the evidence, exactly as for a blocking
    // ask — which is why both kinds go through one reconcile.
    const held = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-gone',
      questions: [{ question: 'Stale', options: [{ label: 'x' }] }],
    })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    act(() => {
      globalStore.dispatch(held)
      testStore.dispatch(held as never)
    })
    mount()
    await act(async () => { await Promise.resolve() })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()
  })

  it('drops an answered blocking draft during snapshot reconciliation', async () => {
    localStorage.clear(); __resetForTests()
    const restored: string[] = []
    const unregister = registerMainComposer((slot, text) => {
      restored.push(`${slot}:${text}`)
      return true
    })
    const held = setQuestionCard({
      slot: ACTIVE,
      ask_id: 'ask-snapshot-answered',
      questions: [{ question: 'Ship?', options: [{ label: 'Yes' }] }],
    })
    const snapshot = Object.assign([], {
      resolved: { 'ask-snapshot-answered': 'answered' },
    })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce(snapshot)
    act(() => {
      globalStore.dispatch(held)
      globalStore.dispatch(setQuestionDraft({ slot: ACTIVE, answers: { 'Ship?': 'Yes' } }))
      testStore.dispatch(held as never)
      testStore.dispatch(setQuestionDraft({ slot: ACTIVE, answers: { 'Ship?': 'Yes' } }) as never)
    })
    mount()
    try {
      await waitFor(() => expect(chat().pendingQuestions[ACTIVE]).toBeUndefined())
      expect(restored).toEqual([])
      expect(chat().messages.some(message => message.role === 'error')).toBe(false)
    } finally {
      unregister()
      globalStore.dispatch(resolveQuestionCard({ ask_id: 'ask-snapshot-answered', settled: true }))
    }
  })

  it('replaces a held stateless card with the one the server now lists', async () => {
    const held = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-old',
      questions: [{ question: 'Old', options: [{ label: 'x' }] }],
    })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { card_id: 'card-new', slot: ACTIVE, questions: [{ question: 'New', options: [{ label: 'y' }] }] },
    ])
    act(() => {
      globalStore.dispatch(held)
      testStore.dispatch(held as never)
    })
    mount()
    await act(async () => { await Promise.resolve() })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-new')
    expect(chat().pendingQuestions[ACTIVE]?.questions[0].question).toBe('New')
  })

  it('keeps a held stateless card the server still lists', async () => {
    // Reload with the same card pending: the snapshot confirms it, so the drop
    // side must leave it alone. A live set that only collected ask_ids would
    // report every stateless card stale and wipe a live question.
    const held = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-live',
      questions: [{ question: 'Still asking', options: [{ label: 'x' }] }],
    })
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { card_id: 'card-live', slot: ACTIVE, questions: [{ question: 'Still asking', options: [{ label: 'x' }] }] },
    ])
    act(() => {
      globalStore.dispatch(held)
      testStore.dispatch(held as never)
    })
    const entry = chat().pendingQuestions[ACTIVE]
    expect(entry?.serverCardId).toBe('card-live')
    mount()
    await act(async () => { await Promise.resolve() })
    // The SAME entry, not a drop-and-re-add: a replaced entry would reset the
    // draft protection and let a reconnect discard a half-typed answer.
    expect(chat().pendingQuestions[ACTIVE]).toBe(entry)
  })

  it('restores a stateless card when a queued answer is cancelled', async () => {
    // The card is cleared optimistically on submit. If the answer was QUEUED and
    // the user then cancels it, nothing ever lands — so without this the slot
    // keeps reporting needs_input with nothing on screen to answer or dismiss.
    // The server still holds the record, so the question comes back.
    // Two Onces, not mockResolvedValue: a persistent mock would leak this
    // snapshot into every later test in the file. The first serves the mount's
    // sync, the second the cancel's.
    const snapshot = [
      { card_id: 'card-unanswered', slot: ACTIVE, questions: [{ question: 'Which region?', options: [{ label: 'us-east-1' }] }] },
    ]
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(snapshot)
      .mockResolvedValueOnce(snapshot)
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    // Simulate the answered-then-cleared state this window would be in.
    act(() => {
      globalStore.dispatch(resolveQuestionCard({ card_id: 'card-unanswered' }))
      testStore.dispatch(resolveQuestionCard({ card_id: 'card-unanswered' }) as never)
    })
    expect(chat().pendingQuestions[ACTIVE]).toBeUndefined()

    await act(async () => {
      ws.simulateMessage({ type: 'queue_cancel', data: { slot: ACTIVE, queue_id: 'q-1' } })
      await Promise.resolve()
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-unanswered')
  })

  it('never overwrites a card that arrived while the request was in flight', async () => {
    // The response describes the server as it was when the request was served, so
    // one card per slot means adding its row would replace a NEWER live card. The
    // deferred promise is load-bearing: the dispatch has to land strictly between
    // the fetch starting and its resolution, or the test passes on ordering luck
    // instead of on the guard.
    let release: (v: unknown) => void = () => {}
    ;(api.pendingQuestions as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((res) => { release = res }),
    )
    const live = setQuestionCard({
      slot: ACTIVE,
      card_id: 'card-new',
      questions: [{ question: 'Live', options: [{ label: 'y' }] }],
    })
    mount()
    // Both stores: the hook reads the module store for its snapshots (like the
    // rest of this file's setup) and dispatches into the Provider's test store.
    act(() => {
      globalStore.dispatch(live)
      testStore.dispatch(live as never)
    })
    await act(async () => {
      release([{ card_id: 'card-old', slot: ACTIVE, questions: [{ question: 'Stale', options: [{ label: 'x' }] }] }])
      await Promise.resolve()
    })
    expect(chat().pendingQuestions[ACTIVE]?.serverCardId).toBe('card-new')
    expect(chat().pendingQuestions[ACTIVE]?.questions[0].question).toBe('Live')
  })
})
