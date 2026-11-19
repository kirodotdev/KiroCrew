/**
 * ChatEmbed's send that THROWS instead of returning a receipt (#23347).
 *
 * `sendTurn` never rejects, so only a bug in the send path reaches the
 * mutation's `onError`. That branch used to drop the optimistic row and
 * nothing else: the composer had already cleared, so the question was gone
 * with no message. It now recovers like a refused send.
 *
 * Pinned here:
 * - a composer submit whose send throws -> the text is back in the composer
 *   and one failure row says it was not sent; the optimistic row is gone.
 * - a chip (override) send whose send throws -> the failure row, but the draft
 *   the user is typing is left alone (the chip never consumed it).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

const { mockSendTurn } = vi.hoisted(() => ({ mockSendTurn: vi.fn() }))
vi.mock('../chat-core/transport/sendTurn', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../chat-core/transport/sendTurn')>()
  return { ...actual, sendTurn: mockSendTurn }
})

interface Row { role: string; content: string }
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList({ messages, transcript }: { messages: Row[]; transcript?: { announcement?: string } }, ref: React.Ref<unknown>) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    return (
      <ol data-testid="rows" data-announcement={transcript?.announcement ?? ''}>
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

async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(50) })
}

const rowsOf = (role: string) => screen.queryAllByRole('listitem').filter(li => li.getAttribute('data-role') === role)
const input = () => screen.getByLabelText('Chat message') as HTMLInputElement

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockSendTurn.mockRejectedValue(new Error('bug in the send path'))
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ChatEmbed send that throws', () => {
  it('a composer submit gets its text back with a failure row', async () => {
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await act(async () => {
      fireEvent.change(input(), { target: { value: 'what is the plan?' } })
    })
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Send message'))
    })
    await settle()
    expect(mockSendTurn).toHaveBeenCalledTimes(1)
    expect(input().value).toBe('what is the plan?')
    expect(rowsOf('error').map(li => li.textContent)).toEqual(["Couldn't send — check your connection and try again."])
    expect(rowsOf('user')).toHaveLength(0)
  })

  it('a chip send that throws shows the failure row and leaves the draft alone', async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next? [OPTIONS: Run tests | Skip]' }],
      running: false,
      title: '',
    })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    await act(async () => {
      fireEvent.change(input(), { target: { value: 'half-typed' } })
    })
    // FollowUpBar's send arrow: the chip supplies its own text as the override.
    await act(async () => { fireEvent.click(screen.getByLabelText('Send now: Run tests')) })
    await settle()
    expect(mockSendTurn).toHaveBeenCalledWith(expect.objectContaining({ message: 'Run tests' }))
    expect(input().value).toBe('half-typed')
    expect(rowsOf('error').map(li => li.textContent)).toEqual(["Couldn't send — check your connection and try again."])
  })

  it('speaks a failed send once, through the embed status region, never the transcript region', async () => {
    const history = [{ role: 'assistant', content: 'Hello.', ts: '1', meta: { mid: 'm1' } }]
    mockGet.mockResolvedValue({ messages: history, running: false, title: '' })
    await act(async () => { renderEmbed(<ChatEmbed slotKey="slot-1" />) })
    await settle()
    const transcriptRegion = () => screen.getByTestId('rows').getAttribute('data-announcement')
    await act(async () => { fireEvent.change(input(), { target: { value: 'what is the plan?' } }) })
    await act(async () => { fireEvent.click(screen.getByLabelText('Send message')) })
    await settle()
    const failure = "Couldn't send — check your connection and try again."
    expect(screen.getAllByRole('status').map(el => el.textContent)).toContain(failure)
    // The transcript region holds only what was sent: not the failure, and not
    // the earlier reply, which a turn that ended without a reply must not re-speak.
    expect(transcriptRegion()).toBe('You: what is the plan?')
    // A poll that brings in another row does not make the transcript region
    // speak the failure either.
    mockGet.mockResolvedValue({ messages: [...history, { role: 'assistant', content: 'Later.', ts: '2', meta: { mid: 'm2' } }], running: false, title: '' })
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(rowsOf('assistant')).toHaveLength(2)
    expect(transcriptRegion()).not.toContain(failure)
  })
})
