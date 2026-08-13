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
}

vi.mock('./ChatMessageList', () => ({
  default: ({ messages, running, onApprove }: MockChatMessageListProps) => (
    <div data-testid="chat-message-list" data-count={messages.length} data-running={String(running)}
      data-can-approve={String(!!onApprove)}>
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
  default: ({ messages, running, onApprove }: MockChatMessageListProps) => (
    <div data-testid="chat-message-list" data-count={messages.length} data-running={String(running)}
      data-can-approve={String(!!onApprove)}>
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

let queryClient: QueryClient

function renderWithProviders(ui: React.ReactElement) {
  return render(
    React.createElement(QueryClientProvider, { client: queryClient }, ui)
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
  mockPost.mockResolvedValue({})
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

    it('renders send button with aria-label', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      expect(screen.getByLabelText('Send message')).toBeInTheDocument()
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
      expect(screen.getByLabelText('Send message')).toBeDisabled()
    })

    it('send button is enabled when input has text', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const input = screen.getByLabelText('Chat message')
      await act(async () => {
        fireEvent.change(input, { target: { value: 'hello' } })
      })
      expect(screen.getByLabelText('Send message')).not.toBeDisabled()
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
        fireEvent.click(screen.getByLabelText('Send message'))
      })

      // Input should be cleared immediately
      expect(input.value).toBe('')
    })

    it('disables input while sending', async () => {
      // Make post hang so sending stays true
      let resolvePost: () => void = () => {}
      mockPost.mockReturnValue(new Promise<void>(r => { resolvePost = r }))

      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const input = screen.getByLabelText('Chat message') as HTMLInputElement

      await act(async () => {
        fireEvent.change(input, { target: { value: 'hello' } })
      })

      await act(async () => {
        fireEvent.click(screen.getByLabelText('Send message'))
      })

      // While sending, input is disabled
      expect(input).toBeDisabled()

      // Resolve the hanging promise
      await act(async () => {
        resolvePost()
      })
      // Advance timers so React Query processes the mutation settlement
      await act(async () => {
        vi.advanceTimersByTime(100)
      })

      expect(input).not.toBeDisabled()
    })

    it('sends message with correct params via API', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" agent="test-agent" />)
      })

      await act(async () => {
        fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'hello world' } })
      })

      await act(async () => {
        fireEvent.click(screen.getByLabelText('Send message'))
      })

      expect(mockPost).toHaveBeenCalledWith('/api/chat', {
        message: 'hello world',
        slot: 'slot-1',
        agent: 'test-agent',
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
      expect(screen.getByLabelText('Send message')).toBeDisabled()
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

      expect(mockPost).toHaveBeenCalledWith('/api/chat', expect.objectContaining({
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
        fireEvent.click(screen.getByLabelText('Send message'))
      })

      expect(onSend).toHaveBeenCalledWith('hi')
      expect(mockPost).not.toHaveBeenCalledWith('/api/chat', expect.anything())
    })

    it('still posts to /api/chat when no onSend is supplied', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      await act(async () => {
        fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'hi' } })
      })
      await act(async () => {
        fireEvent.click(screen.getByLabelText('Send message'))
      })

      expect(mockPost).toHaveBeenCalledWith(
        '/api/chat',
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
        fireEvent.click(screen.getByLabelText('Send message'))
      })

      // Advance timers so React Query processes the rejected promise chain
      await act(async () => {
        vi.advanceTimersByTime(100)
      })

      expect((screen.getByLabelText('Chat message') as HTMLInputElement).disabled).toBe(false)
    })
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

