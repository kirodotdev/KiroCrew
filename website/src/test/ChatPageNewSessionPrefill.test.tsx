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
import { render, act, waitFor, screen, fireEvent, cleanup } from '@testing-library/react'
import { store as singletonStore, type RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { configureStore, type Middleware } from '@reduxjs/toolkit'
import { flushSync } from 'react-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { createSlot, switchSlot, endLocalTurn, setQuestionCard, setQuestionDraft, setFolderSuggestion, pendingQuestionFor, resolveQuestionCard } from '../store/chatSlice'
import dashboardReducer, { markSlotUnread, sseConnected, sseDisconnected, sseSlots, updateSlot } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import { DRAFTS_KEY, saveDrafts, setDraft, __resetForTests as resetDrafts } from '../utils/chatDrafts'
import { loadFileDrafts, saveFileDrafts } from '../utils/chatFileDrafts'
import { loadPasteDrafts, savePasteDrafts, __resetForTests as resetPasteDrafts } from '../utils/chatPasteDrafts'
import { loadSessionRefDrafts, saveSessionRefDrafts, __resetSessionRefDraftsForTests as resetSessionRefs } from '../utils/chatSessionRefDrafts'
import { formatToken } from '../utils/pasteTokens'
import { openPopout, __resetForTests as resetChatPopouts } from '../utils/chatPopout'
import { performSlotSwitch } from '../lib/slotSwitch'
import { captureUrlToken } from '../lib/urlToken'
import { bindSlotReadSender, flushSlotRead, _resetSlotReadRelayForTest } from '../lib/slotReadRelay'
import { i18nT } from '../i18n/t'
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
const deleteChatSlot = vi.fn()
const discardChatSlot = vi.fn()
const slackLink = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    answerQuestion: (...a: unknown[]) => answerQuestion(...a),
    cancelQueuedMessage: (...a: unknown[]) => cancelQueuedRequest(...a),
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: (...a: unknown[]) => chatSlotDetail(...a),
    sendChat: (...a: unknown[]) => sendChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    dashboardConfig: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: (...a: unknown[]) => createChatSlot(...a),
    deleteChatSlot: (...a: unknown[]) => deleteChatSlot(...a),
    discardChatSlot: (...a: unknown[]) => discardChatSlot(...a),
    chatSlotContext: vi.fn().mockResolvedValue({ ok: true }),
    suggestions: vi.fn().mockResolvedValue({ suggestions: [] }),
    slackLink: (...a: unknown[]) => slackLink(...a),
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
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }), emitSlotFocused: () => {}, endSplitPaneFocus: () => {} }))
// The composer's sketch dialog pulls in the excalidraw bundle, which this test
// never opens and which costs seconds to transform.
vi.mock('../components/SketchDialog', () => ({ default: () => null }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'

const PROMPT = 'Draft the release note for the ACP retry backoff'
const NEW_SLOT = 'chat-new-1'

/** `activeSlot: null` with one known session — the state a cold `/chat?new=1`
 *  load lands in, and the one where a spurious create would be visible. */
function makeStore(targetTitle?: string, activeSlot: string | null = null, middleware: Middleware[] = []) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    middleware: getDefault => getDefault().concat(middleware),
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

async function renderAt(route: string, targetTitle?: string, activeSlot?: string, middleware?: Middleware[]) {
  const store = makeStore(targetTitle, activeSlot, middleware)
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
  deleteChatSlot.mockReset()
  deleteChatSlot.mockResolvedValue({ ok: true })
  discardChatSlot.mockReset()
  discardChatSlot.mockResolvedValue({ ok: true })
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

  it('seeds the composer when the activation commit runs before the create caller resumes', async () => {
    // In a browser the create's activation renders, and the slot-restore effect
    // reads the staged prefill, before the code awaiting the create runs again.
    // jsdom under act() runs that caller first, so a prompt staged by the caller
    // would still reach the composer there. Committing the activation inside the
    // dispatch reproduces the browser order.
    const stagedAtActivation: (string | null)[] = []
    const commitActivationNow: Middleware = () => next => action => {
      if (!createSlot.fulfilled.match(action)) return next(action)
      stagedAtActivation.push(sessionStorage.getItem(PREFILL_STORAGE_KEY))
      const result = next(action)
      flushSync(() => {})
      return result
    }
    const store = await renderAt(`/chat?new=1&prefill=${encodeURIComponent(PROMPT)}`, undefined, undefined, [commitActivationNow])

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    // The seed was staged before the activation that reads it, not after.
    expect(stagedAtActivation).toHaveLength(1)
    expect(JSON.parse(stagedAtActivation[0] ?? 'null')).toMatchObject({ slotKey: NEW_SLOT, prompt: PROMPT })
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('still seeds the composer when sessionStorage refuses the staged prompt', async () => {
    const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === PREFILL_STORAGE_KEY) throw new DOMException('full', 'QuotaExceededError')
      return setItem.call(this, key, value)
    })
    const commitActivationNow: Middleware = () => next => action => {
      const result = next(action)
      if (createSlot.fulfilled.match(action)) flushSync(() => {})
      return result
    }
    const store = await renderAt(`/chat?new=1&prefill=${encodeURIComponent(PROMPT)}`, undefined, undefined, [commitActivationNow])

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(sendChat).not.toHaveBeenCalled()
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
  function launch(options: {
    message: string; slotKey?: string; autoSend?: boolean; agent?: string; memoryMode?: string; sessionFolder?: boolean
  }) {
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

  it('keeps the full resend guidance when a launched send times out, since a launch raises no strip', async () => {
    sendChat.mockImplementation(async () => { throw new DOMException('aborted', 'AbortError') })
    launch({ message: PROMPT, slotKey: 'chat-old' })
    const store = await renderAt('/chat?sid=chat-old')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    const notice = await waitFor(() => {
      const row = store.getState().chat.messages.find(m => m.role === 'notice' && m.meta?.deliveryNoticeFor)
      expect(row).toBeDefined()
      return row!
    })
    expect(notice.content).toBe('\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed_resend_manually'))
    expect(screen.queryByTestId('delivery-exit-dismiss')).toBeNull()
  })

  it('shows a restored-answer notice on the session page after its question is gone', async () => {
    const slot = 'chat-old'
    const store = await renderAt(`/chat?sid=${slot}`)
    await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
    const questions = [{ question: 'Which region?', options: [{ label: 'us-east-1' }] }]
    await act(async () => {
      store.dispatch(setQuestionCard({ slot, questions, ask_id: 'ask-gone' }))
      store.dispatch(resolveQuestionCard({ ask_id: 'ask-gone', restored_notice: 'Your answers were not sent.' }))
    })
    expect(pendingQuestionFor(store.getState().chat.pendingQuestions, slot)).toBeNull()
    expect(await screen.findByText('Your answers were not sent.')).toBeInTheDocument()
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
      store.dispatch(setQuestionCard({ slot, questions, ...(kind === 'blocking' ? { ask_id: 'ask-human' } : { card_id: 'card-human' }) }))
      store.dispatch(setFolderSuggestion({ slot, folderId: 'folder-notes', folderName: 'Notes', breadcrumb: 'Notes', ts: 100 }))
      if (kind === 'stateless-draft') store.dispatch(setQuestionDraft({ slot, answers: { 'Which?': 'draft' } }))
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
    } else if (kind === 'blocking') {
      expect(answerQuestion).toHaveBeenCalledWith('ask-human', undefined, queued ? 'queued' : 'composer')
      expect(pendingQuestionFor(store.getState().chat.pendingQuestions, slot)).toBeNull()
      expect(store.getState().chat.folderSuggestions[slot]?.turns).toBe(queued ? 0 : 1)
    } else {
      // A stateless card is server-owned: the send itself does not
      // retire it. The server retires the record when the user row lands and
      // announces it with `question_card_resolved`, which is what takes the
      // card off screen (the mounted card reports no typed answer here, so the
      // draft flag set above was already cleared on mount; draft sparing is
      // pinned in chatSlice.questionCard.test.ts).
      expect(answerQuestion).not.toHaveBeenCalled()
      expect(pendingQuestionFor(store.getState().chat.pendingQuestions, slot)).toEqual(card)
      await act(async () => { store.dispatch(resolveQuestionCard({ card_id: 'card-human' })) })
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
      store.dispatch(setQuestionCard({ slot: 'chat-old', questions, ...(kind === 'blocking' ? { ask_id: 'ask-old' } : { card_id: 'card-old' }) }))
    })
    let settle!: (response: unknown) => void
    sendChat.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
    launch({ message: PROMPT, slotKey: 'chat-old' })
    await act(async () => { navigateInTest('/chat?sid=chat-old') })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await act(async () => {
      store.dispatch(setQuestionCard({ slot: 'chat-old', questions, ...(kind === 'blocking' ? { ask_id: 'ask-replacement' } : { card_id: 'card-replacement' }) }))
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

  // `api.createChatSlot`'s positional arguments the launch shapes.
  const CREATE_AGENT = 1, CREATE_MEMORY_MODE = 4, CREATE_SKIP_SESSION_FOLDER = 9

  it('creates a new draft session with the launch memory mode and no session folder', async () => {
    launch({ message: PROMPT, agent: 'example-agent', memoryMode: 'persistent', sessionFolder: false })
    const store = await renderAt('/chat?new=1')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    await waitFor(() => expect(composer().value).toBe(PROMPT))
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    const call = createChatSlot.mock.calls[0]
    expect(call[CREATE_AGENT]).toBe('example-agent')
    expect(call[CREATE_MEMORY_MODE]).toBe('persistent')
    expect(call[CREATE_SKIP_SESSION_FOLDER]).toBe(true)
    // A session-shaping launch is a draft: the message waits for the user.
    expect(sendChat).not.toHaveBeenCalled()
    expect((window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch).toBeUndefined()
  })

  it('ignores an unknown memory mode and keeps the default', async () => {
    launch({ message: PROMPT, autoSend: false, memoryMode: 'forever' })
    const store = await renderAt('/chat?new=1')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
    expect(createChatSlot).toHaveBeenCalledTimes(1)
    expect(createChatSlot.mock.calls[0][CREATE_MEMORY_MODE]).not.toBe('forever')
    expect(createChatSlot.mock.calls[0][CREATE_SKIP_SESSION_FOLDER]).toBe(false)
  })

  it('does not apply session-shaping options to a targeted launch', async () => {
    launch({ message: PROMPT, slotKey: 'chat-other', memoryMode: 'incognito', sessionFolder: false })
    const store = await renderAt('/chat?sid=chat-other', 'Target notes', 'chat-old')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-other'))
    // Still the targeted auto-send path: the existing session keeps its mode.
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(createChatSlot).not.toHaveBeenCalled()
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

  describe('into a session the list does not show yet', () => {
    const FRESH = 'chat-fresh'
    const listed = [
      { key: 'chat-other', messages: 2, running: false, mode: '', pending_approval: false, waiting_for_input: false },
      { key: 'chat-old', messages: 3, running: false, mode: '', pending_approval: false, waiting_for_input: false },
    ]
    const withFresh = [...listed, { key: FRESH, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false }]

    it('waits for the create broadcast instead of losing the target to its first 404', async () => {
      const store = await renderAt('/chat?sid=chat-old')
      await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
      chatSlotDetail.mockClear()
      // The fresh slot's detail is not served until the gateway has listed it.
      chatSlotDetail.mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
      launch({ message: PROMPT, slotKey: FRESH, autoSend: false })
      await act(async () => { navigateInTest(`/chat?sid=${FRESH}`) })
      expect(chatSlotDetail).not.toHaveBeenCalled()
      expect(store.getState().chat.activeSlot).toBe('chat-old')
      expect(new URLSearchParams(locationInTest.search).get('sid')).toBe(FRESH)
      chatSlotDetail.mockReset()
      chatSlotDetail.mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 })
      await act(async () => { store.dispatch(sseSlots(withFresh as never)) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe(FRESH))
      expect(chatSlotDetail.mock.calls.map(call => call[0])).toEqual([FRESH])
      await waitFor(() => expect(composer().value).toBe(PROMPT))
      expect(new URLSearchParams(locationInTest.search).get('sid')).toBe(FRESH)
      expect(screen.queryByTestId('sid-error')).toBeNull()
      expect(sendChat).not.toHaveBeenCalled()
    })

    it('switches anyway once the wait runs out, and reports a target that never appears', async () => {
      const store = await renderAt('/chat?sid=chat-old')
      await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
      chatSlotDetail.mockClear()
      chatSlotDetail.mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
      try {
        launch({ message: PROMPT, slotKey: FRESH, autoSend: false })
        await act(async () => { navigateInTest(`/chat?sid=${FRESH}`) })
        expect(chatSlotDetail).not.toHaveBeenCalled()
        await act(async () => { vi.advanceTimersByTime(5_000) })
      } finally {
        vi.useRealTimers()
      }
      await waitFor(() => expect(screen.getByTestId('sid-error').textContent).toContain('Could not open the target session.'))
      // The target is tried once; the unwind then reloads the outgoing chat.
      expect(chatSlotDetail.mock.calls.map(call => call[0])).toEqual([FRESH, 'chat-old'])
      expect(store.getState().chat.activeSlot).toBe('chat-old')
      expect(sendChat).not.toHaveBeenCalled()
    })

    it('gives way to a session the user picks while it waits', async () => {
      const store = await renderAt('/chat?sid=chat-old')
      await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
      launch({ message: PROMPT, slotKey: FRESH })
      await act(async () => { navigateInTest(`/chat?sid=${FRESH}`) })
      await act(async () => { await store.dispatch(switchSlot('chat-other')) })
      await waitFor(() => expect(new URLSearchParams(locationInTest.search).get('sid')).toBe('chat-other'))
      chatSlotDetail.mockClear()
      await act(async () => { store.dispatch(sseSlots(withFresh as never)) })
      expect(store.getState().chat.activeSlot).toBe('chat-other')
      expect(chatSlotDetail).not.toHaveBeenCalled()
      expect(sendChat).not.toHaveBeenCalled()
      expect((window as Window & { __mc_chat_launch?: unknown }).__mc_chat_launch).toBeUndefined()
      // The app's message is not dropped silently: the user gets it back to copy.
      await waitFor(() => expect(screen.getByTestId('action-error').textContent).toContain('Send cancelled because you switched sessions.'))
      expect(screen.getByTestId('action-error').textContent).toContain(PROMPT)
    })

    it('resumes a wait that a disconnect outlasts, and leaves URL sync working', async () => {
      const store = await renderAt('/chat?sid=chat-old')
      await waitFor(() => expect(store.getState().chat.slotLoading).toBe(false))
      chatSlotDetail.mockClear()
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
      try {
        launch({ message: PROMPT, slotKey: FRESH, autoSend: false })
        await act(async () => { navigateInTest(`/chat?sid=${FRESH}`) })
        await act(async () => { store.dispatch(sseDisconnected()) })
        // Past the 10s a staged launch stays claimable, before the socket is back.
        await act(async () => { vi.advanceTimersByTime(11_000) })
        expect(chatSlotDetail).not.toHaveBeenCalled()
        // The socket comes back with a slots frame that lists the target.
        await act(async () => {
          store.dispatch(sseConnected())
          store.dispatch(sseSlots(withFresh as never))
        })
      } finally {
        vi.useRealTimers()
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe(FRESH))
      await waitFor(() => expect(composer().value).toBe(PROMPT))
      await act(async () => { await store.dispatch(switchSlot('chat-other')) })
      await waitFor(() => expect(new URLSearchParams(locationInTest.search).get('sid')).toBe('chat-other'))
    })
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

  // A draft launch creates its session before the user types, so leaving it
  // unsent must not leave an empty session in the sidebar or History.
  // The leave check runs in a microtask the leaving commit queues, and the
  // discard request goes out synchronously inside it, so an awaited act around
  // the leave is what lets a "no discard" assertion read a settled answer.
  describe('a draft launch left unsent', () => {
    afterEach(() => { window.history.replaceState(null, '', '/') })

    const isListed = (store: ReturnType<typeof makeStore>) => store.getState().dashboard.slots.some(s => s.key === NEW_SLOT)

    async function openDraft(options: { memoryMode?: string; autoSend?: boolean }, middleware?: Middleware[]) {
      launch({ message: PROMPT, ...options })
      const store = await renderAt('/chat?new=1', undefined, undefined, middleware)
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
      await waitFor(() => expect(composer().value).toBe(PROMPT))
      return store
    }

    it.each([
      ['a memory mode', { memoryMode: 'persistent' }],
      ['autoSend: false', { autoSend: false }],
    ])('discards the session when the user opens another one (%s)', async (_label, options) => {
      const store = await openDraft(options)
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      await waitFor(() => expect(discardChatSlot).toHaveBeenCalledWith(NEW_SLOT))
      expect(discardChatSlot).toHaveBeenCalledTimes(1)
      expect(deleteChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(false)
      expect(sendChat).not.toHaveBeenCalled()
    })

    // The discarded session is the active one, and the user is on another page:
    // no other session is opened in its place, read here or relayed as read.
    it('discards the session when the user leaves the chat page, opening no other one', async () => {
      const actions: string[] = []
      const record: Middleware = () => next => action => {
        actions.push((action as { type: string }).type)
        return next(action)
      }
      const store = await openDraft({ memoryMode: 'persistent' }, [record])
      await act(async () => {
        store.dispatch(markSlotUnread('chat-other'))
        store.dispatch(markSlotUnread('chat-old'))
      })
      // An earlier case's switch can leave a throttled relay pending.
      _resetSlotReadRelayForTest()
      const relayed = vi.fn()
      bindSlotReadSender(relayed)
      try {
        actions.length = 0
        chatSlotDetail.mockClear()
        window.history.replaceState(null, '', '/apps/papyrus')
        await act(async () => { cleanup() })
        await waitFor(() => expect(discardChatSlot).toHaveBeenCalledWith(NEW_SLOT))
        await waitFor(() => expect(actions).toContain('chat/deleteSlot/fulfilled'))
        flushSlotRead()
        expect(deleteChatSlot).not.toHaveBeenCalled()
        expect(isListed(store)).toBe(false)
        expect(store.getState().chat.activeSlot).toBeNull()
        expect(actions.filter(type => type.startsWith('chat/switchSlot') || type === 'dashboard/markSlotRead')).toEqual([])
        expect(chatSlotDetail).not.toHaveBeenCalled()
        expect(store.getState().dashboard.unreadSlots).toEqual(expect.arrayContaining(['chat-other', 'chat-old']))
        expect(relayed).not.toHaveBeenCalled()
      } finally {
        bindSlotReadSender(() => {})
        _resetSlotReadRelayForTest()
      }
    })

    it('keeps the session when a fresh chat page is still showing it', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      window.history.replaceState(null, '', '/chat')
      await act(async () => { cleanup() })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })

    it('keeps the session once the user has sent in it', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      await act(async () => { fireEvent.keyDown(composer(), { key: 'Enter', code: 'Enter' }) })
      await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })

    it('keeps the session when the user wrote in its composer', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      await act(async () => { fireEvent.change(composer(), { target: { value: `${PROMPT}\n\nAnd cite the issue` } }) })
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })

    // The composer holds an edit storage refused, so the stored draft still
    // reads as the launch's message alone.
    it('keeps the session when the composer could not save the user\'s edit', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      const setItem = Storage.prototype.setItem
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
        if (key === DRAFTS_KEY) throw new DOMException('denied', 'SecurityError')
        return setItem.call(this, key, value)
      })
      await act(async () => { fireEvent.change(composer(), { target: { value: `${PROMPT}\n\nAnd cite the issue` } }) })
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })

    it('keeps the session when the user cleared its composer', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      await act(async () => { fireEvent.change(composer(), { target: { value: '' } }) })
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })

    it.each([
      ['renamed', { title: 'Release note notes' }],
      ['pinned', { pinned: true }],
      ['filed into a folder', { folder_id: 'folder-1' }],
      ['recoloured', { color_index: 3 }],
      ['moved to another model', { model: 'other-model' }],
      ['set to mute the sessions it opens', { mutes_opened: true }],
      ['moved in the manual order', { manual_rank: 2 }],
      // A field no list in the hook names counts as the user's too.
      ['changed in a field the leave check does not name', { some_future_setting: 'on' }],
    ])('keeps the session the user %s', async (_label, change) => {
      const store = await openDraft({ memoryMode: 'persistent' })
      await act(async () => { store.dispatch(updateSlot({ key: NEW_SLOT, ...change })) })
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })

    // Any act of the user's inside the chat pane keeps the session, whatever it
    // changed: a key press or a click that leaves no draft or setting behind.
    it.each([
      ['pressed a key in', (el: HTMLElement) => fireEvent.keyDown(el, { key: 'ArrowLeft', code: 'ArrowLeft' })],
      ['clicked in', (el: HTMLElement) => fireEvent.pointerDown(el)],
    ])('keeps the session once the user %s its chat pane', async (_label, act_) => {
      const store = await openDraft({ memoryMode: 'persistent' })
      await act(async () => { act_(composer()) })
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })

    // Leaving through the sidebar is a click outside the pane, which is no act on
    // the session.
    it('still discards the session after a click outside its chat pane', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      await act(async () => { fireEvent.pointerDown(document.body) })
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      await waitFor(() => expect(discardChatSlot).toHaveBeenCalledWith(NEW_SLOT))
      expect(isListed(store)).toBe(false)
    })

    // A model pick reaches the session's row only once its request succeeds; a
    // shortcut can make one with no event in the pane.
    it('keeps the session while a model pick for it is still on the wire', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      let answer: (model: string) => void = () => {}
      const pick = performSlotSwitch('model', NEW_SLOT, 'other-model', () => new Promise<string>((res) => { answer = res }), () => {})
      try {
        await act(async () => { await store.dispatch(switchSlot('chat-old')) })
        expect(discardChatSlot).not.toHaveBeenCalled()
        expect(isListed(store)).toBe(true)
      } finally {
        answer('other-model')
        await pick
      }
    })

    // A split pane keeps its composer apart from the stored drafts, so what the
    // user typed there is not what the leave check reads.
    it('keeps the session once split view has shown it', async () => {
      vi.mocked(api.dashboardConfig).mockResolvedValue({ session_grid: true })
      try {
        const store = await openDraft({ memoryMode: 'persistent' })
        await waitFor(() => expect(screen.getByLabelText(i18nT('pages.chatPage.enter_split_view'))).toBeTruthy())
        await act(async () => { fireEvent.keyDown(document, { key: 'd', ctrlKey: true }) })
        await act(async () => { await store.dispatch(switchSlot('chat-old')) })
        expect(discardChatSlot).not.toHaveBeenCalled()
        expect(isListed(store)).toBe(true)
      } finally {
        vi.mocked(api.dashboardConfig).mockResolvedValue({})
      }
    })

    // The session list's own row for the session carries fields the create's
    // answer does not, and the server fills in what it resolves on its own.
    it('still discards the session when only the server\'s own fields changed', async () => {
      const store = await openDraft({ memoryMode: 'persistent' })
      const listed = store.getState().dashboard.slots
      await act(async () => {
        store.dispatch(sseSlots(listed.map(s => s.key !== NEW_SLOT ? s : {
          ...s, served_model: 'served-1', effective_agent: '', model_withheld: false, row_identity: NEW_SLOT,
          incarnation: 'inc-1', subagents_running: false, subagents_queued: 0, parent: null,
          last_activity_ts: '2026-11-09T10:00:00Z', pin_rev: 4, manual_rev: 7, stop_state: 'idle' as const,
        })))
      })
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      await waitFor(() => expect(discardChatSlot).toHaveBeenCalledWith(NEW_SLOT))
      expect(isListed(store)).toBe(false)
    })

    // A pop-out window keeps its own staged files and session references, which
    // this window's stored drafts do not hold.
    it('keeps the session once it has been popped out', async () => {
      const popout = { focus: () => {}, close: () => {}, closed: false } as unknown as Window
      vi.spyOn(window, 'open').mockReturnValue(popout)
      try {
        const store = await openDraft({ memoryMode: 'persistent' })
        await act(async () => { openPopout(NEW_SLOT) })
        await act(async () => { await store.dispatch(switchSlot('chat-old')) })
        expect(discardChatSlot).not.toHaveBeenCalled()
        expect(isListed(store)).toBe(true)
      } finally {
        resetChatPopouts()
      }
    })

    it('leaves a plain new window alone', async () => {
      const store = await renderAt('/chat?new=1')
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_SLOT))
      await act(async () => { await store.dispatch(switchSlot('chat-old')) })
      expect(discardChatSlot).not.toHaveBeenCalled()
      expect(isListed(store)).toBe(true)
    })
  })
})


describe('Signed channel link', () => {
  /** Hand the boot-time token capture a signed chat link, the shape the channel redirect issues. */
  function openLink(payload: Record<string, unknown>) {
    const token = `${btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_')}.sig`
    captureUrlToken({
      location: { href: `http://localhost/chat?token=${token}` },
      history: { state: null, replaceState: () => {} },
    } as unknown as Pick<Window, 'location' | 'history'>)
  }

  beforeEach(() => {
    slackLink.mockReset()
    slackLink.mockResolvedValue({ ok: true })
  })

  it('sends the link prompt tagged as already in the channel thread, and nothing after it', async () => {
    openLink({ prompt: PROMPT, channel: 'C123', thread_ts: '1700.5' })
    await renderAt('/chat')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(slackLink).toHaveBeenCalledWith(NEW_SLOT, 'C123', '1700.5')
    const [message, slot, , , meta] = sendChat.mock.calls[0]
    expect(message).toBe(PROMPT)
    expect(slot).toBe(NEW_SLOT)
    // The gateway's mirror reads this to skip quoting the person's own message back.
    expect(meta).toMatchObject({ origin: 'channel_challenge' })

    // The next message is typed in the dashboard, so the thread gets its quote.
    await act(async () => { fireEvent.change(composer(), { target: { value: PROMPT } }) })
    await act(async () => { fireEvent.keyDown(composer(), { key: 'Enter', code: 'Enter' }) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
    expect(sendChat.mock.calls[1][4]).not.toHaveProperty('origin')
  })
})
