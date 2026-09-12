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
 * - refused                   -> an `error` row at the transcript's tail with
 *   the server's reason, and the text handed back to the composer (merged
 *   with anything typed since).
 * - a rejected fetch          -> the app-sdk wire reports it as response-late:
 *   one POST with no response cannot tell "never left" from "accepted, then
 *   the connection dropped", and the second means a resend runs the turn
 *   twice -- so it is the unconfirmed case below, not a failure.
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
import { AppApiPermissionError } from '../app-sdk/apiError'
import { AcceptedBodyUnreadable, ApiError } from '../api/apiError'

/** The scoped helper's non-2xx throw: the dashboard's `ApiError` with the `API <status>: <body>` message. */
const apiErr = (status: number, body: string) => new ApiError(status, `API ${status}: ${body}`, body)
import { SEND_ABORT_MS } from '../chat-core/transport/sendTurn'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

interface Row { role: string; content: string }
/** The virtualized mount's imperative handle. The embed's new-message scroll
 *  calls `scrollToBottom('smooth')` through this ref (the transcript owns its
 *  scroller since P5-e), so the announcement is observed here, not on a DOM
 *  sentinel's `scrollIntoView`. */
const { mockScrollToBottom } = vi.hoisted(() => ({ mockScrollToBottom: vi.fn() }))
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList({ messages }: { messages: Row[] }, ref: React.Ref<unknown>) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: mockScrollToBottom }), [])
    return (
      <ol data-testid="rows">
        {messages.map((m, i) => <li key={i} data-role={m.role}>{m.content}</li>)}
      </ol>
    )
  }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

function renderEmbed(ui: React.ReactElement) {
  return render(React.createElement(QueryClientProvider, { client: queryClient }, ui))
}

async function typeAndSend(text: string) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: text } })
  })
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Send message'))
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
  resetEmbedRecoveryForTests()
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
    mockPost.mockRejectedValue(apiErr(409, JSON.stringify({ error: 'slot agent mismatch' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    // Framed, not bare: a raw backend reason reads as the agent erroring mid-work.
    expect(errorRows().map(li => li.textContent)).toEqual(["Couldn't send this message: slot agent mismatch. Your text is back in the composer."])
    expect(input().value).toBe('hello')
  })

  it('a rejected fetch is UNCONFIRMED (text back under the notice), never a retry-safe connection failure', async () => {
    // One POST, no response: "never left" and "the server took it and the
    // connection reset before headers" look identical from here, and the
    // second means a resend runs the turn twice. So the embed does what it
    // does for a late receipt -- hands the text back under the unconfirmed
    // notice -- and lets the poll's sendId echo retire it if the turn ran.
    mockPost.mockRejectedValue(new TypeError('Failed to fetch'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(0)
    expect(noticeRows()).toHaveLength(1)
    expect(noticeRows()[0].textContent).toMatch(/^Delivery not confirmed/)
    expect(input().value).toBe('hello')
  })

  it("a scoped-api permission denial names the missing grant instead of saying 'check your connection'", async () => {
    mockPost.mockRejectedValue(new AppApiPermissionError('[app-sdk] App "x" not permitted to access /api/chat. Declared: [/api/apps/x]', 'x'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    // Human sentence in the row; the developer detail goes to the console.
    expect(errorRows()[0].textContent).toBe("Couldn't send this message: x isn't allowed to send chat messages — it doesn't declare chat access in its permissions. Your text is back in the composer.")
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
    await act(async () => { rejectPost(apiErr(500, 'boom')) })
    await settle()
    // Joined with a SPACE, not the paragraph break other composers use: this
    // is a single-line <input>, and a browser drops newlines assigned to its
    // value, which would glue the two into "secondfirst". jsdom keeps the
    // newline, so the assertion must be exact to catch the real-browser bug.
    expect(input().value).toBe('second first')
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
    mockPost.mockRejectedValue(apiErr(409, JSON.stringify({ error: 'busy' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('  keep my spacing  ')
    await settle()
    expect(screen.getByLabelText('Chat message')).toHaveValue('  keep my spacing  ')
    // The wire carried the trimmed text.
    expect((mockPost.mock.calls[0][1] as { message: string }).message).toBe('keep my spacing')
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
    mockPost.mockRejectedValueOnce(apiErr(500, 'boom')).mockResolvedValue({ ok: true })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    await act(async () => { fireEvent.click(screen.getByLabelText('Send message')) })
    await settle()
    expect(errorRows()).toHaveLength(0)
  })

  it("a follow-up chip's direct send does not clobber the draft on failure", async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next? [OPTIONS: Run tests | Skip]' }],
      running: false,
      title: '',
    })
    mockPost.mockRejectedValue(apiErr(500, 'boom'))
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
    mockPost.mockRejectedValue(apiErr(409, JSON.stringify({ error: 'slot agent mismatch' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    const before = mockScrollToBottom.mock.calls.length
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    expect(mockScrollToBottom.mock.calls.length).toBeGreaterThan(before)
    expect(mockScrollToBottom).toHaveBeenLastCalledWith('smooth')
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

  it("a follow-up chip's send in between does not strand a late send's restored text: its notice survives and still retires on proof", async () => {
    // Late send -> notice + 'hello' handed back. Then the user picks a chip
    // (an override send, which consumes nothing from the composer). The
    // notice and its restore must outlive that send: were the tails wiped
    // there, the poll's later proof of the FIRST send would have nothing to
    // retire, and the restored 'hello' would sit in the composer as an
    // already-delivered turn one Enter away from being sent twice.
    const options = { role: 'assistant', content: 'Next? [OPTIONS: Run tests | Skip]' }
    mockGet.mockResolvedValue({ messages: [options], running: false, title: '' })
    mockPost.mockReturnValueOnce(new Promise(() => {})).mockResolvedValue({ ok: true })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
    const lateId = lastSendId()
    await act(async () => { fireEvent.click(screen.getByLabelText('Send now: Run tests')) })
    await settle()
    expect(mockPost).toHaveBeenLastCalledWith('/api/chat?ws=1', expect.objectContaining({ message: 'Run tests' }))
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
    mockGet.mockResolvedValue({
      messages: [options, { role: 'user', content: 'hello', cls: '', meta: { sendId: lateId } }],
      running: true,
      title: '',
    })
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

  it('a user row with the SAME TEXT and NO sendId at all is not proof either: there is no text fallback', async () => {
    // The server keeps the id through the queue (#8853), so an id-less row past
    // the send-start point is some OTHER writer's -- an older backend, an
    // injection, a resend from a client that minted nothing. Matching it by
    // text would false-retire the notice and silently drop an undelivered draft.
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    mockGet.mockResolvedValue({
      messages: [
        { role: 'user', content: 'hello', cls: '' },
        { role: 'user', content: 'hello', cls: '', meta: {} },
      ],
      running: true,
      title: '',
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
  })

  it('a merged queued row that names this send in meta.sendIds is proof: the drain folded it in', async () => {
    // With merge_queued_messages on, the drain writes ONE row for several
    // queued sends and lists every id it stands for; membership is identity.
    mockPost.mockReturnValue(new Promise(() => {}))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(noticeRows()).toHaveLength(1)
    mockGet.mockResolvedValue({
      messages: [{
        role: 'user',
        content: '[2 queued messages merged]\n\nsomething else\n\nhello',
        cls: '',
        meta: { sendId: 's-someone-else', sendIds: ['s-someone-else', lastSendId()] },
      }],
      running: true,
      title: '',
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(0)
    expect(input().value).toBe('')
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
    mockPost.mockRejectedValue(apiErr(409, JSON.stringify({ error: 'slot agent mismatch' })))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    expect(screen.getByRole('status')).toHaveTextContent('')
    await typeAndSend('hello')
    await settle()
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByRole('status')).toHaveTextContent("Couldn't send this message: slot agent mismatch. Your text is back in the composer.")
  })

  it('an error row stays when the poll appends later messages: a refused send is still refused', async () => {
    mockPost.mockRejectedValue(apiErr(500, 'boom'))
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    await settle()
    expect(errorRows()).toHaveLength(1)
    mockGet.mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(errorRows()).toHaveLength(1)
    expect(input().value).toBe('hello')
  })

  it('a refusal that lands after the embed unmounted is shown by the next embed for that slot, text and all', async () => {
    // Send, then the host unmounts the embed (switched specs) BEFORE the server
    // refuses. A refused send has no server copy, so writing the recovery into
    // the dead instance would lose the text silently. It is parked by slot and
    // the next embed for that slot drains it.
    let reject!: (e: unknown) => void
    mockPost.mockReturnValue(new Promise((_, rej) => { reject = rej }))
    let first!: ReturnType<typeof renderEmbed>
    await act(async () => { first = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    first.unmount()
    await act(async () => { reject(apiErr(409, JSON.stringify({ error: 'slot agent mismatch' }))) })
    await settle()
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    expect(errorRows().map(li => li.textContent)).toEqual(["Couldn't send this message: slot agent mismatch. Your text is back in the composer."])
    expect(input().value).toBe('hello')
  })

  it('a receipt parked for one slot is not shown by an embed for another slot', async () => {
    let reject!: (e: unknown) => void
    mockPost.mockReturnValue(new Promise((_, rej) => { reject = rej }))
    let first!: ReturnType<typeof renderEmbed>
    await act(async () => { first = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    first.unmount()
    await act(async () => { reject(apiErr(409, JSON.stringify({ error: 'nope' }))) })
    await settle()
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-2" />) })
    await settle()
    expect(errorRows()).toHaveLength(0)
    expect(input().value).toBe('')
  })

  it('a late send whose embed remounted BEFORE the deadline fired still gets its notice and text on the mounted embed', async () => {
    // The user navigates away and straight back within the deadline: the
    // sending instance is gone, a fresh one is already showing the slot. The
    // receipt must reach the mounted one, not wait for another remount.
    mockPost.mockReturnValue(new Promise(() => {}))
    let first!: ReturnType<typeof renderEmbed>
    await act(async () => { first = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('hello')
    first.unmount()
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    expect(noticeRows()).toHaveLength(0)
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(errorRows()).toHaveLength(0)
    expect(noticeRows()).toHaveLength(1)
    expect(noticeRows()[0].textContent).toMatch(/^Delivery not confirmed/)
    expect(input().value).toBe('hello')
  })

  it('two overlapping sends that both fail after their embeds unmounted each get their text back -- nothing overwrites', async () => {
    // The in-flight guard is per instance: a remounted embed for the same slot
    // can send again while the first instance's send is still out. Both refused
    // after both instances unmounted -> the next embed must hand BOTH texts back.
    let rejectA!: (e: unknown) => void
    let rejectB!: (e: unknown) => void
    mockPost
      .mockReturnValueOnce(new Promise((_, rej) => { rejectA = rej }))
      .mockReturnValueOnce(new Promise((_, rej) => { rejectB = rej }))
    let first!: ReturnType<typeof renderEmbed>
    await act(async () => { first = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('first thought')
    first.unmount()
    let second!: ReturnType<typeof renderEmbed>
    await act(async () => { second = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('second thought')
    second.unmount()
    await act(async () => {
      rejectA(apiErr(409, JSON.stringify({ error: 'nope A' })))
      rejectB(apiErr(409, JSON.stringify({ error: 'nope B' })))
    })
    await settle()
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    expect(input().value).toBe('first thought second thought')
    // Each send keeps its own row: the second outcome does not overwrite the first.
    expect(errorRows().map(li => li.textContent)).toEqual(["Couldn't send this message: nope A. Your text is back in the composer.", "Couldn't send this message: nope B. Your text is back in the composer."])
  })

  it('two late sends drained together are retired INDEPENDENTLY: proof of the first takes back only its text', async () => {
    // The finding this pins: with one "last outcome" record, B's recovery
    // overwrote A's sendId, so when the poll later proved A delivered, its
    // notice could never retire and its text stayed resendable beside the
    // delivered turn. Each record keeps its own id and its own restore.
    mockPost
      .mockReturnValueOnce(new Promise(() => {}))
      .mockReturnValueOnce(new Promise(() => {}))
    let first!: ReturnType<typeof renderEmbed>
    await act(async () => { first = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('first thought')
    const idA = lastSendId()
    first.unmount()
    let second!: ReturnType<typeof renderEmbed>
    await act(async () => { second = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('second thought')
    const idB = lastSendId()
    second.unmount()
    expect(idA).toMatch(/^s-/)
    expect(idB).toMatch(/^s-/)
    expect(idA).not.toBe(idB)
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    expect(noticeRows()).toHaveLength(2)
    expect(input().value).toBe('first thought second thought')
    // The poll proves A landed. A's notice goes; B's stays; A's text is taken
    // back out from UNDER B's, which is rebuilt in place.
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'first thought', cls: '', meta: { sendId: idA } }], running: true, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('second thought')
    // Then B lands too: nothing left to show, nothing left to resend.
    mockGet.mockResolvedValue({
      messages: [
        { role: 'user', content: 'first thought', cls: '', meta: { sendId: idA } },
        { role: 'user', content: 'second thought', cls: '', meta: { sendId: idB } },
      ],
      running: true,
      title: '',
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(noticeRows()).toHaveLength(0)
    expect(input().value).toBe('')
  })

  it('retiring one of two late sends leaves the composer alone once the user has typed into it', async () => {
    mockPost
      .mockReturnValueOnce(new Promise(() => {}))
      .mockReturnValueOnce(new Promise(() => {}))
    let first!: ReturnType<typeof renderEmbed>
    await act(async () => { first = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('first thought')
    const idA = lastSendId()
    first.unmount()
    let second!: ReturnType<typeof renderEmbed>
    await act(async () => { second = renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await typeAndSend('second thought')
    second.unmount()
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SEND_ABORT_MS + 50) })
    await act(async () => { fireEvent.change(input(), { target: { value: 'first thought second thought, edited' } }) })
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'first thought', cls: '', meta: { sendId: idA } }], running: true, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    // A's notice still retires -- proof is proof -- but the edited text stays.
    expect(noticeRows()).toHaveLength(1)
    expect(input().value).toBe('first thought second thought, edited')
  })
})
