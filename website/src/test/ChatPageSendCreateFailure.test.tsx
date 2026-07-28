/**
 * A failed session-create must never eat the user's message.
 *
 * `send()` clears the composer (and deletes its drafts) BEFORE it creates the
 * session for a new-session send. The create used to be a bare
 * `.unwrap()`: when it rejected, send() unwound with the text already gone —
 * nothing sent, no error surfaced, no draft to recover, and `sendingRef` left
 * true. This pins the recovery: text, paste blocks and attachments come back,
 * an error message is shown, and nothing is sent.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const createChatSlot = vi.fn()
const sendChat = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
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

function makeStore(pendingInput = 'do not lose me') {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [
          { key: 'slot-a', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          { key: 'slot-b', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput,
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

/** Store with no active slot and no slot list — the pre-session window an
 *  auto-send can fire in, before slot auto-selection resolves. */
function makeSlotlessStore(pendingInput = 'do not lose me') {
  const store = makeStore(pendingInput)
  const state = store.getState()
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: { ...state.dashboard, slots: [], slotsLoaded: false } as unknown as RootState['dashboard'],
      chat: { ...state.chat, activeSlot: null, pendingInput } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

/** Render without waiting for the composer: with no active slot ChatPage renders an
 *  empty state instead, but the auto-send effect still fires. */
async function renderSlotless(store: ReturnType<typeof makeStore>, route = '/chat?autoSend=1&newSession=1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[route]}><ChatPage /></MemoryRouter>
        </ThemeProvider>
      </Provider>
      </QueryClientProvider>,
    )
  })
}

async function renderPage(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat?autoSend=1&newSession=1']}><ChatPage /></MemoryRouter>
        </ThemeProvider>
      </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  createChatSlot.mockReset()
  sendChat.mockReset()
  sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
})

describe('send() when creating the session fails', { timeout: 20_000 }, () => {
  it('restores the message and reports the failure instead of dropping it', async () => {
    createChatSlot.mockRejectedValue(new Error('gateway unavailable'))
    const store = makeStore()
    await renderPage(store)

    // `pendingInput` + ?autoSend=1&newSession=1 is the reachable forceNew path
    // (the "Chat" buttons on Projects / Dev Fleet / Prompts): it arms
    // newSessionRef and auto-sends, so send() takes the create branch.
    await act(async () => { await Promise.resolve() })

    // Nothing was sent...
    expect(sendChat).not.toHaveBeenCalled()
    // ...the text is recovered into the composer and persisted per-slot...
    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('do not lose me')
    })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')['slot-a']).toBe('do not lose me')
    })

    // ...and the failure is visible rather than silent.
    const errs = store.getState().chat.messages.filter(m => m.role === 'error')
    expect(errs.length).toBe(1)
    expect(errs[0].content).toContain('Could not start a new session')
  })

  it('merges into an existing composer draft instead of overwriting it', async () => {
    // The reachable forceNew path passes the prompt as optionText, so send()
    // never clears the composer: the user's own draft is still sitting there.
    // A failed create must not replace it.
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-a': 'my own unsent draft' }))
    createChatSlot.mockRejectedValue(new Error('gateway unavailable'))
    const store = makeStore()
    // renderPage's act() already drains the auto-send effect, so the failed
    // create (and the recovery) has happened by the time it returns.
    await renderPage(store)
    await act(async () => { await Promise.resolve() })

    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
        .toBe('my own unsent draft\n\ndo not lose me')
    })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')['slot-a'])
        .toBe('my own unsent draft\n\ndo not lose me')
    })
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('merges around text typed while a slow create was in flight (no duplication)', async () => {
    // A deferred rejection opens the real async window the instant-reject tests
    // cannot reach: the user keeps typing while createSlot is pending.
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementation(() => new Promise((_res, rej) => { rejectCreate = rej }))
    const store = makeStore()
    await renderPage(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: 'typed while waiting' } })
    await act(async () => {
      rejectCreate(new Error('gateway unavailable'))
      await Promise.resolve()
    })

    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
        .toBe('typed while waiting\n\ndo not lose me')
    })
    // The payload appears exactly once — the merge must not re-append it.
    const value = (screen.getByLabelText('Message input') as HTMLTextAreaElement).value
    expect(value.split('do not lose me').length - 1).toBe(1)
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('does not treat a substring collision as already restored', async () => {
    // GPT's case: payload "test" sits inside the newer draft "latest". A bare
    // substring match calls that already-restored and drops the payload.
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementation(() => new Promise((_res, rej) => { rejectCreate = rej }))
    const store = makeStore('test')
    await renderPage(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'latest' } })
    await act(async () => {
      rejectCreate(new Error('gateway unavailable'))
      await Promise.resolve()
    })

    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
        .toBe('latest\n\ntest')
    })
  })

  it('leaves the new-session intent disarmed when the user switched away', async () => {
    // Re-arming newSessionRef after a slot switch makes the OTHER session's next
    // message spawn an unintended new session — observable via createChatSlot.
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementation(() => new Promise((_res, rej) => { rejectCreate = rej }))
    const store = makeStore()
    await renderPage(store)

    await act(async () => {
      store.dispatch(setActiveSlot('slot-b'))
      rejectCreate(new Error('gateway unavailable'))
      await Promise.resolve()
    })
    // The payload is parked in the ORIGIN slot's draft; slot-b is untouched.
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')['slot-a']).toBe('do not lose me')
    })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
    // No error bubble in slot-b (the message never belonged there) — a notification
    // carries the failure instead, so it is not silent.
    expect(store.getState().chat.messages.filter(m => m.role === 'error')).toHaveLength(0)
    const notes = store.getState().notifications.items
    expect(notes).toHaveLength(1)
    expect(notes[0].title).toBe('Could not start a new session')
    expect(notes[0].slot).toBe('slot-a')

    // Now send from slot-b: it must go to slot-b, NOT create a session.
    createChatSlot.mockReset()
    createChatSlot.mockResolvedValue({ key: 'slot-c', title: 'slot-c', messages: 0, running: false })
    const input = screen.getByLabelText('Message input')
    fireEvent.change(input, { target: { value: 'a normal message' } })
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })

    expect(createChatSlot).not.toHaveBeenCalled()
    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(sendChat.mock.calls[0][1]).toBe('slot-b')
  })

  it('does not double the payload when the composer already holds it', async () => {
    // Pins the already-restored branch: the composer (restored from the slot's
    // draft) is byte-identical to the auto-send payload, which is what a
    // synchronously-rejected create looks like before React flushes the clear.
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-a': 'do not lose me' }))
    createChatSlot.mockRejectedValue(new Error('gateway unavailable'))
    const store = makeStore()
    await renderPage(store)
    await act(async () => { await Promise.resolve() })

    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('do not lose me')
    })
    expect(sendChat).not.toHaveBeenCalled()
  })

  it('appends rather than drops when the draft merely contains the payload', async () => {
    // Only an exact match proves the payload is already in the composer. "latest test"
    // containing "test" is a different message, so the payload is appended — a visible
    // duplicate is always preferable to silently losing the user's text.
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementation(() => new Promise((_res, rej) => { rejectCreate = rej }))
    const store = makeStore('test')
    await renderPage(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'latest test' } })
    await act(async () => {
      rejectCreate(new Error('gateway unavailable'))
      await Promise.resolve()
    })

    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
        .toBe('latest test\n\ntest')
    })
  })

  it('surfaces the real failure reason, not the developer fallback', async () => {
    // RTK unwrap() rejects with a SERIALIZED error (a plain object), so an
    // `instanceof Error` test would always miss and every user would read the
    // fallback. The thrown Error's message must reach the bubble.
    createChatSlot.mockRejectedValue(new Error('Failed to fetch'))
    const store = makeStore()
    await renderPage(store)
    await act(async () => { await Promise.resolve() })

    await waitFor(() => {
      const errs = store.getState().chat.messages.filter(m => m.role === 'error')
      expect(errs).toHaveLength(1)
      expect(errs[0].content).toContain('Failed to fetch')
    })
  })

  it('re-queues a slotless payload so it sends once a session exists', async () => {
    // No active slot at auto-send: nothing durable can hold the text, and a
    // notification body would reach the OS notification centre. So the payload goes
    // back to the auto-send mechanism, which resends it when a slot activates.
    createChatSlot.mockRejectedValueOnce(new Error('Failed to fetch'))
    createChatSlot.mockResolvedValue({ key: 'slot-z', title: 'slot-z', messages: 0, running: false })
    const store = makeSlotlessStore('remember this exact sentence')
    await renderSlotless(store)
    await act(async () => { await Promise.resolve() })

    // Reported, and no notification carries the message text (the OS-exposure path).
    await waitFor(() => expect(store.getState().notifications.items).toHaveLength(1))
    const note = store.getState().notifications.items[0]
    expect(note.title).toBe('Could not start a new session')
    expect(note.body).toContain('Failed to fetch')
    for (const n of store.getState().notifications.items) {
      expect(n.body).not.toContain('remember this exact sentence')
    }
    expect(note.ts).toMatch(/^\d+(\.\d+)?$/)
    expect(note.kind).toBe('agent')
    expect(sendChat).not.toHaveBeenCalled()

    // A slot becoming available re-drives the auto-send, and the message goes out.
    await act(async () => {
      store.dispatch(setActiveSlot('slot-a'))
      await Promise.resolve()
    })
    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(String(sendChat.mock.calls[0][0])).toContain('remember this exact sentence')
    // Destination matters: the retry must create its OWN session, never deliver into
    // the slot that happened to activate.
    expect(createChatSlot).toHaveBeenCalledTimes(2)
    expect(sendChat.mock.calls[0][1]).toBe('slot-z')
  })

  it('does not treat a payload that is merely a phrase inside the draft as restored', async () => {
    // "please run tests first" CONTAINS the distinct payload "run tests" as a
    // whitespace-delimited phrase; treating that as already-restored drops it.
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementation(() => new Promise((_res, rej) => { rejectCreate = rej }))
    const store = makeStore('run tests')
    await renderPage(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'please run tests first' } })
    await act(async () => {
      rejectCreate(new Error('gateway unavailable'))
      await Promise.resolve()
    })

    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
        .toBe('please run tests first\n\nrun tests')
    })
  })

  it('drives the queued retry even when a slot activated before the rejection', async () => {
    // The auto-send effect only re-runs on `connected` / `send` identity / tick
    // changes. If auto-selection activated a slot BEFORE the create rejected, none of
    // those change again, so the queued payload would sit dormant until page close.
    let rejectCreate: (e: Error) => void = () => {}
    createChatSlot.mockImplementationOnce(() => new Promise((_res, rej) => { rejectCreate = rej }))
    createChatSlot.mockResolvedValue({ key: 'slot-z', title: 'slot-z', messages: 0, running: false })
    const store = makeSlotlessStore('queued sentence')
    await renderSlotless(store)

    // Slot activates first, THEN the create fails.
    await act(async () => {
      store.dispatch(setActiveSlot('slot-a'))
      await Promise.resolve()
    })
    await act(async () => {
      rejectCreate(new Error('Failed to fetch'))
      await Promise.resolve()
    })

    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(String(sendChat.mock.calls[0][0])).toContain('queued sentence')
    // Silent on this path: the retry fires immediately and reports its own outcome.
    // A "you may need to retype it" notification here invites a duplicate turn.
    expect(store.getState().notifications.items).toHaveLength(0)
    // And it must go to a NEWLY created session, not the one auto-selection happened
    // to activate — the new-session intent has to survive the failed attempt.
    expect(createChatSlot).toHaveBeenCalledTimes(2)
    expect(sendChat.mock.calls[0][1]).toBe('slot-z')
  })

  it('creates its own session on retry even when the failed send had no new-session intent', async () => {
    // The slotless path is also reached with forceNew === false — ?autoSend=1 with no
    // newSession=1 (the challenge-token flow, whose own createSlot already failed).
    // Arming the retry with that `false` would deliver the payload as a user turn in
    // whatever unrelated session auto-selection activates.
    createChatSlot.mockRejectedValueOnce(new Error('Failed to fetch'))
    createChatSlot.mockResolvedValue({ key: 'slot-z', title: 'slot-z', messages: 0, running: false })
    const store = makeSlotlessStore('slack originated prompt')
    await renderSlotless(store, '/chat?autoSend=1')
    await act(async () => { await Promise.resolve() })

    await act(async () => {
      store.dispatch(setActiveSlot('slot-a'))
      await Promise.resolve()
    })

    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    expect(String(sendChat.mock.calls[0][0])).toContain('slack originated prompt')
    expect(createChatSlot).toHaveBeenCalledTimes(2)
    // NOT 'slot-a' — that session has nothing to do with this message.
    expect(sendChat.mock.calls[0][1]).toBe('slot-z')
  })
})
