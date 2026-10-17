import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FORCE_KILL_ARMING_MS } from '../utils/stopDebounce'
import { KILLING_ESCAPE_MS } from '../hooks/useStopEscapeHatch'

// Stop in the embed: drawn where Send sits while a turn runs, sending the slot
// stop endpoint with the shared soft-then-force press protocol.
const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: ({ transcript }: { transcript?: { belowRows?: React.ReactNode } }) => (
    <div data-testid="chat-message-list">{transcript?.belowRows}</div>
  ),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

async function mount() {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" />
      </QueryClientProvider>,
    )
  })
  await act(async () => { vi.advanceTimersByTime(100) })
}

const stopCalls = () => mockPost.mock.calls.filter(([p]) => String(p).includes('/stop'))

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  mockPost.mockResolvedValue({ ok: true })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatEmbed Stop', () => {
  it('shows Stop instead of Send while a turn runs and the draft is empty', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '' })
    await mount()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Send message' })).toBeNull()
    // A draft mid-turn still sends (it queues), so the send control comes back,
    // named for what it does now (main's ComposerSend: "Queue message").
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'more' } })
    expect(screen.queryByTestId('stop-button-armed')).toBeNull()
    expect(screen.getByRole('button', { name: 'Queue message' })).toBeInTheDocument()
  })

  it('is not the guide\'s chat-page Stop target', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '' })
    await mount()
    // composer.stop lives on the chat page; an embed inside an app must not
    // register a second target the guide could point at.
    expect(screen.getByTestId('stop-button-armed')).not.toHaveAttribute('data-ui-location')
  })

  it('hides the press hint when a draft swaps Stop for Send', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stopping: true, stop_state: 'soft_pending' })
    await mount()
    expect(screen.getByTestId('chat-embed-stop-hint')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'more' } })
    // Send queues the draft; "Click again to force stop" would be a lie.
    expect(screen.queryByTestId('chat-embed-stop-hint')).toBeNull()
  })

  it('draws the line above the hint strip, not between the hint and the input', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stopping: true, stop_state: 'soft_pending' })
    await mount()
    // The strip opens the composer block, so the line that separates the
    // block from the transcript (and its working indicator) sits on top of it.
    const strip = screen.getByTestId('chat-embed-stop-hint').parentElement!.closest('.shrink-0')
    expect(strip).toHaveClass('border-t')
    expect(screen.getByRole('textbox').closest('.shrink-0')).not.toHaveClass('border-t')
    // A draft hides the hint, and the composer row draws the line again.
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'more' } })
    expect(screen.queryByTestId('chat-embed-stop-hint')).toBeNull()
    expect(screen.getByRole('textbox').closest('.shrink-0')).toHaveClass('border-t')
  })

  it('shows Send, not Stop, once the turn is over', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'assistant', content: 'done', ts: 1 }], running: false, title: '' })
    await mount()
    expect(screen.queryByTestId('stop-button-armed')).toBeNull()
    expect(screen.getByRole('button', { name: 'Send message' })).toBeInTheDocument()
  })

  it('soft-stops on the first press and force-stops on a deliberate second press', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle' })
    await mount()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    expect(stopCalls().map(([p]) => p)).toEqual(['/api/chat/slots/slot-1/stop'])
    // The poll has not reported soft_pending yet; the press is held optimistically.
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('Click again to force stop')
    // The cost of the force stop is shown where the user decides, not only on hover.
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('discards in-progress work and queued messages')
    // The header carries the status with the footer; the placeholder stays
    // neutral, so a narrow embed does not say it three times.
    expect(screen.getAllByText('Stopping…').length).toBeGreaterThan(0)
    expect(screen.getByRole('textbox').getAttribute('placeholder')).toMatch(/^Agent is working/)
    // A double-tap inside the arming window does nothing.
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-pulsing')) })
    expect(stopCalls()).toHaveLength(1)
    await act(async () => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 50) })
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-pulsing')) })
    expect(stopCalls().map(([p]) => p)).toEqual([
      '/api/chat/slots/slot-1/stop',
      '/api/chat/slots/slot-1/stop?force=true',
    ])
  })

  it('reads the backend stop phase for the button and the working indicator', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stopping: true, stop_state: 'soft_pending' })
    await mount()
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
    expect(screen.getByTestId('chat-footer')).toHaveTextContent(/Stopping/)
  })

  it('draws the main composer\'s killing spinner, then its escape hatch that force-stops again', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stopping: true, stop_state: 'killing' })
    await mount()
    expect(screen.getByTestId('stop-button-killing')).toBeDisabled()
    // The header follows the footer's "Killing…"; the placeholder stays neutral.
    expect(screen.getAllByText('Killing…').length).toBeGreaterThan(0)
    expect(screen.getByRole('textbox').getAttribute('placeholder')).toMatch(/^Agent is working/)
    expect(screen.queryByTestId('chat-embed-stop-hint')).toBeNull()
    await act(async () => { vi.advanceTimersByTime(KILLING_ESCAPE_MS + 100) })
    // The escape hatch's hint sits above the composer, not beside the button.
    // An instruction, not a status line.
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('Click again to force stop · taking longer than expected')
    expect(screen.queryByTestId('stop-escape-hint')).toBeNull()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-escape-hatch')) })
    expect(stopCalls().map(([p]) => p)).toEqual(['/api/chat/slots/slot-1/stop?force=true'])
  })

  it('says so when the stop is refused, and the retry is a fresh soft press', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle' })
    mockPost.mockResolvedValueOnce({ ok: false, error: 'crew unreachable' })
    await mount()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    expect(screen.getByTestId('chat-embed-stop-error')).toHaveTextContent('crew unreachable')
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
  })

  it('after a compaction decline, reads as not stopping and warns that the next press resets the session', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle' })
    mockPost.mockResolvedValueOnce({ ok: true, info: 'compacting', compacting: true })
    await mount()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    // Nothing is stopping: no pulsing force-stop, no "Stopping…" anywhere.
    expect(screen.queryByTestId('stop-button-pulsing')).toBeNull()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(screen.getByRole('textbox').getAttribute('placeholder')).toMatch(/^Agent is working/)
    expect(screen.queryByText('Stopping…')).toBeNull()
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('Stop will restart this session: the chat history stays, but work in progress and queued messages are dropped.')
    // The reset press is a plain stop: the backend's hatch is what escalates it.
    await act(async () => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 50) })
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    expect(stopCalls().map(([p]) => p)).toEqual(['/api/chat/slots/slot-1/stop', '/api/chat/slots/slot-1/stop'])
  })

  it('warns before the first press when the slot read reports an armed decline', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle', stop_declined: true })
    await mount()
    // Nothing was pressed in this visit, so the warning must not say "again".
    expect(screen.getByTestId('chat-embed-stop-hint')).not.toHaveTextContent(/again/i)
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('Stop will restart this session: the chat history stays, but work in progress and queued messages are dropped.')
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    expect(stopCalls().map(([p]) => p)).toEqual(['/api/chat/slots/slot-1/stop'])
  })

  it('keeps the reset warning beside the error when the press after a decline fails', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle' })
    mockPost
      .mockResolvedValueOnce({ ok: true, info: 'compacting', compacting: true })
      .mockResolvedValueOnce({ ok: false, error: 'crew unreachable' })
    await mount()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    await act(async () => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 50) })
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    // The refusal armed nothing and spent nothing: the backend's hatch is still
    // armed, so the next press still restarts the session and must say so.
    // One instruction: the failure is said without its own "press Stop again".
    expect(screen.getByTestId('chat-embed-stop-error')).toHaveTextContent("That Stop didn't go through.")
    expect(screen.getByTestId('chat-embed-stop-error')).not.toHaveTextContent(/again/i)
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('Stop will restart this session: the chat history stays, but work in progress and queued messages are dropped.')
  })

  // A soft stop with a message queued: the drain starts the next turn at once,
  // and the poll reads it still running, phase idle. Only `turn` changed.
  it('clears an earlier turn stop when the poll shows the next turn', async () => {
    const turn = (n: number) => ({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle', turn: n, turn_gen: 'g' })
    mockGet.mockResolvedValue(turn(4))
    await mount()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
    mockGet.mockResolvedValue(turn(5))
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500) })
    // The new turn was never stopped: no "Stopping", no force-stop hint.
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(screen.queryByTestId('chat-embed-stop-hint')).toBeNull()
    expect(screen.queryByText('Stopping…')).toBeNull()
    // And its first press is a soft stop, not ?force=true.
    await act(async () => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 50) })
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    expect(stopCalls().map(([p]) => p)).toEqual(['/api/chat/slots/slot-1/stop', '/api/chat/slots/slot-1/stop'])
  })

  // GPT finding on 3b4bfeac5f: a soft press that gets no answer keeps
  // `soft_pending` (it may have armed the cancel), so the next press is a force
  // stop that drops the queue. The network error must not hide that cost.
  it('keeps the force-stop and queued-loss warning beside a network error, before the force press', async () => {
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle' })
    mockPost.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await mount()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
    // One instruction: the failure is said without its own "Press Stop again".
    expect(screen.getByTestId('chat-embed-stop-error')).toHaveTextContent("That Stop didn't go through.")
    expect(screen.getByTestId('chat-embed-stop-error')).not.toHaveTextContent(/again/i)
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('Click again to force stop')
    expect(screen.getByTestId('chat-embed-stop-hint')).toHaveTextContent('discards in-progress work and queued messages')
    await act(async () => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 50) })
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-pulsing')) })
    expect(stopCalls().map(([p]) => p)).toEqual([
      '/api/chat/slots/slot-1/stop',
      '/api/chat/slots/slot-1/stop?force=true',
    ])
  })

  // A late failure for the previous turn's press must not clear the new turn's
  // arming stamp: a double-tap would then force-stop the new turn.
  it('ignores a late failure for the previous turn after the new turn was pressed', async () => {
    const turn = (n: number) => ({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '', stop_state: 'idle', turn: n, turn_gen: 'g' })
    mockGet.mockResolvedValue(turn(4))
    let failFirst!: (e: unknown) => void
    mockPost.mockImplementationOnce(() => new Promise((_, reject) => { failFirst = reject }))
    await mount()
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    mockGet.mockResolvedValue(turn(5))
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500) })
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-armed')) })
    await act(async () => { failFirst(Object.assign(new Error('boom'), { status: 500 })) })
    expect(screen.queryByTestId('chat-embed-stop-error')).toBeNull()
    expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
    // The double-tap is still inside the new press's window: ignored, not forced.
    await act(async () => { fireEvent.click(screen.getByTestId('stop-button-pulsing')) })
    expect(stopCalls().map(([p]) => p)).toEqual(['/api/chat/slots/slot-1/stop', '/api/chat/slots/slot-1/stop'])
  })
})
