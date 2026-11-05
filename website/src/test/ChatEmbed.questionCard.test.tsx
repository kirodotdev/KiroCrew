/**
 * ChatEmbed shows the slot's pending `ask_question` card and answers it through
 * the slot's own question routes (#25336). Before, the card appeared only in the
 * dashboard Sessions tab and the embed's agent waited out its whole window.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AcceptedBodyUnreadable, ApiError } from '../api/apiError'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const QUESTIONS = [{ question: 'Which branch?', options: [{ label: 'main' }, { label: 'dev' }] }]
const QUESTION_PATH = '/api/chat/slots/slot-1/question'

let pending: unknown[]
let questionError: unknown
let queryClient: QueryClient

function renderEmbed() {
  return render(
    React.createElement(QueryClientProvider, { client: queryClient }, <ChatEmbed slotKey="slot-1" />),
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  Element.prototype.scrollIntoView = vi.fn()
  pending = [{ ask_id: 'a'.repeat(32), slot: 'slot-1', questions: QUESTIONS, ts: 1 }]
  questionError = null
  mockGet.mockImplementation(async (path: string) => {
    if (path === QUESTION_PATH) {
      if (questionError) throw questionError
      return { pending }
    }
    return { messages: [], running: true, title: '' }
  })
  mockPost.mockImplementation(async (path: string) => {
    if (path.endsWith('/question/answer')) pending = []
    return {}
  })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ChatEmbed ask_question card', () => {
  it('shows the pending card, sends the answer to the slot route, and clears', async () => {
    renderEmbed()
    expect(await screen.findByText('Which branch?')).toBeInTheDocument()
    expect(mockGet).toHaveBeenCalledWith(QUESTION_PATH)

    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      '/api/chat/slots/slot-1/question/answer',
      { ask_id: 'a'.repeat(32), answers: { 'Which branch?': 'dev' } },
    ))
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    // Never the owner-wide routes, which refuse every app token.
    expect(mockPost.mock.calls.some(([p]) => String(p).startsWith('/api/ask-question'))).toBe(false)
  })

  it('declines a blocking card through the answer route', async () => {
    renderEmbed()
    await screen.findByText('Which branch?')
    await act(async () => { fireEvent.click(screen.getByLabelText('Dismiss question without answering')) })
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      '/api/chat/slots/slot-1/question/answer',
      { ask_id: 'a'.repeat(32), dismissed: true },
    ))
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
  })

  it('shows a native mid-turn card and steers its answer into the running turn', async () => {
    pending = [{ card_id: 'card-1', slot: 'slot-1', questions: QUESTIONS, native: true, ts: 1 }]
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/steer')) { pending = []; return { ok: true, steered: true } }
      return {}
    })
    renderEmbed()
    expect(await screen.findByText('Which branch?')).toBeInTheDocument()

    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })

    // The answer steers through the slot's own steer route, keyed by card_id.
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      '/api/chat/slots/slot-1/question/steer',
      { card_id: 'card-1', message: 'dev' },
    ))
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    // Never /answer (no ask_id) and never the owner-wide routes.
    expect(mockPost.mock.calls.some(([p]) => String(p).endsWith('/question/answer'))).toBe(false)
    expect(mockPost.mock.calls.some(([p]) => String(p).startsWith('/api/ask-question'))).toBe(false)
  })

  it('hands a native answer back to the composer when no turn is running to steer into', async () => {
    pending = [{ card_id: 'card-1', slot: 'slot-1', questions: QUESTIONS, native: true, ts: 1 }]
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/steer')) { pending = []; return { ok: true, steered: false, unavailable: true } }
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    // Unavailable: the card clears and the answer is kept in the composer, not lost.
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('dev'))
    // Not auto-sent as a message the user did not choose to send.
    expect(mockPost.mock.calls.some(([p]) => p === '/api/chat')).toBe(false)
  })

  it('dismisses a native card through the dismiss route', async () => {
    pending = [{ card_id: 'card-1', slot: 'slot-1', questions: QUESTIONS, native: true, ts: 1 }]
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/dismiss')) { pending = []; return { ok: true } }
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    await act(async () => { fireEvent.click(screen.getByLabelText('Dismiss question without answering')) })
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      '/api/chat/slots/slot-1/question/dismiss',
      { card_id: 'card-1' },
    ))
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    expect(mockPost.mock.calls.some(([p]) => String(p).endsWith('/question/steer'))).toBe(false)
  })

  it('does not drop a native answer when the steer 200 body is unreadable', async () => {
    // F2: /steer returns 200 but scopedApi cannot read the body, so steered vs
    // unavailable is indeterminate. The answer must NOT be settle-dropped: while
    // the card is still shown it is kept (as this card's draft), so a later retry
    // or hand-back still has it — never silently discarded.
    pending = [{ card_id: 'card-1', slot: 'slot-1', questions: QUESTIONS, native: true, ts: 1 }]
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/steer')) throw new AcceptedBodyUnreadable(new Error('connection reset'))
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    // The card is still shown (the answer was kept, not dropped) and nothing was
    // auto-sent as a message the user did not choose to send. (F3's "latest edit
    // wins on a non-delivered steer" shares the blocking card's mechanism, covered
    // by 'restores an edit made during a send that then fails after the card retired'.)
    await waitFor(() => expect(screen.getByText('Which branch?')).toBeInTheDocument())
    expect(mockPost.mock.calls.some(([p]) => p === '/api/chat')).toBe(false)
    expect(screen.queryByTestId('chat-embed-question-restored')).toBeNull()
    // F2: an unreadable 2xx is indeterminate, so while the card stays shown the
    // user must SEE it was not confirmed -- the same ErrorNotice the /answer path
    // raises on an unreadable reply -- not a silent card that looks sent.
    await waitFor(() => expect(screen.getByTestId('chat-embed-question-error')).toBeInTheDocument())
  })

  it('restores the edit made while a native steer POST was pending', async () => {
    // F3: the custom-answer input stays editable while the POST is out. An edit
    // typed after Submit must win over the submitted snapshot when the steer does
    // not land — captured before settle, which deletes the draft.
    pending = [{ card_id: 'card-1', slot: 'slot-1', questions: QUESTIONS, native: true, ts: 1 }]
    let resolve: (v: unknown) => void = () => {}
    mockPost.mockImplementation((path: string) => {
      if (path.endsWith('/question/steer')) return new Promise(r => { resolve = r })
      return Promise.resolve({})
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'first' } })
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    // Edited while the POST is still out...
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'second thoughts' } })
    // ...then the steer resolves unavailable: the newer edit is handed back, not 'first'.
    pending = []
    await act(async () => { resolve({ ok: true, steered: false, unavailable: true }) })
    await waitFor(() => expect((screen.getByRole('textbox', { name: /chat message/i }) as HTMLTextAreaElement).value).toContain('second thoughts'))
  })

  it('keeps a native card when an auth proxy answers the dismiss with a login page', async () => {
    // F2: a 200 whose body is an auth proxy's HTML login page did NOT reach the
    // gateway, so the dismiss never happened. The card must stay (a subsequent
    // poll still returns it) rather than be hidden for the embed's lifetime.
    pending = [{ card_id: 'card-1', slot: 'slot-1', questions: QUESTIONS, native: true, ts: 1 }]
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/dismiss')) {
        throw new AcceptedBodyUnreadable(new Error('not JSON'), {
          headers: new Headers({ 'content-type': 'text/html; charset=utf-8' }),
          redirected: true,
          url: 'https://sso.example.com/login',
        })
      }
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    await act(async () => { fireEvent.click(screen.getByLabelText('Dismiss question without answering')) })
    // Not settled: the card is still shown and an error notice says it did not go out.
    expect(await screen.findByTestId('chat-embed-question-error')).toBeInTheDocument()
    expect(screen.getByText('Which branch?')).toBeInTheDocument()
  })

  it('hands a typed draft back to the composer when its card retires unsubmitted', async () => {
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'release-9' } })
    // The ask expires: the next poll no longer lists it.
    pending = []
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['app-sdk-embed-question', 'slot-1'] }) })
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    // Nothing failed: the draft was kept, so this is a status, not an alert.
    const notice = screen.getByTestId('chat-embed-question-restored')
    expect(notice.getAttribute('role')).not.toBe('alert')
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('release-9')
  })

  it('keeps polling a slot that did not exist yet, and shows its card once it does', async () => {
    questionError = new ApiError(404, 'API 404', '{"code":"slot_not_found"}')
    renderEmbed()
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(QUESTION_PATH))
    expect(screen.queryByTestId('chat-embed-question-load-failed')).toBeNull()
    // The first send creates the slot; the next poll finds the card.
    questionError = null
    expect(await screen.findByText('Which branch?', {}, { timeout: 4000 })).toBeInTheDocument()
  })

  it('does not hand back an answer the poll retired while its POST was in flight', async () => {
    let finish: (v: unknown) => void = () => {}
    mockPost.mockImplementation((path: string) => {
      if (path.endsWith('/question/answer')) return new Promise(r => { finish = r })
      return Promise.resolve({})
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'release-9' } })
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    // A poll before the server handled the POST re-serves the same card (a fresh
    // array, which re-fires QuestionCard's draft callback)...
    pending = [{ ask_id: 'a'.repeat(32), slot: 'slot-1', questions: QUESTIONS.map(q => ({ ...q })), ts: 1 }]
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['app-sdk-embed-question', 'slot-1'] }) })
    // ...then the server resolves the ask before replying, so a poll lands first.
    pending = []
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['app-sdk-embed-question', 'slot-1'] }) })
    // The poll has retired the card while the POST is still out.
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    await act(async () => { finish({ ok: true }) })
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    expect(screen.queryByTestId('chat-embed-question-restored')).toBeNull()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).not.toContain('release-9')
  })

  it('parks a rejected answer when the embed is gone, for the next embed of that slot', async () => {
    let reject: (e: unknown) => void = () => {}
    mockPost.mockImplementation((path: string) => {
      if (path.endsWith('/question/answer')) return new Promise((_, r) => { reject = r })
      return Promise.resolve({})
    })
    const first = renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    first.unmount()
    pending = []
    await act(async () => { reject(new ApiError(404, 'API 404', '{"code":"question_not_found"}')) })
    renderEmbed()
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('dev'))
  })

  it('keeps an answer whose POST failed, and hands it back when the card retires', async () => {
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/answer')) throw new Error('offline')
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    await screen.findByTestId('chat-embed-question-error')
    // The user presses Stop: the ask is withdrawn and the poll no longer lists it.
    pending = []
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['app-sdk-embed-question', 'slot-1'] }) })
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    expect(screen.getByTestId('chat-embed-question-restored')).toBeInTheDocument()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('dev')
  })

  it('drops a shown card once the app may no longer read questions', async () => {
    renderEmbed()
    await screen.findByText('Which branch?')
    questionError = new ApiError(403, 'API 403', '{"code":"session_approval_not_granted"}')
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['app-sdk-embed-question', 'slot-1'] }) })
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
  })

  it('parks an answer whose POST fails after the embed unmounted', async () => {
    let reject: (e: unknown) => void = () => {}
    mockPost.mockImplementation((path: string) => {
      if (path.endsWith('/question/answer')) return new Promise((_, r) => { reject = r })
      return Promise.resolve({})
    })
    const first = renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    first.unmount()
    await act(async () => { reject(new Error('offline')) })
    pending = []
    renderEmbed()
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('dev'))
  })

  it('keeps polling through a lapsed session and shows the card once it is refreshed', async () => {
    questionError = new ApiError(403, 'API 403', '{"error":"auth"}', true)
    renderEmbed()
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(QUESTION_PATH))
    // The session is refreshed; the next poll succeeds.
    questionError = null
    expect(await screen.findByText('Which branch?', {}, { timeout: 6000 })).toBeInTheDocument()
  })

  it('restores an edit made during a send that then fails after the card retired', async () => {
    let reject: (e: unknown) => void = () => {}
    mockPost.mockImplementation((path: string) => {
      if (path.endsWith('/question/answer')) return new Promise((_, r) => { reject = r })
      return Promise.resolve({})
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'first' } })
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    // Edited while the POST is still out...
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'second thoughts' } })
    // ...the card retires, then the send fails.
    pending = []
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['app-sdk-embed-question', 'slot-1'] }) })
    await act(async () => { reject(new Error('offline')) })
    await waitFor(() => expect((screen.getByRole('textbox', { name: /chat message/i }) as HTMLTextAreaElement).value).toContain('second thoughts'))
  })

  it('treats an accepted answer whose reply body was unreadable as sent', async () => {
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/answer')) { pending = []; throw new AcceptedBodyUnreadable(new Error('connection reset')) }
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'release-9' } })
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    expect(screen.queryByTestId('chat-embed-question-restored')).toBeNull()
    expect(screen.queryByTestId('chat-embed-question-error')).toBeNull()
    expect((screen.getByRole('textbox', { name: /chat message/i }) as HTMLTextAreaElement).value).not.toContain('release-9')
  })

  it('keeps the answer when an auth proxy answered with a login page', async () => {
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/answer')) {
        throw new AcceptedBodyUnreadable(new Error('not JSON'), {
          headers: new Headers({ 'content-type': 'text/html; charset=utf-8' }),
          redirected: true,
          url: 'https://sso.example.com/login',
        })
      }
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.change(screen.getByPlaceholderText(/custom answer/i), { target: { value: 'release-9' } })
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    // Nothing reached the gateway: the card stays, says the answer did not go out...
    expect(await screen.findByTestId('chat-embed-question-error')).toBeInTheDocument()
    expect(screen.getByText('Which branch?')).toBeInTheDocument()
    // ...and keeps the answer, which goes to the composer if the ask then ends.
    pending = []
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['app-sdk-embed-question', 'slot-1'] }) })
    await waitFor(() => expect((screen.getByRole('textbox', { name: /chat message/i }) as HTMLTextAreaElement).value).toContain('release-9'))
  })

  it('says so when the question read keeps failing', async () => {
    questionError = new Error('offline')
    renderEmbed()
    // Two retries with React Query's back-off come first.
    expect(await screen.findByTestId('chat-embed-question-load-failed', {}, { timeout: 6000 })).toBeInTheDocument()
  })

  it('hands the answer back to the composer when the wait is gone', async () => {
    mockPost.mockImplementation(async (path: string) => {
      if (path.endsWith('/question/answer')) {
        pending = []
        throw new ApiError(404, 'API 404', '{"code":"question_not_found"}')
      }
      return {}
    })
    renderEmbed()
    await screen.findByText('Which branch?')
    fireEvent.click(screen.getByText('dev'))
    await act(async () => { fireEvent.click(screen.getByText('Submit')) })
    await waitFor(() => expect(screen.queryByText('Which branch?')).toBeNull())
    // A refused submit is a failure: the notice is an alert.
    expect(screen.getByTestId('chat-embed-question-restored').getAttribute('role')).toBe('alert')
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('dev')
    // Not sent as a message the user did not choose to send.
    expect(mockPost.mock.calls.some(([p]) => p === '/api/chat')).toBe(false)
  })

  it('shows nothing when the app may not read the slot question', async () => {
    questionError = new ApiError(403, 'API 403', '{"code":"session_approval_not_granted"}')
    renderEmbed()
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(QUESTION_PATH))
    expect(screen.queryByTestId('chat-embed-question')).toBeNull()
  })
})
