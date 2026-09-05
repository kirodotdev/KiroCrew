/**
 * Receipt-policy tests for ChatEmbed's send path under the chat-core transport.
 *
 * `sendTurn` decides WHAT happened; this surface decides how to REACT. Before
 * this, the embed reacted to nothing: the bare endpoint's SSE stream failed
 * JSON parsing, the `.catch(SyntaxError => undefined)` called that success,
 * and a refused POST left `sendMutation.isError` unread -- a failed send
 * looked sent and the composer had already been cleared, so the text was gone.
 *
 * Pinned here:
 * - refused / transport-error -> an `error` row at the transcript's tail with
 *   the server's reason when there is one, and the text handed back to the
 *   composer (merged with anything typed since).
 * - response-late             -> the text handed back under a `notice` row
 *   ("unconfirmed"): this embed keeps no optimistic bubble, so the cleared
 *   composer was the only copy -- the recoverable indeterminate outcome.
 * - unknown                   -> nothing: a 2xx was received, so restoring
 *   could invite a duplicate of a turn already running.
 * - an option chip's direct send never restores into the composer: it did not
 *   consume the draft, so restoring would clobber it.
 * - a host-supplied onSend's rejection proves nothing about delivery, so it
 *   is the unconfirmed case (notice + hand back), never a failure.
 * - the unconfirmed notice on a chip send says "re-pick the option" instead
 *   of claiming a restore that the draft-clobber gate prevents.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppApiError, AppApiPermissionError } from '../app-sdk/apiError'
import { AcceptedBodyUnreadable } from '../api/apiError'
import { SEND_ABORT_MS } from '../chat-core/transport/sendTurn'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
}))

interface Row { role: string; content: string }
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: ({ messages }: { messages: Row[] }) => (
    <ol data-testid="rows">
      {messages.map((m, i) => <li key={i} data-role={m.role}>{m.content}</li>)}
    </ol>
  ),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

let queryClient: QueryClient

function renderEmbed(ui: React.ReactElement) {
  return render(React.createElement(Provider, { store: createTestStore() },
    React.createElement(QueryClientProvider, { client: queryClient }, ui)))
}

async function typeAndSend(text: string) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: text } })
  })
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  })
}

async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(50) })
}

/** The sendId the embed stamped on its last POST -- the polled user row must
 *  echo it back for the embed to accept that row as proof of delivery. */
const lastSendId = () => (mockPost.mock.calls.at(-1)?.[1] as { meta?: { sendId?: string } })?.meta?.sendId
const rowsOf = (role: string) => screen.queryAllByRole('listitem').filter(li => li.getAttribute('data-role') === role)
const errorRows = () => rowsOf('error')
const noticeRows = () => rowsOf('notice')
const input = () => screen.getByLabelText('Chat message') as HTMLInputElement

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ChatEmbed send receipt policy', () => {
  it('a refused send appends an error row carrying the server reason and hands the text back', async () => {
    mockPost.mockRejectedValue(new AppApiError(409, JSON.stringify({ error: 'slot agent mismatch' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    // Framed, not bare: a raw backend reason reads as the agent erroring mid-work.
    expect(errorRows().map(li => li.textContent)).toEqual(['Send failed: slot agent mismatch'])
    expect(input().value).toBe('hello')
  })

  it('a transport-level failure states its cause (connection) and hands the text back', async () => {
    mockPost.mockRejectedValue(new TypeError('Failed to fetch'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    expect(errorRows()[0].textContent).toBe("Couldn't send — check your connection and try again.")
    expect(input().value).toBe('hello')
  })

  it("a scoped-api permission denial names the missing grant instead of saying 'check your connection'", async () => {
    mockPost.mockRejectedValue(new AppApiPermissionError('[app-sdk] App "x" not permitted to access /api/chat. Declared: [/api/apps/x]'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    // Human sentence in the row; the developer detail goes to the console.
    expect(errorRows()[0].textContent).toBe("Send failed: This app isn't allowed to send chat messages.")
    expect(input().value).toBe('hello')
  })

  it('merges the handed-back text with what the user typed while the send was in flight', async () => {
    let rejectPost: (e: unknown) => void = () => {}
    mockPost.mockReturnValue(new Promise((_r, rej) => { rejectPost = rej }))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('first')
    await act(async () => {
      fireEvent.change(input(), { target: { value: 'second' } })
    })
    await act(async () => { rejectPost(new AppApiError(500, 'boom')) })
    await settle()
    // The shared recovery rule's paragraph-break join, verbatim: the composer
    // is ChatInput's textarea, the same field every other recovery site
    // writes into, so the join renders as a real line break here too.
    expect(input().value).toBe('second\n\nfirst')
  })

  it('an unreadable 2xx (the old swallowed-as-success shape) neither reports nor restores', async () => {
    mockPost.mockRejectedValue(new AcceptedBodyUnreadable(new SyntaxError('Unexpected token')))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(0)
    expect(input().value).toBe('')
  })

  it('a refused send hands back the draft as typed, not the trimmed wire text', async () => {
    mockPost.mockRejectedValue(new AppApiError(409, JSON.stringify({ error: 'busy' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('  keep my spacing  \n')
    await settle()
    expect(screen.getByLabelText('Chat message')).toHaveValue('  keep my spacing  \n')
    // The wire carried the trimmed text.
    expect((mockPost.mock.calls[0][1] as { message: string }).message).toBe('keep my spacing')
  })

  it('an Enter while a send is in flight is acknowledged: the spinner pulses and "Sending…" is announced', async () => {
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('first')
    await settle()
    const busy = screen.getByRole('button', { name: 'Sending…' })
    expect(busy).toHaveAttribute('aria-busy', 'true')
    expect(busy.className).not.toContain('scale-125')
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'second' } })
      fireEvent.keyDown(screen.getByLabelText('Chat message'), { key: 'Enter', code: 'Enter' })
    })
    expect(busy.className).toContain('scale-125')
    expect(screen.getAllByRole('status').some(el => el.textContent === 'Sending…')).toBe(true)
    // Only one request ever left; the second Enter did not double-send.
    expect(mockPost).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(busy.className).not.toContain('scale-125')
  })

  it('a late response (deadline fired) hands the text back under a NOTICE, not an error', async () => {
    // Unlike ChatPane, this embed keeps no optimistic bubble: once the composer
    // cleared, the user's text had no visible copy left. The transport contract
    // names this the one indeterminate outcome a caller may recover -- so the
    // text comes back, and the row says "unconfirmed", not "failed".
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(errorRows()).toHaveLength(0)
    expect(noticeRows()).toHaveLength(1)
    expect(noticeRows()[0].textContent).toMatch(/^Delivery not confirmed/)
    expect(input().value).toBe('hello')
  })

  it('a dispatched send leaves no row and no restore behind', async () => {
    mockPost.mockResolvedValue({ ok: true })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(0)
    expect(input().value).toBe('')
  })

  it('the next send clears the previous failure row', async () => {
    mockPost.mockRejectedValueOnce(new AppApiError(500, 'boom')).mockResolvedValue({ ok: true })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send' })) })
    await settle()
    expect(errorRows()).toHaveLength(0)
  })

  it("a follow-up chip's direct send does not clobber the draft on failure", async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next? [OPTIONS: Run tests | Skip]' }],
      running: false,
      title: '',
    })
    mockPost.mockRejectedValue(new AppApiError(500, 'boom'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    await act(async () => {
      fireEvent.change(input(), { target: { value: 'my own draft' } })
    })
    // FollowUpBar's send arrow: the chip supplies its own text as the override.
    await act(async () => { fireEvent.click(screen.getByLabelText('Send now: Run tests')) })
    await settle()
    expect(mockPost).toHaveBeenCalledWith('/api/chat?ws=1', expect.objectContaining({ message: 'Run tests' }))
    expect(errorRows()).toHaveLength(1)
    expect(input().value).toBe('my own draft')
  })

  it('a failure row in a top-anchored embed is announced by the new-message scroll, not left below the fold', async () => {
    // Non-startAtBottom embeds scroll only on NEW messages. The send-tail row
    // is not in `messages`, so without folding it into the hash a refused
    // send could append its row off-screen with nothing to announce it -- the
    // silent-loss experience this surface is being fixed for.
    const scroll = Element.prototype.scrollIntoView as ReturnType<typeof vi.fn>
    mockPost.mockRejectedValue(new AppApiError(409, JSON.stringify({ error: 'slot agent mismatch' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    const before = scroll.mock.calls.length
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    expect(scroll.mock.calls.length).toBeGreaterThan(before)
  })

  it("a host onSend's rejection is UNCONFIRMED, not failed: notice + text handed back, no error row", async () => {
    // A host endpoint may have accepted the POST and lost the answer; its
    // rejection proves nothing about delivery, so it must not be reported as
    // a failure that invites a duplicate-turn retry.
    const onSend = vi.fn().mockRejectedValue(new Error('stale tab'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" onSend={onSend} />) })
    await typeAndSend('hello')
    await settle()
    expect(onSend).toHaveBeenCalledWith('hello')
    expect(mockPost).not.toHaveBeenCalled()
    expect(errorRows()).toHaveLength(0)
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
  })

  it("a follow-up chip's send that times out says to re-pick the option, not that the text is back", async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next? [OPTIONS: Run tests | Skip]' }],
      running: false,
      title: '',
    })
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    await act(async () => {
      fireEvent.change(input(), { target: { value: 'my own draft' } })
    })
    await act(async () => { fireEvent.click(screen.getByLabelText('Send now: Run tests')) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(noticeRows()).toHaveLength(1)
    expect(noticeRows()[0].textContent).toMatch(/re-pick the option/)
    expect(noticeRows()[0].textContent).not.toMatch(/back in the composer/)
    expect(input().value).toBe('my own draft')
  })

  it('when the poll proves a late send landed, the notice retires AND the untouched restored text is taken back', async () => {
    // Otherwise the delivered turn renders above a standing "not confirmed"
    // while the composer still holds the same text -- an invitation to send it
    // twice.
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
    expect(lastSendId()).toMatch(/^s-/)
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hello', cls: '', meta: { sendId: lastSendId() } }], running: true, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(0)
    expect(input().value).toBe('')
  })

  it('a user row with the SAME TEXT but a different sendId is not proof: identity, not words', async () => {
    // A manual resend or a duplicate injection can carry identical text.
    // Retiring on it would withdraw THIS send's text while it is unproven.
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hello', cls: '', meta: { sendId: 's-someone-else' } }], running: true, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
  })

  it('proven delivery leaves alone a restored draft the user has edited since', async () => {
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    await act(async () => { fireEvent.change(input(), { target: { value: 'hello, and also this' } }) })
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hello', cls: '', meta: { sendId: lastSendId() } }], running: true, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(0)
    expect(input().value).toBe('hello, and also this')
  })

  it('a send the poll already showed before the deadline fired never leaves a notice or a restored copy', async () => {
    // The yardstick is the transcript length when the send STARTED. If it were
    // read at the deadline, a poll that had already appended the delivered
    // turn would make the notice unretirable.
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hello', cls: '', meta: { sendId: lastSendId() } }], running: true, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS) })
    expect(noticeRows()).toHaveLength(0)
    expect(input().value).toBe('')
  })

  it('unrelated transcript growth is NOT proof of delivery: the notice and the restored text both stay', async () => {
    // A prior turn still streaming, or a cron / sub-agent injecting into the
    // slot, appends rows that have nothing to do with this send. Withdrawing
    // the restored text on that would silently lose an undelivered message.
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(noticeRows()).toHaveLength(1)
    mockGet.mockResolvedValue({
      messages: [
        { role: 'assistant', content: 'still finishing the previous answer', cls: '' },
        { role: 'user', content: 'a different message from a cron', cls: '' },
      ],
      running: true,
      title: '',
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
  })

  it('the send-tail text is also announced through a polite live region for screen readers', async () => {
    mockPost.mockRejectedValue(new AppApiError(409, JSON.stringify({ error: 'slot agent mismatch' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    expect(screen.getByRole('status')).toHaveTextContent('')
    await typeAndSend('hello')
    await settle()
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByRole('status')).toHaveTextContent('Send failed: slot agent mismatch')
  })

  it('an error row stays when the poll appends later messages: a refused send is still refused', async () => {
    mockPost.mockRejectedValue(new AppApiError(500, 'boom'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    mockGet.mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(errorRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
  })
})
