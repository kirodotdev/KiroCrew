/**
 * `/chat?new=1&prefill=<text>` — the cold-URL deep link an external launcher (a
 * Slack card, a browser bookmarklet, a CLI `--open`) can build to hand the
 * dashboard a pre-drafted prompt.
 *
 * Before this, `?new=1` created a blank session and `?prefill=` was honoured only
 * alongside an existing `?sid=`, so a launcher had no way in: the only cold path
 * that could both create a session AND carry a prompt was the signed `?token=`
 * channel flow, which an external tool cannot mint.
 *
 * Two properties are load-bearing and both are pinned here.
 *  1. It SEEDS ONLY. Nothing is sent — the human still presses Enter — so the
 *     link adds no way to spend a model turn.
 *  2. It requires the explicit `new=1` marker. A bare `?prefill=<v>` is an in-app
 *     SENTINEL in this dashboard (the file explorer's "Chat about this file" uses
 *     `?prefill=1`, a project idea's "Edit in chat" uses `?prefill=plan`, both
 *     with the real text riding Redux `pendingInput`), so honouring one would
 *     spawn a spurious empty session and type the marker into it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, act, waitFor, screen, fireEvent } from '@testing-library/react'
import { store as singletonStore, type RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { switchSlot, endLocalTurn, setQuestionCard, setQuestionDraft, setFolderSuggestion, pendingQuestionFor } from '../store/chatSlice'
import dashboardReducer, { updateSlot } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import { DRAFTS_KEY, saveDrafts, setDraft, __resetForTests as resetDrafts } from '../utils/chatDrafts'
import { loadFileDrafts, saveFileDrafts } from '../utils/chatFileDrafts'
import { loadPasteDrafts, savePasteDrafts, __resetForTests as resetPasteDrafts } from '../utils/chatPasteDrafts'
import { loadSessionRefDrafts, saveSessionRefDrafts, __resetSessionRefDraftsForTests as resetSessionRefs } from '../utils/chatSessionRefDrafts'
import { formatToken } from '../utils/pasteTokens'
import type { useKnowledgeFetch } from '../pages/chat/useKnowledgeFetch'

let knowledgeInTest: ReturnType<typeof useKnowledgeFetch>
vi.mock('../pages/chat/useKnowledgeFetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../pages/chat/useKnowledgeFetch')>()
  return {
    ...actual,
    useKnowledgeFetch: (...args: Parameters<typeof useKnowledgeFetch>) => {
      knowledgeInTest = actual.useKnowledgeFetch(...args)
      return knowledgeInTest
    },
  }
})

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const createChatSlot = vi.fn()
const sendChat = vi.fn()
const chatSlotDetail = vi.fn()
const answerQuestion = vi.fn()
const cancelQueuedRequest = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    answerQuestion: (...a: unknown[]) => answerQuestion(...a),
    cancelQueuedMessage: (...a: unknown[]) => cancelQueuedRequest(...a),
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: (...a: unknown[]) => chatSlotDetail(...a),
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
    createChatSlot: (...a: unknown[]) => createChatSlot(...a),
    deleteChatSlot: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotContext: vi.fn().mockResolvedValue({ ok: true }),
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
// The composer's sketch dialog pulls in the excalidraw bundle, which this test
// never opens and which costs seconds to transform.
vi.mock('../components/SketchDialog', () => ({ default: () => null }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

const PROMPT = 'Draft the release note for the ACP retry backoff'
const NEW_SLOT = 'chat-new-1'

/** `activeSlot: null` with one known session — the state a cold `/chat?new=1`
 *  load lands in, and the one where a spurious create would be visible. */
function makeStore(targetTitle?: string, activeSlot: string | null = null) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [
          { key: 'chat-other', title: targetTitle, messages: 2, running: false, mode: '', pending_approval: false, waiting_for_input: false },
          { key: 'chat-old', messages: 3, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [], switchSlotGone: null,
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false, followups: {},
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

let navigateInTest: ReturnType<typeof useNavigate>
let locationInTest: ReturnType<typeof useLocation>
function NavigationProbe() {
  navigateInTest = useNavigate()
  locationInTest = useLocation()
  return null
}

async function renderAt(route: string, targetTitle?: string, activeSlot?: string) {
  const store = makeStore(targetTitle, activeSlot)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={[route]}><NavigationProbe /><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  return store
}

const composer = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

afterEach(() => { vi.restoreAllMocks() })

beforeEach(() => {
  delete (window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch
  sessionStorage.clear()
  localStorage.clear()
  resetDrafts()
  resetPasteDrafts()
  resetSessionRefs()
  createChatSlot.mockReset()
  chatSlotDetail.mockReset()
  answerQuestion.mockReset()
  cancelQueuedRequest.mockReset()
  cancelQueuedRequest.mockResolvedValue({ ok: true })
  answerQuestion.mockResolvedValue({ ok: true })
  chatSlotDetail.mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 })
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
  createChatSlot.mockResolvedValue({ key: NEW_SLOT, title: NEW_SLOT, messages: 0, running: false })
})

describe('ChatPage — /chat?new=1&prefill= seeds a fresh session', { timeout: 20_000 }, () => {
  it('creates the session and seeds its composer with the linked prompt', async () => {
    const store = await renderAt(`/chat?new=1&prefill=${encodeURIComponent(PROMPT)}`)

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    // The staged value is consumed, not left behind for the next slot switch.
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('sends nothing — the prompt waits for a human Enter', async () => {
    await renderAt(`/chat?new=1&prefill=${encodeURIComponent(PROMPT)}`)
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('still creates a plain blank session when ?prefill= is empty', async () => {
    const store = await renderAt('/chat?new=1&prefill=')

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    expect(composer().value).toBe('')
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('ignores a bare ?prefill=1 with no new=1, because that is an in-app sentinel', async () => {
    // The file explorer's "Chat about this file" navigates to /chat?prefill=1 and
    // passes the real text through Redux `pendingInput`. Creating a session here
    // and typing "1" into it is the failure this guard exists for.
    await renderAt('/chat?prefill=1')

    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    expect(createChatSlot).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })

  it('ignores a bare ?prefill=<text> with no new=1, so the launcher URL must say what it wants', async () => {
    await renderAt(`/chat?prefill=${encodeURIComponent(PROMPT)}`)

    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    expect(createChatSlot).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
  })
})


describe('App SDK chat launch intent', () => {
  function launch(options: { message: string; slotKey?: string; autoSend?: boolean; agent?: string }) {
    ;(window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch = { ...options, ts: Date.now() }
  }

  it.each([true, false])('honors a cold app target over the carried active slot (autoSend=%s)', async (autoSend) => {
    launch({ message: PROMPT, slotKey: 'chat-other', autoSend })
    const store = await renderAt('/chat?sid=chat-other', 'Target notes', 'chat-old')
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    expect(store.getState().chat.activeSlot).toBe('chat-other')
    expect(chatSlotDetail.mock.calls.map(call => call[0])).toEqual(['chat-other'])
    if (autoSend) {
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      expect(sendChat.mock.calls[0]).toContain(PROMPT)
      expect(sendChat.mock.calls[0]).toContain('chat-other')
    } else {
      await waitFor(() => expect(composer().value).toBe(PROMPT))
      expect(sendChat).not.toHaveBeenCalled()
    }
    expect(createChatSlot).not.toHaveBeenCalled()
  })

  it('seeds an existing slot without sending or creating a replacement', async () => {
    launch({ message: PROMPT, slotKey: 'chat-old', autoSend: false })
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-old'))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(sendChat).not.toHaveBeenCalled()
    expect(createChatSlot).not.toHaveBeenCalled()
  })

  it.each(['cold', 'hot'])('preserves an existing draft on a %s targeted draft launch', async (entry) => {
    const existing = 'Keep my unsent notes  '
    const merged = `${existing}\n\n${PROMPT}`
    const drafts: Record<string, string> = {}
    setDraft(drafts, 'chat-other', existing)
    saveDrafts(drafts)
    let store: ReturnType<typeof makeStore>
    if (entry === 'cold') {
      launch({ message: PROMPT, slotKey: 'chat-other', autoSend: false })
      store = await renderAt('/chat?sid=chat-other')
    } else {
      store = await renderAt('/chat?sid=chat-old')
      await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
      launch({ message: PROMPT, slotKey: 'chat-other', autoSend: false })
      await act(async () => { navigateInTest('/chat?sid=chat-other') })
    }
    await waitFor(() => expect(composer().value).toBe(merged))
    await waitFor(() => expect(JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}')['chat-other']).toBe(merged))
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
    await act(async () => { await store.dispatch(switchSlot('chat-old')) })
    await act(async () => { await store.dispatch(switchSlot('chat-other')) })
    await waitFor(() => expect(composer().value).toBe(merged))
    expect(sendChat).not.toHaveBeenCalled()
    expect(createChatSlot).not.toHaveBeenCalled()
  })

  it('creates a fresh draft without spending a model turn', async () => {
    launch({ message: PROMPT, autoSend: false })
    const store = await renderAt('/chat?new=1')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('sends to the explicitly selected existing slot, not a new session', async () => {
    launch({ message: PROMPT, slotKey: 'chat-old' })
    await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(sendChat.mock.calls[0]).toContain('chat-old')
    expect(createChatSlot).not.toHaveBeenCalled()
  })

  it.each([
    { kind: 'blocking', queued: false },
    { kind: 'blocking', queued: true },
    { kind: 'stateless', queued: false },
    { kind: 'stateless-draft', queued: false },
  ].flatMap(params => [true, false].map(isolated => ({ ...params, isolated }))))('keeps card actions human-owned ($kind, queued=$queued, isolated=$isolated)', async ({ kind, queued, isolated }) => {
    const slot = 'chat-old'
    const store = await renderAt(`/chat?sid=${slot}`)
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    vi.spyOn(singletonStore, 'getState').mockImplementation(store.getState)
    const questions = [{ question: 'Which task should run?', options: [{ label: 'Keep waiting' }] }]
    await act(async () => {
      store.dispatch(setQuestionCard({ slot, questions, ...(kind === 'blocking' ? { ask_id: 'ask-human' } : {}) }))
      store.dispatch(setFolderSuggestion({ slot, folderId: 'folder-notes', folderName: 'Notes', breadcrumb: 'Notes', ts: 100 }))
      if (kind === 'stateless-draft') store.dispatch(setQuestionDraft({ slot, active: true }))
    })
    const card = pendingQuestionFor(store.getState().chat.pendingQuestions, slot)
    const folder = store.getState().chat.folderSuggestions[slot]
    sendChat.mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true, ...(queued ? { queued: true, queue_id: 'app-queued' } : {}) }) })
    if (isolated) {
      launch({ message: PROMPT, slotKey: slot })
      await act(async () => { navigateInTest(`/chat?sid=${slot}`) })
    } else {
      fireEvent.change(composer(), { target: { value: PROMPT } })
      await act(async () => { fireEvent.keyDown(composer(), { key: 'Enter' }) })
    }
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await act(async () => { await Promise.resolve() })
    if (isolated) {
      expect(answerQuestion).not.toHaveBeenCalled()
      expect(pendingQuestionFor(store.getState().chat.pendingQuestions, slot)).toEqual(card)
      expect(store.getState().chat.folderSuggestions[slot]).toEqual(folder)
    } else {
      if (kind === 'blocking') expect(answerQuestion).toHaveBeenCalledWith('ask-human')
      expect(pendingQuestionFor(store.getState().chat.pendingQuestions, slot)).toBeNull()
      expect(store.getState().chat.folderSuggestions[slot]?.turns).toBe(queued ? 0 : 1)
    }
  })

  it.each(['blocking', 'stateless'])('keeps replacement cards when an app receipt lands after switching (%s)', async (kind) => {
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    vi.spyOn(singletonStore, 'getState').mockImplementation(store.getState)
    const questions = [{ question: 'Which task?', options: [{ label: 'Wait' }] }]
    await act(async () => {
      store.dispatch(setQuestionCard({ slot: 'chat-old', questions, ...(kind === 'blocking' ? { ask_id: 'ask-old' } : {}) }))
    })
    let settle!: (response: unknown) => void
    sendChat.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
    launch({ message: PROMPT, slotKey: 'chat-old' })
    await act(async () => { navigateInTest('/chat?sid=chat-old') })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await act(async () => {
      store.dispatch(setQuestionCard({ slot: 'chat-old', questions, fresh: true, ...(kind === 'blocking' ? { ask_id: 'ask-replacement' } : {}) }))
      await store.dispatch(switchSlot('chat-other'))
      store.dispatch(setQuestionCard({ slot: 'chat-other', questions, ask_id: 'ask-other' }))
    })
    const pending = store.getState().chat.pendingQuestions
    await act(async () => { settle({ ok: true, json: async () => ({ ok: true }) }) })
    expect(answerQuestion).not.toHaveBeenCalled()
    expect(store.getState().chat.pendingQuestions).toEqual(pending)
  })

  it.each(['accepted', 'refused', 'busy-refused', 'transport-error', 'queued', 'new-session', 'create-refused', 'create-refused-switched'])('isolates app sends from staged composer context (%s)', async (outcome) => {
    const slot = 'chat-old'
    const files = ['/sample/unsent.txt', '/sample/unsent.png']
    const pastes = [{ id: 'private-paste', seq: 1, lines: 3, content: 'Private\npaste\ncontents' }]
    const refs = [{ key: 'another-session', title: 'Unsent reference' }]
    const draft = `Keep these notes  ${formatToken(pastes[0])}`
    const textDrafts: Record<string, string> = {}
    setDraft(textDrafts, slot, draft)
    saveDrafts(textDrafts)
    saveFileDrafts({ [slot]: files })
    savePasteDrafts({ [slot]: pastes })
    saveSessionRefDrafts({ [slot]: refs })
    const store = await renderAt(`/chat?sid=${slot}`)
    await waitFor(() => expect(composer().value).toBe(draft))
    await act(async () => {
      knowledgeInTest.inject([{ id: 'private-doc', title: 'Unsent knowledge', source: null, match_type: 'fts', tokens: 5, summary: '', content: 'Private knowledge contents' }])
    })
    await waitFor(() => expect(knowledgeInTest.pendingKnowledge?.items).toHaveLength(1))
    let settle!: (response: unknown) => void
    let fail!: (error: Error) => void
    sendChat.mockImplementationOnce(() => new Promise((resolve, reject) => { settle = resolve; fail = reject }))
    // A matching paste marker must remain literal, never resolve through a
    // different message's staged paste. Files and refs have no inline marker.
    const message = `App text ${formatToken(pastes[0])}`
    const refused = outcome.endsWith('refused')
    const createRefused = outcome.startsWith('create-refused')
    const newSession = outcome === 'new-session' || createRefused
    if (createRefused) createChatSlot.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
    if (outcome === 'busy-refused') {
      // send() reads the routed singleton; match it to this test's Provider.
      vi.spyOn(singletonStore, 'getState').mockImplementation(store.getState)
      await act(async () => { store.dispatch(updateSlot({ key: slot, subagents_running: 1 })) })
      chatSlotDetail.mockResolvedValueOnce({ messages: [], running: true, has_more: false, total: 0 })
    }
    launch({ message, slotKey: newSession ? undefined : slot })
    await act(async () => { navigateInTest(newSession ? '/chat' : `/chat?sid=${slot}`) })
    if (createRefused) {
      await waitFor(() => expect(createChatSlot).toHaveBeenCalledTimes(1))
      if (outcome === 'create-refused-switched') {
        await act(async () => { await store.dispatch(switchSlot('chat-other')) })
        await act(async () => { fireEvent.change(composer(), { target: { value: 'Other session draft' } }) })
      }
      await act(async () => { fail(new Error('create unavailable')) })
      expect(sendChat).not.toHaveBeenCalled()
      expect(JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}')[slot]).toBe(draft)
      const originMessages = store.getState().chat.activeSlot === slot
        ? store.getState().chat.messages
        : store.getState().chat.slotMessages[slot]
      expect(originMessages.some(m => m.role === 'user' && m.content === message)).toBe(false)
      expect(screen.getByTestId('action-error').textContent).toContain(message)
      if (outcome === 'create-refused-switched') {
        expect(composer().value).toBe('Other session draft')
        await act(async () => { await store.dispatch(switchSlot(slot)) })
      }
    } else {
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      await act(async () => {
        if (outcome === 'transport-error') fail(new TypeError('network unavailable'))
        else settle({ ok: !refused, status: refused ? 403 : 200, json: async () => ({ ok: !refused, queued: outcome === 'queued', queue_id: outcome === 'queued' ? 'app-queue' : undefined }) })
      })
      expect(sendChat.mock.calls[0][0]).toBe(message)
      expect(sendChat.mock.calls[0][1]).toBe(outcome === 'new-session' ? NEW_SLOT : slot)
      const meta = sendChat.mock.calls[0][4]
      expect(meta).toEqual({ sendId: expect.any(String) })
    }
    if (outcome === 'new-session') await act(async () => { await store.dispatch(switchSlot(slot)) })
    await waitFor(() => expect(composer().value).toBe(draft))
    expect(knowledgeInTest.pendingKnowledge?.items[0].content).toBe('Private knowledge contents')
    expect(loadFileDrafts()[slot]).toEqual(files)
    expect(loadPasteDrafts()[slot]).toEqual(pastes)
    expect(loadSessionRefDrafts()[slot]).toEqual(refs)
    expect(JSON.parse(localStorage.getItem(DRAFTS_KEY) ?? '{}')[slot]).toBe(draft)
    if (outcome === 'queued') {
      // A queued receipt leaves the draft untouched (asserted above). The
      // user's later button click explicitly asks to move this card to input.
      await act(async () => {
        store.dispatch({ type: 'chat/appendQueuedMessage', payload: { slot, content: message, ts: 'app-queue-ts', queueId: 'app-queue' } })
      })
      const cancel = await screen.findByRole('button', { name: 'Cancel queued message' })
      expect(cancel).toHaveAttribute('title', 'Cancel and move back to input')
      expect(cancelQueuedRequest).not.toHaveBeenCalled()
      expect(composer().value).toBe(draft)
      await act(async () => { fireEvent.click(cancel) })
      await waitFor(() => expect(composer().value).toBe(`${draft}\n\n${message}`))
      expect(cancelQueuedRequest).toHaveBeenCalledWith(slot, 'app-queue')
      expect(sendChat).toHaveBeenCalledTimes(1)
      expect(knowledgeInTest.pendingKnowledge?.items[0].content).toBe('Private knowledge contents')
      expect(loadFileDrafts()[slot]).toEqual(files)
      expect(loadPasteDrafts()[slot]).toEqual(pastes)
      expect(loadSessionRefDrafts()[slot]).toEqual(refs)
    }
    if (outcome === 'busy-refused') expect(store.getState().chat.messages.some(m => m.role === 'user')).toBe(false)
    if (createRefused || refused || outcome === 'transport-error') {
      expect(screen.getByTestId('action-error').textContent).toContain(message)
      await act(async () => { await store.dispatch(switchSlot('chat-other')) })
      await act(async () => { await store.dispatch(switchSlot(slot)) })
      expect(screen.getByTestId('action-error').textContent).toContain(message)
      expect(composer().value).toBe(draft)
    }
    if (createRefused) {
      sendChat.mockReset()
      sendChat.mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
      await act(async () => { fireEvent.keyDown(composer(), { key: 'Enter', code: 'Enter' }) })
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      expect(createChatSlot).toHaveBeenCalledTimes(1)
      expect(sendChat.mock.calls[0][1]).toBe(slot)
      expect(sendChat.mock.calls[0][0]).not.toContain('App text')
    }
    if (outcome === 'accepted') {
      await act(async () => { store.dispatch(endLocalTurn(slot)) })
      await act(async () => { fireEvent.keyDown(composer(), { key: 'Enter', code: 'Enter' }) })
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
      const humanText = sendChat.mock.calls[1][0]
      expect(humanText).toContain('Private knowledge contents')
      expect(humanText).toContain('Private\npaste\ncontents')
      expect(humanText).toContain('/sample/unsent.txt')
      expect(humanText).toContain('/sample/unsent.png')
      expect(humanText).toContain('another-session')
    }
  })

  it('retains a failed cold app launch without a slot or an automatic retry', async () => {
    createChatSlot.mockRejectedValueOnce(new Error('create unavailable'))
    launch({ message: PROMPT })
    await renderAt('/chat')
    await waitFor(() => expect(screen.getByTestId('action-error').textContent).toContain(PROMPT))
    expect(sendChat).not.toHaveBeenCalled()
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    await act(async () => { navigateInTest('/chat?sid=chat-old') })
    await waitFor(() => expect(composer().value).toBe(''))
    expect(screen.getByTestId('action-error').textContent).toContain(PROMPT)
    expect(createChatSlot).toHaveBeenCalledTimes(1)
  })

  it('handles a fresh draft launch while ChatPage is already mounted', async () => {
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-old'))
    launch({ message: PROMPT, autoSend: false, agent: 'example-agent' })
    await act(async () => { navigateInTest('/chat?new=1') })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    expect(createChatSlot.mock.calls[0]).toContain('example-agent')
    expect(sendChat).not.toHaveBeenCalled()
  })

  it.each([true, false])('activates another slot on a hot launch (autoSend=%s)', async (autoSend) => {
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    expect(store.getState().chat.activeSlot).toBe('chat-old')
    chatSlotDetail.mockClear()
    launch({ message: PROMPT, slotKey: 'chat-other', autoSend })
    await act(async () => { navigateInTest('/chat?sid=chat-other') })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-other'))
    if (autoSend) {
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      expect(sendChat.mock.calls[0]).toContain('chat-other')
    } else {
      await waitFor(() => expect(composer().value).toBe(PROMPT))
      expect(sendChat).not.toHaveBeenCalled()
    }
    expect(chatSlotDetail).toHaveBeenCalledTimes(1)
    expect(createChatSlot).not.toHaveBeenCalled()
  })

  it('keeps a claimed target message while activation outlasts the launch TTL', async () => {
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    let finish!: (value: unknown) => void
    chatSlotDetail.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const now = Date.now()
    launch({ message: PROMPT, slotKey: 'chat-other' })
    await act(async () => { navigateInTest('/chat?sid=chat-other') })
    expect(store.getState().chat.slotLoading).toBe(true)
    expect(sendChat).not.toHaveBeenCalled()
    const clock = vi.spyOn(Date, 'now').mockReturnValue(now + 11_000)
    try {
      await act(async () => { finish({ messages: [], running: false, has_more: false, total: 0 }) })
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      expect(sendChat.mock.calls[0]).toContain('chat-other')
    } finally {
      clock.mockRestore()
    }
  })

  it('does not send a slow launch after the user chooses another slot', async () => {
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    let finish!: (value: unknown) => void
    chatSlotDetail.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    launch({ message: PROMPT, slotKey: 'chat-other' })
    await act(async () => { navigateInTest('/chat?sid=chat-other') })
    expect(store.getState().chat.slotLoading).toBe(true)
    await act(async () => { await store.dispatch(switchSlot('chat-old')) })
    await act(async () => { finish({ messages: [], running: false, has_more: false, total: 0 }) })
    expect(store.getState().chat.activeSlot).toBe('chat-old')
    expect(sendChat).not.toHaveBeenCalled()
    expect(composer().value).toBe('')
    expect(screen.getByTestId('action-error').textContent).toContain(PROMPT)
    expect(screen.getByTestId('action-error').textContent).toContain('Send cancelled because you switched sessions.')
    expect(screen.getByTestId('action-error').textContent).toContain('Message not sent. Copy it to try again:')
  })

  it('keeps only the newer launch when target loads finish out of order', async () => {
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    let finish!: (value: unknown) => void
    chatSlotDetail.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    launch({ message: 'Older launch', slotKey: 'chat-other' })
    await act(async () => { navigateInTest('/chat?sid=chat-other') })
    launch({ message: PROMPT, slotKey: 'chat-old' })
    await act(async () => { navigateInTest('/chat?sid=chat-old') })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await act(async () => { finish({ messages: [], running: false, has_more: false, total: 0 }) })
    expect(sendChat).toHaveBeenCalledTimes(1)
    expect(sendChat.mock.calls[0]).toContain('chat-old')
    expect(sendChat.mock.calls[0]).toContain(PROMPT)
  })

  it.each([true, false])('restores URL sync when a new launch supersedes a slow target (autoSend=%s)', async (autoSend) => {
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    let finish!: (value: unknown) => void
    chatSlotDetail.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    launch({ message: 'Older launch', slotKey: 'chat-other' })
    await act(async () => { navigateInTest('/chat?sid=chat-other') })
    launch({ message: PROMPT, autoSend })
    await act(async () => { navigateInTest(autoSend ? '/chat' : '/chat?new=1') })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await act(async () => { finish({ messages: [], running: false, has_more: false, total: 0 }) })
    await waitFor(() => expect(new URLSearchParams(locationInTest.search).get('sid')).toBe(NEW_SLOT))
    await act(async () => { await store.dispatch(switchSlot('chat-old')) })
    await waitFor(() => expect(new URLSearchParams(locationInTest.search).get('sid')).toBe('chat-old'))
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    expect(sendChat).toHaveBeenCalledTimes(autoSend ? 1 : 0)
    if (autoSend) expect(sendChat.mock.calls[0]).toContain(PROMPT)
  })

  it.each([
    { status: 404, title: undefined },
    { status: 500, title: undefined },
    { status: 404, title: 'Saved notes' },
    { status: 500, title: 'Saved notes' },
  ].flatMap(params => [true, false].map(autoSend => ({ ...params, autoSend }))))('shows failed target activation without sending (HTTP $status, title=$title, autoSend=$autoSend)', async ({ status, title, autoSend }) => {
    const store = await renderAt('/chat?sid=chat-old', title)
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    chatSlotDetail.mockRejectedValueOnce(Object.assign(new Error('target load failed'), { status }))
    launch({ message: PROMPT, slotKey: 'chat-other', autoSend })
    await act(async () => { navigateInTest('/chat?sid=chat-other') })
    await waitFor(() => expect(screen.getByTestId('sid-error').textContent).toContain(title
      ? `Couldn't open "${title}". Try again.`
      : 'Could not open the target session.'))
    expect(screen.getByTestId('sid-error').textContent).toContain('Message not sent. Copy it to try again:')
    expect(screen.getByTestId('sid-error').textContent).not.toContain('chat-other')
    expect(screen.getByTestId('sid-error').textContent).toContain(PROMPT)
    expect(store.getState().chat.switchSlotGone).toBeNull()
    expect(sendChat).not.toHaveBeenCalled()
    expect(createChatSlot).not.toHaveBeenCalled()
    expect((window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch).toBeUndefined()
  })

  it('does not send an intent whose target was not activated', async () => {
    launch({ message: PROMPT, slotKey: 'some-other-slot' })
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-old'))
    expect(sendChat).not.toHaveBeenCalled()
    expect(createChatSlot).not.toHaveBeenCalled()
  })

  it('preserves the default new-session autosend behavior', async () => {
    launch({ message: PROMPT })
    await renderAt('/chat')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    expect(sendChat.mock.calls[0]).toContain(NEW_SLOT)
  })
})
