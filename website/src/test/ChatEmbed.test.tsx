import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ── Mocks ──

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
}))

interface MockChatMessageListProps {
  messages: unknown[]
  running: boolean
  onApprove?: (approvalId: string, decision: string) => void
  canTrust?: boolean
}

vi.mock('./ChatMessageList', () => ({
  default: ({ messages, running, onApprove, canTrust }: MockChatMessageListProps) => (
    <div data-testid="chat-message-list" data-count={messages.length} data-running={String(running)}
      data-can-approve={String(!!onApprove)} data-can-trust={String(!!canTrust)}>
      {onApprove && (
        <>
          <button data-testid="mock-approve" onClick={() => onApprove('appr-1', 'approved')}>approve</button>
          <button data-testid="mock-trust" onClick={() => onApprove('appr-1', 'trust')}>trust</button>
        </>
      )}
    </div>
  ),
}))

// Mock ChatMessageList from the correct path (ChatEmbed imports from ./ChatMessageList)
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: ({ messages, running, onApprove, canTrust }: MockChatMessageListProps) => (
    <div data-testid="chat-message-list" data-count={messages.length} data-running={String(running)}
      data-can-approve={String(!!onApprove)} data-can-trust={String(!!canTrust)}>
      {onApprove && (
        <>
          <button data-testid="mock-approve" onClick={() => onApprove('appr-1', 'approved')}>approve</button>
          <button data-testid="mock-trust" onClick={() => onApprove('appr-1', 'trust')}>trust</button>
        </>
      )}
    </div>
  ),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { deriveFollowUpOptions } from '../app-sdk/protocol'
import { api } from '../api/client'
import type { ChatMessage } from '../types'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

let queryClient: QueryClient

// ChatEmbed now mounts the real ChatInput, which reads slot state from Redux.
function renderWithProviders(ui: React.ReactElement) {
  return render(
    React.createElement(Provider, { store: createTestStore() },
      React.createElement(QueryClientProvider, { client: queryClient }, ui)),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  // vitest 4's restoreAllMocks no longer clears standalone vi.fn() call history
  // (mockGet/mockPost), so clear it explicitly or calls leak across tests.
  vi.clearAllMocks()
  vi.useFakeTimers()
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
  // Default: return empty slot data
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({ ok: true })
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatEmbed', () => {
  describe('rendering', () => {
    it('renders message input with aria-label', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      expect(screen.getByLabelText('Chat message')).toBeInTheDocument()
    })

    it('renders the real composer send button', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument()
    })

    it('shows custom placeholder', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" placeholder="Ask me anything..." />)
      })
      expect(screen.getByPlaceholderText('Ask me anything...')).toBeInTheDocument()
    })

    it('shows default placeholder when none provided', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      expect(screen.getByPlaceholderText('Message...')).toBeInTheDocument()
    })

    it('shows agent label when agent prop provided', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" agent="privacy-dev" />)
      })
      expect(screen.getByText('privacy-dev')).toBeInTheDocument()
    })

    it('shows session ready message when no messages and not running', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      expect(screen.getByText('Session ready. Type a message to start.')).toBeInTheDocument()
    })

    it('does not show session ready message when running', async () => {
      mockGet.mockResolvedValue({ messages: [], running: true, title: '' })
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      // Advance timers so React Query's internal scheduling fires, then flush microtasks
      await act(async () => {
        vi.advanceTimersByTime(100)
      })
      expect(screen.queryByText('Session ready. Type a message to start.')).toBeNull()
    })
  })

  describe('send behavior', () => {
    it('send button is disabled when input is empty', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    })

    it('send button is enabled when input has text', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const input = screen.getByLabelText('Chat message')
      await act(async () => {
        fireEvent.change(input, { target: { value: 'hello' } })
      })
      expect(screen.getByRole('button', { name: 'Send' })).not.toBeDisabled()
    })

    it('clears input after send', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const input = screen.getByLabelText('Chat message') as HTMLInputElement

      await act(async () => {
        fireEvent.change(input, { target: { value: 'test message' } })
      })

      expect(input.value).toBe('test message')

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      })

      // Input should be cleared immediately
      expect(input.value).toBe('')
    })

    it('a second submit while a send is in flight does not fire a second POST', async () => {
      // The composer is the real ChatInput and is deliberately NOT disabled
      // while a send is pending (its placeholder chain would announce
      // "Stopping..."); `send()` guards re-entry instead.
      let resolvePost: (v: unknown) => void = () => {}
      mockPost.mockReturnValue(new Promise(r => { resolvePost = r }))

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const input = screen.getByLabelText('Chat message') as HTMLTextAreaElement
      await act(async () => {
        fireEvent.change(input, { target: { value: 'hello' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      })
      expect(mockPost).toHaveBeenCalledTimes(1)
      expect(input).not.toBeDisabled()

      // The button acknowledges the in-flight send: spinner, "Sending…" label,
      // aria-busy -- and a click on it does nothing, with the field still live.
      const pending = screen.getByRole('button', { name: 'Sending…' })
      expect(pending).toHaveAttribute('aria-busy', 'true')
      // The draft has just been cleared; the button must NOT fall into the
      // empty-value disabled state, or the spinner renders dimmed.
      expect(pending).not.toBeDisabled()
      await act(async () => {
        fireEvent.change(input, { target: { value: 'again' } })
      })
      await act(async () => {
        fireEvent.click(pending)
      })
      expect(mockPost).toHaveBeenCalledTimes(1)

      await act(async () => { resolvePost({ ok: true }) })
      await act(async () => { vi.advanceTimersByTime(100) })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      })
      expect(mockPost).toHaveBeenCalledTimes(2)
    })

    it("honours the user's send-key setting from chat settings (Ctrl/Cmd+Enter mode)", async () => {
      localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: 'ctrl-enter' }))
      try {
        await act(async () => {
          renderWithProviders(<ChatEmbed slotKey="slot-1" />)
        })
        const input = screen.getByLabelText('Chat message')
        await act(async () => {
          fireEvent.change(input, { target: { value: 'hello' } })
        })
        await act(async () => {
          fireEvent.keyDown(input, { key: 'Enter' })
        })
        expect(mockPost).not.toHaveBeenCalled()
        await act(async () => {
          fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true })
        })
        expect(mockPost).toHaveBeenCalledTimes(1)
      } finally {
        localStorage.removeItem('mc-chat-config')
      }
    })

    it('sends message with correct params via API', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" agent="test-agent" />)
      })

      await act(async () => {
        fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'hello world' } })
      })

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      })

      // `?ws=1` selects the JSON receipt the shared transport reads; the scoped
      // path check ignores the query, so the app's `/api/chat` grant still covers it.
      expect(mockPost).toHaveBeenCalledWith('/api/chat?ws=1', {
        message: 'hello world',
        slot: 'slot-1',
        agent: 'test-agent',
        // Client-minted correlation id, same convention as ChatPage/ChatPane.
        meta: { sendId: expect.stringMatching(/^s-/) },
      })
    })

    it('does not send empty or whitespace-only messages', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })

      await act(async () => {
        fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: '   ' } })
      })

      // Button should be disabled for whitespace-only input
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    })

    it('sends on Enter key press (not Shift+Enter)', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const input = screen.getByLabelText('Chat message')

      await act(async () => {
        fireEvent.change(input, { target: { value: 'hello' } })
      })

      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter', shiftKey: false })
      })

      expect(mockPost).toHaveBeenCalledWith('/api/chat?ws=1', expect.objectContaining({
        message: 'hello',
      }))
    })

    it('does not send on Shift+Enter', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const input = screen.getByLabelText('Chat message')

      await act(async () => {
        fireEvent.change(input, { target: { value: 'hello' } })
      })

      await act(async () => {
        fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
      })

      expect(mockPost).not.toHaveBeenCalled()
    })
  })

  describe('onSend routing', () => {
    it('routes the composer through onSend instead of POST /api/chat', async () => {
      // POST /api/chat keys off slotKey alone and CREATES the slot when it is
      // missing -- with no app ownership and no project. A host app that owns its
      // slots must be able to keep sends on its own endpoint, so a stale tab
      // cannot resurrect an unscoped session.
      const onSend = vi.fn().mockResolvedValue(undefined)
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" onSend={onSend} />)
      })
      await act(async () => {
        fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'hi' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      })

      expect(onSend).toHaveBeenCalledWith('hi')
      expect(mockPost).not.toHaveBeenCalledWith('/api/chat?ws=1', expect.anything())
    })

    it('still posts to /api/chat when no onSend is supplied', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      await act(async () => {
        fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'hi' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      })

      expect(mockPost).toHaveBeenCalledWith(
        '/api/chat?ws=1',
        expect.objectContaining({ message: 'hi', slot: 'slot-1' }),
      )
    })
  })

  describe('polling', () => {
    it('loads messages on mount', async () => {
      mockGet.mockResolvedValue({
        messages: [{ role: 'user', content: 'hi', cls: '' }],
        running: false,
        title: 'Session',
      })

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="test-slot" />)
      })

      expect(mockGet).toHaveBeenCalledWith('/api/chat/slots/' + encodeURIComponent('test-slot'))
    })

    it('polls at 5000ms interval when idle', async () => {
      mockGet.mockResolvedValue({ messages: [], running: false, title: '' })

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })

      mockGet.mockClear()

      await act(async () => {
        vi.advanceTimersByTime(5000)
      })

      expect(mockGet).toHaveBeenCalledTimes(1)
    })

    it('polls at 1000ms interval when running', async () => {
      mockGet.mockResolvedValue({ messages: [], running: true, title: '' })

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })

      // Flush the initial loadMessages promise to let running=true settle into state
      await act(async () => {
        await Promise.resolve()
      })

      mockGet.mockClear()

      await act(async () => {
        vi.advanceTimersByTime(1000)
      })

      // Should have polled after 1000ms (fast rate)
      expect(mockGet).toHaveBeenCalled()
    })

    it('shows streaming indicator when running', async () => {
      mockGet.mockResolvedValue({ messages: [], running: true, title: 'My Session' })

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })

      // Advance timers so React Query's internal scheduling fires
      await act(async () => {
        vi.advanceTimersByTime(100)
      })

      expect(screen.getByText('streaming')).toBeInTheDocument()
    })

    it('shows title from API data', async () => {
      mockGet.mockResolvedValue({ messages: [], running: false, title: 'My Custom Title' })

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })

      // Advance timers so React Query's internal scheduling fires
      await act(async () => {
        vi.advanceTimersByTime(100)
      })

      expect(screen.getByText('My Custom Title')).toBeInTheDocument()
    })

    it('shows slotKey as fallback when no title', async () => {
      mockGet.mockResolvedValue({ messages: [], running: false })

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="fallback-slot" />)
      })

      expect(screen.getByText('fallback-slot')).toBeInTheDocument()
    })
  })

  describe('error handling', () => {
    it('tolerates loadMessages failure gracefully', async () => {
      mockGet.mockRejectedValue(new Error('Network error'))

      // Should not throw
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })

      // Component still renders
      expect(screen.getByLabelText('Chat message')).toBeInTheDocument()
    })

    it('tolerates send failure gracefully (SSE response expected)', async () => {
      mockPost.mockRejectedValue(new Error('JSON parse error'))

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })

      await act(async () => {
        fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'test' } })
      })

      // Should not throw
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      })

      // Advance timers so React Query processes the rejected promise chain
      await act(async () => {
        vi.advanceTimersByTime(100)
      })

      expect((screen.getByLabelText('Chat message') as HTMLInputElement).disabled).toBe(false)
    })
  })
})

describe('ChatEmbed follow-up options', () => {
  // Regression for #3304: ChatMessageList strips `[OPTIONS: a | b]` markers out of
  // the rendered prose, but ChatEmbed never offered those choices anywhere else --
  // an agent's follow-up question left the embedding app's user with nothing to
  // click, unlike the main chat and side panel which render a row of pills.
  it('renders a follow-up pill for each option in the last assistant turn', async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next step? [OPTIONS: Run tests | Skip]' }],
      running: false,
      title: '',
    })
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    expect(screen.getByRole('button', { name: 'Run tests' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Skip' })).toBeInTheDocument()
  })

  it('renders no follow-up bar when the last turn carries no options', async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Just a plain reply.' }],
      running: false,
      title: '',
    })
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    expect(screen.queryByText('Run tests')).toBeNull()
  })

  it('suppresses the follow-up bar while the turn is still streaming', async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next step? [OPTIONS: Run tests | Skip]' }],
      running: true,
      title: '',
    })
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    expect(screen.queryByText('Run tests')).toBeNull()
  })

  it('a later user message clears the previous turn\'s options', async () => {
    mockGet.mockResolvedValue({
      messages: [
        { role: 'assistant', content: 'Next step? [OPTIONS: Run tests | Skip]' },
        { role: 'user', content: 'Run tests' },
      ],
      running: false,
      title: '',
    })
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    expect(screen.queryByText('Run tests')).toBeNull()
  })

  it('picking an option edits the draft instead of sending immediately', async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next step? [OPTIONS: Run tests | Skip]' }],
      running: false,
      title: '',
    })
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    const input = screen.getByLabelText('Chat message') as HTMLInputElement

    // Chip clicks are debounced 220ms (so a double-click can still fire the
    // distinct "send now" gesture) whenever onSend is supplied, as it is here.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Run tests' }))
      vi.advanceTimersByTime(250)
    })

    expect(input.value).toBe('Run tests')
    expect(mockPost).not.toHaveBeenCalledWith('/api/chat?ws=1', expect.anything())
  })

  it('picking an option twice removes it from the draft again', async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'Next step? [OPTIONS: Run tests | Skip]' }],
      running: false,
      title: '',
    })
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    const input = screen.getByLabelText('Chat message') as HTMLInputElement

    // Chip clicks are debounced 220ms (so a double-click can still fire the
    // distinct "send now" gesture) whenever onSend is supplied, as it is here.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Run tests' }))
      vi.advanceTimersByTime(250)
    })
    expect(input.value).toBe('Run tests')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Run tests' }))
      vi.advanceTimersByTime(250)
    })
    expect(input.value).toBe('')
  })

  // Pins the deliberate exclusion recorded at ChatEmbed's deriveFollowUpOptions
  // destructure (#6057): this embed is NOT a plan-capable host. A message whose
  // derivation yields followUpIsPlan=true still keeps its chips on the
  // composer-draft path. Inverse of the ChatPane/ChatPage dispatch tests (same
  // plan fixture: header + stage line + protocol footer, which is what makes
  // parseOptions set isPlan).
  it('a plan-shaped chip edits the draft and never dispatches a plan action', async () => {
    const planMessages = [
      { role: 'user', content: 'plan it' },
      { role: 'assistant', content: '📋 Plan for: ship it\n\nStage 1: build the thing\n\n[OPTION: Go | Go All | Cancel]' },
    ]
    // Premise pin: the fixture MUST derive as a plan, or this test silently
    // degrades into a duplicate of the plain follow-up test above while staying
    // green (e.g. if the plan grammar tightens and only this copy drifts).
    expect(deriveFollowUpOptions(planMessages as ChatMessage[], false).followUpIsPlan).toBe(true)
    // Plan dispatch, wherever it is wired, goes through the global api client's
    // planAction (usePlanActionMutation) -- NOT the useAppApi() object mocked as
    // mockPost. Spy the real transport so a future wiring that dispatches AND
    // fills the draft cannot sail through green. Stubbed (not call-through) so
    // that failing case surfaces as this test's own assertion, not as an
    // unhandled rejection from a real fetch under jsdom.
    const planActionSpy = vi.spyOn(api, 'planAction').mockResolvedValue({ ok: true })

    mockGet.mockResolvedValue({ messages: planMessages, running: false, title: '' })
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-plan-1" />)
    })
    await act(async () => {
      vi.advanceTimersByTime(100)
    })
    const input = screen.getByLabelText('Chat message') as HTMLInputElement
    expect(screen.getByText('Go')).toBeInTheDocument()

    // Chip clicks are debounced 220ms (so a double-click can still fire the
    // distinct "send now" gesture) whenever onSend is supplied, as it is here.
    await act(async () => {
      fireEvent.click(screen.getByText('Go'))
      vi.advanceTimersByTime(250)
    })

    // Composer-draft path: the label lands in the input, exactly like a plain
    // follow-up chip. This is the load-bearing assertion -- a dispatch branch
    // returns before the draft append (per ChatPane), so wiring dispatch into
    // this path would red this line.
    expect(input.value).toBe('Go')
    // And no dispatch on either client: not the embed's own API surface, not
    // the global client's plan-action transport.
    expect(mockPost).not.toHaveBeenCalled()
    expect(planActionSpy).not.toHaveBeenCalled()
  })
})

describe('ChatEmbed approvals', () => {
  // An embedded agent that hits a permission prompt must be actionable. The
  // group header only renders Approve/Reject when an onApprove handler is
  // supplied; the embed used to supply none, so "Approval needed" was a dead
  // label and the worker blocked until the runner timed out.
  it('supplies an approval handler to the message list', async () => {
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByTestId('chat-message-list')).toHaveAttribute('data-can-approve', 'true')
  })

  it('POSTs the decision to the slot approval endpoint with the request id', async () => {
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      screen.getByTestId('mock-approve').click()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(mockPost).toHaveBeenCalledWith('/api/chat/slots/slot-1/approve', {
      action: 'approved',
      request_id: 'appr-1',
    })
  })

  // Regression: /api/approvals/{id}/{action} accepts only approve|reject, so
  // routing 'trust' through it downgraded the decision to a one-shot approve --
  // the card read "Trusted" while the next tool call prompted again. The slot
  // endpoint carries the decision verbatim.
  it('sends Trust as trust, not as a plain approve', async () => {
    await act(async () => {
      renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      await vi.advanceTimersByTimeAsync(0)
    })
    // The embed must DECLARE the trust tier: CollapsibleToolGroup is fail-closed
    // and only renders the Trust button on a canTrust mount (#5434). Dropping
    // the flag would silently remove Trust from every embedded approval row.
    expect(screen.getByTestId('chat-message-list').dataset.canTrust).toBe('true')
    await act(async () => {
      screen.getByTestId('mock-trust').click()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(mockPost).toHaveBeenCalledWith('/api/chat/slots/slot-1/approve', {
      action: 'trust',
      request_id: 'appr-1',
    })
    expect(mockPost).not.toHaveBeenCalledWith(expect.stringContaining('/api/approvals/'), expect.anything())
  })
})

