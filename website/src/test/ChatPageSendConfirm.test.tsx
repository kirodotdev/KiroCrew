/**
 * The single-chat composer's optimistic bubble is confirmed by the send's OWN
 * HTTP response (#4131).
 *
 * `meta.optimistic` marks a bubble as awaiting confirmation, and the only other
 * thing that clears it is `reconcileOptimisticEcho`, driven by a `chat_message`
 * user echo. That echo is never broadcast for a dashboard send:
 * `DashboardState.append` defaults `broadcast_user=False` precisely BECAUSE the
 * composer already rendered the bubble, and the composer's persistence point
 * does not override it (only a row replayed from a CHANNEL transcript opts in).
 * So without a response-driven confirmation every message the user types stays
 * pending forever — which is why this reducer exists, and why the 30s wall-clock
 * indicator that once read that state flagged every message rather than lost
 * ones, and was removed.
 *
 * These tests pin both directions: an accepted response retires the pending
 * state, a refused one leaves it alone.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor, cleanup } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { confirmOptimisticSend, appendMessage, appendSlotMessage, sseChatMessage } from '../store/chatSlice'
import { loadDrafts } from '../utils/chatDrafts'
import { loadStagedSend, stagedSendKey, PANE_RECOVERY_KEY } from '../utils/chatPaneRecovery'
import { DRAFT_TTL_MS } from '../utils/draftConstants'
import { FILE_DRAFTS_KEY, loadFileDrafts } from '../utils/chatFileDrafts'
import { SESSION_REF_DRAFTS_KEY, loadSessionRefDrafts } from '../utils/chatSessionRefDrafts'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { api } from '../api/client'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
const sendChat = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    // quick_send makes a single chip click SEND rather than select, which is the only
    // route that reaches `send(optionText)` from this harness.
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: true }),
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: (...a: unknown[]) => sendChat(...a),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    knowledgeSources: vi.fn().mockResolvedValue({ sources: [] }),
    createChatSlot: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
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

function makeStore(busy = false, extraSlots: string[] = []) {
  const slotRow = (key: string) => ({ key, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slotsLoaded: true,
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...(busy ? { orchestrating: true } : {}) }, ...extraSlots.map(slotRow)],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: '',
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

async function renderPage(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={['/chat']}><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

/** Type into the composer and submit, the way the user does. */
async function sendText(text: string) {
  const box = screen.getByLabelText('Message input')
  fireEvent.change(box, { target: { value: text } })
  await act(async () => { fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' }) })
}

const userRow = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.find(m => m.role === 'user')

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  sendChat.mockReset()
})

describe('send() confirms its own optimistic bubble from the response', { timeout: 20_000 }, () => {
  it('retires the pending state on an accepted send', async () => {
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('this one was delivered')

    await waitFor(() => expect(userRow(store)).toBeTruthy())
    await waitFor(() => expect(userRow(store)?.meta?.optimistic).toBeUndefined())
    // The correlation id survives so a late echo (channel-linked slot) still
    // updates this row in place instead of pushing a duplicate bubble.
    expect(userRow(store)?.meta?.sendId).toMatch(/^s-/)
  })

  it('does NOT count a queued acceptance as delivery for this bubble', async () => {
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: true }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('queued is not delivered')

    await waitFor(() => expect(userRow(store)).toBeTruthy())
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // The busy branch queues only a non-empty message yet answers ok+queued
    // either way, and when it does queue, its own `queue_push` card owns the
    // message. Either way this bubble is not a delivered row.
    expect(userRow(store)?.meta?.optimistic).toBe(true)
    // The queued row owns the message, so this bubble must stop being a re-attach
    // candidate — a content join cannot pair it, since the queue redacts content.
    expect(userRow(store)?.meta?.pendingServerRow).toBe(false)
  })

  it('leaves an unreadable 2xx receipt SILENT — pending bubble, no error row (#4217)', async () => {
    // The request was accepted and only its answer is mangled, so the turn may
    // be running. The bubble stays pending, which is exactly what it means, and
    // no error row claims a refusal that nothing proves — telling the user to
    // retry here duplicates a delivered turn, side effects included.
    sendChat.mockResolvedValue({ ok: true, json: () => Promise.reject(new Error('unexpected end of JSON input')) })
    const store = makeStore()
    await renderPage(store)
    await sendText('maybe it landed')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(userRow(store)).toBeTruthy())
    expect(userRow(store)?.meta?.optimistic).toBe(true)
    expect(store.getState().chat.messages.some(m => m.role === 'error')).toBe(false)
    // The payload is NOT handed back: a composer holding it again is the retry
    // invitation this whole branch exists to withhold.
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
  })

  it('leaves the bubble pending when the server rejects the send', async () => {
    sendChat.mockResolvedValue({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('this one was refused')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // A refusal is not a receipt, so the pending flag stays put. What the user
    // is told is not this flag: the refusal path appends its own error row and
    // hands the text back to the composer, immediately and by name.
    expect(userRow(store)?.meta?.optimistic).toBe(true)
  })

  it('keeps retention when the RESPONSE fails after an accepted POST', async () => {
    // A transport error is not proof of non-delivery: the server may have accepted
    // and appended the row, so clearing retention would let a refetch delete it.
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore()
    await renderPage(store)
    await sendText('this one may have landed')

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(userRow(store)?.meta?.pendingServerRow).toBe(true)
  })

  it('RESTORES the composer on an unknown delivery, so a reload cannot lose it', async () => {
    // The bubble is store-only and this send never reached the server, so the
    // persisted draft is the only copy that can outlive a reload.
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore()
    await renderPage(store)
    await sendText('page unknown delivery')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    expect(userRow(store)?.content).toBe('page unknown delivery')
    await waitFor(() =>
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('page unknown delivery'),
    )
  })

  it('STILL restores the composer when the server explicitly refused', async () => {
    // The refusal arm, which restores for a different reason: nothing was sent, so
    // the row is un-retained and a resend cannot duplicate a delivered turn.
    sendChat.mockResolvedValue({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) })
    const store = makeStore()
    await renderPage(store)
    await sendText('page never left')

    await waitFor(() =>
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('page never left'),
    )
    expect(userRow(store)?.meta?.deliveryUnknown).toBeUndefined()
  })

  it('marks a timed-out send unconfirmed rather than leaving it looking delivered', async () => {
    // The abort fires before any receipt, so an unmarked row reads as delivered and a
    // later refetch preserves it as a phantom prompt indefinitely.
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('this one stalled')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    // Retention must survive: an abort is no proof the POST never left.
    expect(userRow(store)?.meta?.pendingServerRow).toBe(true)
  })

  it('hands an ABORTED send back even when a bubble was appended', async () => {
    // The bubble is store-only, so a reload keeps neither it nor the draft cleared
    // before the POST -- the composer is the only copy that can outlive one.
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('stalled with a bubble')

    // Precondition: the slot was idle, so a bubble WAS appended. That is the case the
    // old guard skipped the restore for, so this cannot pass by the busy route.
    await waitFor(() => expect(userRow(store)?.content).toBe('stalled with a bubble'))
    await waitFor(() =>
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('stalled with a bubble'),
    )
  })

  it('KEEPS the restored composer when the ECHO confirms the send', async () => {
    // The echo path STRIPS the one-shot sendId, so this is the case a sendId-only
    // comparison would silently stop retiring -- the reason a durable key exists.
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('echoed after all')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toBe('echoed after all'))
    const sendId = userRow(store)?.meta?.sendId as string
    expect(sendId).toBeTruthy()

    await act(async () => {
      store.dispatch(sseChatMessage({
        slot: 'slot-a', role: 'user', content: 'echoed after all',
        meta: { sendId, mid: 'srv-1' },
      }))
    })

    expect(userRow(store)?.meta?.sendId).toBeUndefined()
    expect(userRow(store)?.meta?.confirmedSendId).toBe(sendId)
    await waitFor(() => expect(box().value).not.toBe(''))
  })

  it('does NOT retire a resend because an EARLIER send of identical text was confirmed', async () => {
    // A short prompt ("continue") is easy to send twice. The first is confirmed and its
    // row lingers until the chat_done refetch; the second aborts and is the only copy.
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('continue')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toBe('continue'))

    // Stands for the earlier, genuinely delivered send: same text, DIFFERENT sendId.
    await act(async () => {
      store.dispatch(appendMessage({
        role: 'user', content: 'continue',
        meta: { deliveryConfirmed: true, confirmedSendId: 'earlier-send' },
      } as unknown as Parameters<typeof appendMessage>[0]))
    })

    expect(box().value).toBe('continue')
    expect(localStorage.getItem('mc-chat-drafts') ?? '').toContain('continue')
  })

  it('KEEPS the restored composer once a receipt confirms the send', async () => {
    // The caption retires on confirmation, so leaving the identical payload staged
    // turns one reflexive Enter into a duplicate of an answered turn.
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('confirmed after all')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toBe('confirmed after all'))
    const sendId = userRow(store)?.meta?.sendId as string
    expect(sendId).toBeTruthy()

    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId })) })

    expect(userRow(store)?.meta?.deliveryUnknown).toBeUndefined()
    await waitFor(() => expect(box().value).not.toBe(''))
  })

  it('keeps a restored payload the user has since EDITED', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('confirmed after all')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toBe('confirmed after all'))
    await act(async () => { fireEvent.change(box(), { target: { value: 'my own newer text' } }) })

    const sendId = userRow(store)?.meta?.sendId as string
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId })) })

    expect(box().value).toBe('my own newer text')
  })

  it('addresses a transport failure to the SENDING slot, not the active one', async () => {
    // A slot switch can land before the rejection, and `appendMessage` writes to
    // whichever slot is then on screen — so the row must name the sending slot.
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore()
    const seen: { type: string; slot?: string; role?: string }[] = []
    const realDispatch = store.dispatch.bind(store)
    store.dispatch = ((a: unknown) => {
      const act_ = a as { type?: string; payload?: { slot?: string; message?: { role?: string }; role?: string } }
      if (typeof act_?.type === 'string' && act_.type.startsWith('chat/append')) {
        seen.push({ type: act_.type, slot: act_.payload?.slot, role: act_.payload?.message?.role ?? act_.payload?.role })
      }
      return realDispatch(a as Parameters<typeof realDispatch>[0])
    }) as typeof store.dispatch
    await renderPage(store)
    const sendingSlot = store.getState().chat.activeSlot as string
    await sendText('typed in the sending slot')

    await waitFor(() => expect(seen.some(a => a.role === 'error')).toBe(true))
    const errorAppend = seen.find(a => a.role === 'error')
    // Slot-addressed, and addressed to the SENDING slot specifically.
    expect(errorAppend?.type).toBe('chat/appendSlotMessage')
    expect(errorAppend?.slot).toBe(sendingSlot)
  })
})

describe('discarding a recovered send drops the session references it carried', () => {
  it('clears the restored refs rather than leaving them staged', async () => {
    sessionStorage.setItem(SESSION_REF_DRAFTS_KEY, JSON.stringify({
      'slot-a': [{ key: 'chat-9-1788000009', title: 'Referenced session', messages: 2 }],
    }))
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore()
    await renderPage(store)
    await sendText('carries a ref and is discarded')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    // Premise: the failure arm restored the refs, so one is staged before the discard.
    expect(loadSessionRefDrafts()['slot-a'] ?? []).toHaveLength(1)

    const discard = await screen.findByText(/^Discard restored draft$/i)
    await act(async () => { fireEvent.click(discard) })

    // Discard drops the payload, so the context it linked must go with it -- otherwise the
    // next send silently re-links a session the user never chose for it.
    await waitFor(() => expect(loadSessionRefDrafts()['slot-a'] ?? []).toHaveLength(0))
  })
})

describe('a confirmed send retires the session references it carried', () => {
  it('KEEPS the restored refs in the slot draft', async () => {
    // The failure arm stages text AND session refs back. Confirmation retired only the
    // text and the pastes, so the refs stayed staged and the next send re-linked them.
    sessionStorage.setItem(SESSION_REF_DRAFTS_KEY, JSON.stringify({
      'slot-a': [{ key: 'chat-7-1788000000', title: 'Referenced session', messages: 3 }],
    }))
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore()
    await renderPage(store)
    await sendText('carries a session ref')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const sendId = userRow(store)?.meta?.sendId as string
    expect(typeof sendId).toBe('string')
    // Premise: the restore put the refs back, so the draft still holds one.
    expect(loadSessionRefDrafts()['slot-a'] ?? []).toHaveLength(1)

    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId, mid: 'm-ref-1' })) })

    expect(loadSessionRefDrafts()['slot-a'] ?? []).toHaveLength(1)
  })
})

describe('confirmation retires the recovered payload ONLY', () => {
  it('keeps work the user typed WHILE the send was still in flight', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    sendChat.mockImplementation(() => new Promise((_res, rej) => { rejectSend = rej }))
    const store = makeStore()
    await renderPage(store)
    await sendText('the payload that failed')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // The POST is open for up to 10s and the user types a fresh message in it.
    await act(async () => { fireEvent.change(box(), { target: { value: 'work typed during the post' } }) })
    await act(async () => {
      rejectSend(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })

    // Premise: the failure MERGES, so the composer now holds both.
    await waitFor(() => expect(box().value).toContain('the payload that failed'))
    expect(box().value).toContain('work typed during the post')
    const sendId = userRow(store)?.meta?.sendId as string
    expect(sendId).toBeTruthy()

    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId })) })

    // Only the recovered payload is the duplicate; the newer typing is not.
    await waitFor(() => expect(box().value).toContain('work typed during the post'))
    expect(localStorage.getItem('mc-chat-drafts') ?? '').toContain('the payload that failed')
  })

  it('stops tracking once the OWNER edits, so a later update cannot erase the draft', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('delivered after all')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toBe('delivered after all'))
    const staged = box().value
    const sendId = userRow(store)?.meta?.sendId as string

    // An OWNED read is trustworthy, so this mismatch is a real user edit. Reading
    // through the owned slot removed the untrustworthy-read case this once guarded.
    await act(async () => { fireEvent.change(box(), { target: { value: 'another slot text' } }) })
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId })) })
    // The user later restores the same words, and an unrelated turn arrives.
    await act(async () => { fireEvent.change(box(), { target: { value: staged } }) })
    await act(async () => {
      store.dispatch(appendMessage({
        role: 'assistant', content: 'a later turn',
      } as unknown as Parameters<typeof appendMessage>[0]))
    })

    // Tracking stopped at the edit, so the restored text is the user's to keep.
    expect(box().value).toBe(staged)
  })
})

describe('a draft that merely LOOKS like the recovered payload is still the user\'s', () => {
  it('keeps text the user retyped to the same words while the send was open', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    sendChat.mockImplementation(() => new Promise((_res, rej) => { rejectSend = rej }))
    const store = makeStore()
    await renderPage(store)
    await sendText('duplicate me')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // The send already emptied the composer, so this is the user typing their own
    // fresh draft in the window the POST is open. It happens to read the same.
    await waitFor(() => expect(box().value).toBe(''))
    await act(async () => { fireEvent.change(box(), { target: { value: 'duplicate me' } }) })
    await act(async () => {
      rejectSend(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })

    // Premise: the merge dedupes identical text, so the composer holds one copy.
    await waitFor(() => expect(box().value).toBe('duplicate me'))
    const sendId = userRow(store)?.meta?.sendId as string
    expect(sendId).toBeTruthy()

    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId })) })
    await waitFor(() => expect(userRow(store)?.meta?.deliveryConfirmed).toBe(true))
    await act(async () => { await Promise.resolve() })

    // Confirmation retires the RECOVERED copy. The draft the user typed is theirs.
    expect(box().value).toBe('duplicate me')
  })
})

describe('a confirmation the user never saw still retires the delivered payload', () => {
  it('KEEPS the staged refs when the sending slot confirms OFF-SCREEN', async () => {
    // The receipt can land after a slot switch, so the confirmation is written to the
    // sending slot rather than the rendered one. Miss it and a reload can resend.
    sessionStorage.setItem(SESSION_REF_DRAFTS_KEY, JSON.stringify({
      'slot-a': [{ key: 'chat-9-1788000009', title: 'Referenced session', messages: 4 }],
    }))
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore(false, ['slot-b'])
    await renderPage(store)
    await sendText('delivered while off screen')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const sendId = userRow(store)?.meta?.sendId as string
    expect(sendId).toBeTruthy()
    // Premise: the failure staged the refs back into the SENDING slot's draft.
    expect(loadSessionRefDrafts()['slot-a'] ?? []).toHaveLength(1)

    // The user leaves the slot, so its confirmation arrives off-screen. Assert the
    // switch stuck: ChatPage re-selects a lone slot, which would hide the defect.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-b' }) })
    expect(store.getState().chat.activeSlot).toBe('slot-b')
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'slot-a',
        message: { role: 'user', content: 'delivered while off screen', cls: '', ts: '2026-09-01T12:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: sendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })

    // The delivered turn's refs must not stay staged, or the next send re-links them.
    await waitFor(() => expect(loadSessionRefDrafts()['slot-a'] ?? []).toHaveLength(1))
  })
})

describe('an edit UNDONE back to the same words is still the user\'s own draft', () => {
  it('keeps a draft edited and undone before any confirmation arrived', async () => {
    // No store update lands between the edit and the undo, so the retire effect never
    // runs in between: value equality alone cannot tell this from an untouched draft.
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('deliberately kept')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toBe('deliberately kept'))
    const staged = box().value
    const sendId = userRow(store)?.meta?.sendId as string

    // The user edits, then undoes back to the same words. Both are local composer
    // events, so nothing dispatches and the effect cannot observe the edit by value.
    await act(async () => { fireEvent.change(box(), { target: { value: 'deliberately kept plus more' } }) })
    await act(async () => { fireEvent.change(box(), { target: { value: staged } }) })
    // Only NOW does the delayed confirmation land.
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId })) })
    await waitFor(() => expect(userRow(store)?.meta?.deliveryConfirmed).toBe(true))
    await act(async () => { await Promise.resolve() })

    expect(box().value).toBe(staged)
  })
})

describe('an echo that confirms BEFORE the failure arm restores', () => {
  it('hands the delivered prompt back rather than clearing it', async () => {
    let rejectSend: (e: unknown) => void = () => {}
    sendChat.mockImplementation(() => new Promise((_res, rej) => { rejectSend = rej }))
    const store = makeStore()
    await renderPage(store)
    await sendText('already delivered once')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    const sendId = (sendChat.mock.calls[0][4] as { sendId: string }).sendId
    expect(sendId).toBeTruthy()

    // The echo confirms while nothing is staged yet, so this is the only store
    // change: the retire has to notice the staging that happens afterwards.
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId })) })
    await act(async () => { rejectSend(new DOMException('The operation was aborted.', 'AbortError')); await Promise.resolve() })
    await act(async () => { await Promise.resolve() })

    // Delivered, so it must not sit in the composer or the draft awaiting a resend.
    expect(box().value).not.toBe('')
    expect(loadDrafts()['slot-a'] ?? '').not.toBe('')
  })
})

describe('a keystroke in ANOTHER slot must not retire this slot tracking', () => {
  it('still KEEPS the delivered draft when the user typed in a different slot', async () => {
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore(false, ['slot-b'])
    await renderPage(store)
    await sendText('delivered while I typed elsewhere')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const sendId = userRow(store)?.meta?.sendId as string
    expect(typeof sendId).toBe('string')
    // Premise: the failure arm put the payload back into slot-a's draft.
    await waitFor(() => expect(loadDrafts()['slot-a'] ?? '').toContain('delivered while I typed elsewhere'))

    // The user leaves slot-a and types in slot-b. That keystroke belongs to slot-b.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-b' }) })
    expect(store.getState().chat.activeSlot).toBe('slot-b')
    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box(), { target: { value: 'unrelated work in slot b' } }) })

    // slot-a's send is then confirmed off-screen: its delivered draft must be retired.
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'slot-a',
        message: { role: 'user', content: 'delivered while I typed elsewhere', cls: '', ts: '2026-09-02T02:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: sendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })

    await waitFor(() => expect(loadDrafts()['slot-a'] ?? '').not.toBe(''))
  })
})

describe('a timeout that restores the payload also restores its attachments', () => {
  it('puts the sent files back, and leaves them staged once delivery is confirmed', async () => {
    sessionStorage.setItem(FILE_DRAFTS_KEY, JSON.stringify({ 'slot-a': ['tok-attach-1'] }))
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('carries an attachment')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const sendId = userRow(store)?.meta?.sendId as string
    expect(sendId).toBeTruthy()
    // The abort restored the text; the attachment must come back with it, or the
    // user cannot reconstruct the message it belonged to.
    await waitFor(() => expect(loadFileDrafts()['slot-a'] ?? []).toContain('tok-attach-1'))

    // Delivery is then proven. The TEXT retires, but the attachment must NOT: a file is
    // a bare path, so a re-picked one cannot be told from this copy and would be deleted.
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId, mid: 'm-attach' })) })
    await waitFor(() => expect(loadFileDrafts()['slot-a'] ?? []).toContain('tok-attach-1'))
    expect(loadFileDrafts()['slot-a'] ?? []).toContain('tok-attach-1')
  })
})

describe('discard cannot reach context staged AFTER the restore', () => {
  it('withholds the affordance for a file staged DURING the pending send', async () => {
    // The failure arm MERGES what went out with what the user staged mid-flight, so a snapshot
    // taken from the merged arrays would license Discard to delete their unsent attachment.
    sessionStorage.setItem(FILE_DRAFTS_KEY, JSON.stringify({ 'slot-a': ['tok-went-out'] }))
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/staged-mid-flight.pdf'] })
    let rejectSend: ((e: unknown) => void) | undefined
    sendChat.mockImplementationOnce(() => new Promise((_res, rej) => { rejectSend = rej }))
    const store = makeStore()
    await renderPage(store)
    await sendText('sent while another file was being staged')

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['y'], 'staged-mid-flight.pdf', { type: 'application/pdf' })] })
    await act(async () => { fireEvent.change(fileInput) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())

    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))

    // The mid-flight file is the user's own unsent work, so the control must not be offered.
    await waitFor(() => expect(screen.queryByText(/^Discard restored draft$/i),
      'discard must not reach a file staged during the request').toBeNull())
  })

  it('withholds the affordance once a new file is attached', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/added-after.pdf'] })
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('recovered with nothing attached')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    // Premise: the restore staged text ALONE, so Discard is offered at this point.
    expect(await screen.findByText(/^Discard restored draft$/i)).not.toBeNull()

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [new File(['x'], 'added-after.pdf', { type: 'application/pdf' })] })
    await act(async () => { fireEvent.change(fileInput) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())

    // The file is the user's own, staged after the recovery, so the control must retire.
    await waitFor(() => expect(screen.queryByText(/^Discard restored draft$/i),
      'discard must not survive a file the user attached after the restore').toBeNull())
  })
})

describe('discard still retires the attachment the restore itself staged', () => {
  it('offers the affordance for a recovered send that carried a file', async () => {
    sessionStorage.setItem(FILE_DRAFTS_KEY, JSON.stringify({ 'slot-a': ['tok-attach-2'] }))
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('recovered beside a file')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const sendId = userRow(store)?.meta?.sendId as string
    await waitFor(() => expect(loadFileDrafts()['slot-a'] ?? []).toContain('tok-attach-2'))
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId, mid: 'm-file' })) })
    await waitFor(() => expect(screen.queryAllByText(/would deliver a duplicate/i).length).toBeGreaterThan(0))

    // The restore staged this file, so it is part of the payload Discard exists to retire --
    // withholding on its mere presence would hide the exit for every send carrying context.
    expect(screen.queryByText(/^Discard restored draft$/i), 'discard belongs to the payload it restored')
      .not.toBeNull()
  })
})

describe('a SECOND failed send must not drop the first slot tracking', () => {
  it('still tracks the first slot after another slot also fails', async () => {
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore(false, ['slot-b'])
    await renderPage(store)
    await sendText('first slot payload')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const firstSendId = userRow(store)?.meta?.sendId as string
    expect(firstSendId).toBeTruthy()
    await waitFor(() => expect(loadDrafts()['slot-a'] ?? '').toContain('first slot payload'))

    // A SECOND send fails in another slot. With one shared record this silently
    // replaced slot-a's, leaving its delivered copy staged for a duplicate resend.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-b' }) })
    expect(store.getState().chat.activeSlot).toBe('slot-b')
    await sendText('second slot payload')
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))

    // slot-a's send is then confirmed: its draft must still retire.
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'slot-a',
        message: { role: 'user', content: 'first slot payload', cls: '', ts: '2026-09-02T04:00:00.000Z',
          meta: { deliveryConfirmed: true, confirmedSendId: firstSendId } },
      } as unknown as Parameters<typeof appendSlotMessage>[0]))
    })

    await waitFor(() => expect(loadDrafts()['slot-a'] ?? '').not.toBe(''))
  })
})

describe('the duplicate-resend warning is echoed AT the composer', () => {
  it('shows it while a staged payload sits there, and drops it only once the payload is gone', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('probably delivered')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const sendId = userRow(store)?.meta?.sendId as string

    // The resend is fired at the composer, so the composer OWNS the resend clause and is
    // its only carrier; the transcript bubble states the state alone, without repeating it.
    await waitFor(() => expect(screen.getAllByText(/resending may send it twice/i)).toHaveLength(1))
    expect(screen.queryAllByText(/^Delivery unconfirmed$/i).length).toBeGreaterThan(0)

    // Confirmation must NOT release it -- the payload is still one Enter from a duplicate.
    // The copy swaps hedge -> fact, and that swap IS the read of the delivery markers.
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId, mid: 'm-warn' })) })
    await waitFor(() => expect(screen.queryAllByText(/would deliver a duplicate/i).length).toBeGreaterThan(0))
    expect(screen.queryAllByText(/resending may send it twice/i)).toHaveLength(0)

    // An edit that LEAVES the payload in place must not release it: that is the natural
    // pre-resend move, and the payload is still one Enter from a duplicate.
    const box = screen.getByLabelText('Message input')
    await act(async () => { fireEvent.change(box, { target: { value: 'probably delivered, edited' } }) })
    expect(screen.queryAllByText(/send it twice|would deliver a duplicate/i).length).toBeGreaterThan(0)
    // Replacing it outright is a different message, so the warning is spent.
    await act(async () => { fireEvent.change(box, { target: { value: 'a wholly different prompt' } }) })
    await waitFor(() => expect(screen.queryAllByText(/send it twice|would deliver a duplicate/i)).toHaveLength(0))
  })

  it('agrees with the bubble once confirmed, and offers a working discard', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('probably delivered')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    const sendId = userRow(store)?.meta?.sendId as string
    // A LATER send's confirm demotes this one to `deliveryUnresolved`; then it confirms too.
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId: 'other-send', mid: 'm-other' })) })
    await act(async () => { store.dispatch(confirmOptimisticSend({ slot: 'slot-a', sendId, mid: 'm-warn' })) })

    // The two captions must not contradict each other in one viewport.
    await waitFor(() => expect(screen.queryAllByText(/would deliver a duplicate/i).length).toBeGreaterThan(0))
    expect(screen.queryAllByText(/never confirmed/i), 'the bubble must not call a delivered send unconfirmed')
      .toHaveLength(0)

    // And the payload it warns about has an exit that leaves the composer empty.
    const discard = screen.getByText(/^Discard restored draft$/i)
    await act(async () => { fireEvent.click(discard) })
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe(''))
    expect(screen.queryAllByText(/send it twice|would deliver a duplicate/i)).toHaveLength(0)
  })

  it('drops the warning only once the restored text is no longer in the composer', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('probably delivered')

    await waitFor(() => expect(screen.getAllByText(/resending may send it twice/i)).toHaveLength(1))

    // Appending to it keeps the resendable payload, so the warning still applies.
    const box = screen.getByLabelText('Message input')
    await act(async () => { fireEvent.change(box, { target: { value: 'probably delivered, edited' } }) })
    expect(screen.queryAllByText(/resending may send it twice/i).length).toBeGreaterThan(0)

    // Clearing it is the one edit that genuinely retires the risk.
    await act(async () => { fireEvent.change(box, { target: { value: '' } }) })
    await waitFor(() => expect(screen.queryAllByText(/resending may send it twice/i)).toHaveLength(0))
  })
})

describe('a restored payload stays MARKED across a reload', () => {
  it('re-arms the caption from the persisted marker, so a reload cannot resend it unmarked', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore()
    await renderPage(store)
    await sendText('delivered but unconfirmed')

    await waitFor(() => expect(userRow(store)?.meta?.deliveryUnknown).toBe(true))
    // The payload is a DRAFT, so it survives the reload; the marker must too.
    await waitFor(() => expect(loadDrafts()['slot-a'] ?? '').toContain('delivered but unconfirmed'))
    expect(loadStagedSend('slot-a')).toBeTruthy()

    // Reload: a brand new store and a brand new mount, same session storage.
    cleanup()
    const reloaded = makeStore()
    await renderPage(reloaded)

    // Without the persisted marker the draft comes back with no caption at all, and one
    // Enter resends a turn the server may already have taken.
    await waitFor(() => expect(screen.queryAllByText(/send it twice|would deliver a duplicate/i).length).toBeGreaterThan(0))
  })
})

describe('a marker from ANOTHER slot must not speak for this one', () => {
  // The store prunes on its OWN timestamp while the sibling drafts are re-stamped each
  // revisit, so a read-only marker ages out from under a live, still-captioned draft.
  it('re-stamps the marker on revisit so it cannot expire under its own draft', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore(false, ['slot-b'])
    await renderPage(store)
    await sendText('payload that must stay captioned')
    await waitFor(() => expect(loadStagedSend('slot-a')).toBeTruthy())

    // Age slot-a's marker stamp to just inside the TTL, as a long-lived tab would.
    const aged = Date.now() - (DRAFT_TTL_MS - 60_000)
    localStorage.setItem(`${PANE_RECOVERY_KEY}-ts`, JSON.stringify({ [stagedSendKey('slot-a')]: aged }))

    // Leave slot-a and come back: this is the revisit that re-stamps every other draft.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-b' }) })
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-a' }) })

    await waitFor(() => {
      const stamps = JSON.parse(localStorage.getItem(`${PANE_RECOVERY_KEY}-ts`) ?? '{}')
      expect(stamps[stagedSendKey('slot-a')], 'the revisit left the marker on its original stamp, so it expires first')
        .toBeGreaterThan(aged)
    })
  })

  it('shows slot-a its own caption after a later failure staged slot-b', async () => {
    sendChat.mockRejectedValue(new DOMException('The operation was aborted.', 'AbortError'))
    const store = makeStore(false, ['slot-b'])
    await renderPage(store)
    await sendText('slot a payload')
    await waitFor(() => expect(loadStagedSend('slot-a')).toBeTruthy())

    // A second failed send in another slot. Its marker must not be the one slot-a sees.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-b' }) })
    await sendText('slot b payload')
    await waitFor(() => expect(loadStagedSend('slot-b')).toBeTruthy())

    // Back to slot-a: the caption must name slot-a's own staged send, not slot-b's.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-a' }) })
    await waitFor(() => expect(screen.queryAllByText(/send it twice|would deliver a duplicate/i).length).toBeGreaterThan(0))
  })
})

describe('an explicitly REFUSED send arms no duplicate-resend caption', () => {
  it('restores the text but claims no duplicate risk the server ruled out', async () => {
    sendChat.mockResolvedValue({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) } as never)
    const store = makeStore()
    await renderPage(store)
    await sendText('refused outright')

    // The payload comes back -- that part is unchanged.
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
      .toContain('refused outright'))
    // But nothing was sent, so a retry is safe and no caption may say otherwise.
    expect(screen.queryAllByText(/send it twice|would deliver a duplicate/i)).toHaveLength(0)
    expect(loadStagedSend('slot-a')).toBeUndefined()
  })
})


describe('the warning offers no control that could erase a merged draft', () => {
  it('keeps text typed during the request and renders no discard affordance', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    sendChat.mockImplementationOnce(() => new Promise((_res, rej) => { rejectSend = rej }))
    const store = makeStore()
    await renderPage(store)
    await sendText('the payload that went out')

    // The failure arm MERGES rather than clobbers, so a message typed mid-flight ends up
    // beside the restored payload -- which is why no control may clear it wholesale.
    const box = screen.getByLabelText('Message input')
    await act(async () => { fireEvent.change(box, { target: { value: 'unrelated new work' } }) })
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })

    await waitFor(() => expect(screen.queryAllByText(/send it twice|would deliver a duplicate/i).length).toBeGreaterThan(0))
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
      .toContain('unrelated new work')
    expect(screen.queryByText(/discard/i)).toBeNull()
  })
})

describe('an option send never touches the composer it did not come from', () => {
  it('leaves an existing draft byte-identical when the option send aborts', async () => {
    let rejectSend: ((e: unknown) => void) | undefined
    sendChat.mockImplementation(() => new Promise((_res, rej) => { rejectSend = rej }))
    const store = makeStore()
    await renderPage(store)

    const box = screen.getByLabelText('Message input')
    await act(async () => { fireEvent.change(box, { target: { value: 'half-written thought' } }) })
    await act(async () => {
      store.dispatch(appendSlotMessage({
        slot: 'slot-a',
        message: { role: 'assistant', content: 'pick one [OPTIONS: the clicked option text | other]', cls: '' },
      }))
    })
    // With quick_send a single chip click sends instantly -- that is the option send.
    const chips = await screen.findAllByRole('button', { name: /the clicked option text/i })
    await act(async () => { fireEvent.click(chips[0]) })
    await waitFor(() => expect(sendChat).toHaveBeenCalled())
    await act(async () => {
      rejectSend?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })

    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value)
      .toBe('half-written thought')
  })
})

describe('an off-screen failure does not steal the active slot warning', () => {
  it('leaves the visible slot own duplicate-send warning standing', async () => {
    const rejects: ((e: unknown) => void)[] = []
    sendChat.mockImplementation(() => new Promise((_res, rej) => { rejects.push(rej) }))
    const store = makeStore(false, ['slot-b'])
    await renderPage(store)
    await sendText('sent from slot-a')

    // slot-b earns a warning of its OWN, then slot-a's older send fails off-screen. The
    // in-memory marker is single-valued, so an ungated write replaces slot-b's warning.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-b' }) })
    await sendText('sent from slot-b')
    await act(async () => {
      rejects[1]?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    const visible = () => Array.from(document.querySelectorAll('span[role="status"]'))
      .filter(e => /send it twice|would deliver a duplicate/i.test(e.textContent ?? ''))
    await waitFor(() => expect(visible()).toHaveLength(1))

    await act(async () => {
      rejects[0]?.(new DOMException('The operation was aborted.', 'AbortError'))
      await Promise.resolve()
    })
    expect(visible()).toHaveLength(1)
  })
  /* UX at ea3041f4f: the duplicate-send warning was released on the first keystroke, but an EDIT
   * is the natural move before resending -- so the user pressed Enter on a near-identical payload
   * with the "may send it twice" caption already gone. */
  it('keeps the duplicate-send warning while the composer still holds the payload', async () => {
    sendChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = makeStore()
    await renderPage(store)
    await sendText('page keeps warning')

    const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(box().value).toBe('page keeps warning'))
    await waitFor(() => expect(document.querySelector('[key="delivery-unconfirmed"], .pt-1\\.5')).toBeTruthy())
    const warned = () => screen.queryByText(/resending may send it twice/i)
    await waitFor(() => expect(warned()).toBeTruthy())

    // Tweak the restored text -- the resendable payload is STILL in the composer.
    await act(async () => { fireEvent.change(box(), { target: { value: 'page keeps warning please' } }) })
    expect(warned(),
      'the warning must survive an edit that leaves the resendable payload in place').toBeTruthy()

    // Replacing it entirely is a different message, so the warning is spent.
    await act(async () => { fireEvent.change(box(), { target: { value: 'something else' } }) })
    await waitFor(() => expect(warned()).toBeNull())
  })

})
