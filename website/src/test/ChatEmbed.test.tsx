import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
  /** The virtualized mount's slots: the embed's empty state renders above the rows. */
  transcript?: { aboveRows?: React.ReactNode }
}

vi.mock('./ChatMessageList', () => ({
  default: ({ messages, running, onApprove, canTrust, transcript }: MockChatMessageListProps) => (
    <div data-testid="chat-message-list" data-count={messages.length} data-running={String(running)}
      data-can-approve={String(!!onApprove)} data-can-trust={String(!!canTrust)}>
      {transcript?.aboveRows}
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
  default: ({ messages, running, onApprove, canTrust, transcript }: MockChatMessageListProps) => (
    <div data-testid="chat-message-list" data-count={messages.length} data-running={String(running)}
      data-can-approve={String(!!onApprove)} data-can-trust={String(!!canTrust)}>
      {transcript?.aboveRows}
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
      const input = screen.getByLabelText('Chat message') as HTMLTextAreaElement

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
      const input = screen.getByLabelText('Chat message') as HTMLTextAreaElement

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

    it('Shift+Enter inserts a newline into the draft instead of sending', async () => {
      // userEvent's keystroke pipeline wedges under this file's strict fake
      // timers — measured: setup({ advanceTimers: vi.advanceTimersByTime })
      // and the same with delay: null both time the test out at 15s. The
      // in-tree bridge that works elsewhere pairs advanceTimers with
      // vi.useFakeTimers({ shouldAdvanceTime: true }) (FolderPanel.search),
      // but this file's beforeEach arms strict fake timers for every test and
      // rewiring that risks its 35 neighbours. This test needs no timer
      // control, so it runs on real ones (afterEach/beforeEach re-arm fake
      // timers for the neighbours); nothing here waits on the wall clock, so
      // the 5s refetchInterval cannot fire inside it.
      vi.useRealTimers()
      const user = userEvent.setup()
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const composer = screen.getByLabelText('Chat message') as HTMLTextAreaElement

      await user.click(composer)
      await user.keyboard('line1{Shift>}{Enter}{/Shift}line2')

      // The break must LAND in the draft — a single-line box silently drops it,
      // which is exactly the reported defect. And it must not have sent.
      expect(composer.value).toBe('line1\nline2')
      expect(mockPost).not.toHaveBeenCalled()
    })

    it('the composer is a textarea attached to the bounded auto-grow ref', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const composer = screen.getByLabelText('Chat message') as HTMLTextAreaElement
      // A textarea, resting one row tall — the element swap itself. (happy-dom
      // reports `rows` as a string, so compare through Number.)
      expect(composer.tagName).toBe('TEXTAREA')
      expect(Number(composer.rows)).toBe(1)

      // Attached to useComposerDraft's autosize ref: growing content resizes the
      // box, and past the 240px default cap it stops (scrolls instead). The test
      // DOM never lays out, so scrollHeight is pinned to model the content
      // height — the same technique as useComposerDraft's own auto-grow tests.
      Object.defineProperty(composer, 'scrollHeight', { value: 120, configurable: true })
      await act(async () => {
        fireEvent.change(composer, { target: { value: 'a few\nlines' } })
      })
      expect(composer.style.height).toBe('120px')

      Object.defineProperty(composer, 'scrollHeight', { value: 9999, configurable: true })
      await act(async () => {
        fireEvent.change(composer, { target: { value: 'a very long draft' } })
      })
      expect(composer.style.height).toBe('240px')
    })

    it('the border lives outside the height math and the class contract is pinned', async () => {
      await act(async () => {
        renderWithProviders(<ChatEmbed slotKey="slot-1" />)
      })
      const composer = screen.getByLabelText('Chat message')
      const classes = composer.className.split(/\s+/)

      // The auto-grow effect writes a BORDER-BOX height taken from scrollHeight,
      // which excludes borders — a real border on the auto-sized element leaves
      // the box exactly its border-widths short of the content at EVERY height,
      // clipping descenders on the last visible line. The field's edge is drawn
      // with an inset ring (box-shadow — no layout space) instead, the
      // OAuthRelayAffordance field precedent; forced-colors keeps a real border
      // (ring is invisible there; the borders-back cost is bounded to 2px of
      // scrollable overflow past one line — at rest the floor absorbs it).
      // jsdom/happy-dom do no layout, so this pins the class contract, the
      // same convention flexInputMinWidth.test.tsx states.
      expect(classes).toContain('ring-1')
      expect(classes).toContain('ring-inset')
      expect(classes).toContain('ring-border')
      expect(classes).not.toContain('border')
      expect(classes).not.toContain('border-border')

      // The hook's docstring delegates the EMPTY size to the element's own CSS
      // floor; this floor reproduces the old input's 38px resting geometry and
      // guards the hidden mount: a display:none mount measures scrollHeight 0
      // and would otherwise write a 0px height that persists until the draft
      // next changes.
      expect(classes).toContain('min-h-[38px]')

      // No drag-resize; bounded growth scrolls vertically past the cap.
      expect(classes).toContain('resize-none')
      expect(classes).toContain('overflow-y-auto')

      // The row bottom-aligns so the send button holds its resting position
      // while the box grows.
      const row = composer.parentElement as HTMLElement
      expect(row.className.split(/\s+/)).toContain('items-end')
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

      // Bounded to one page (P5-e): the newest EMBED_PAGE_LIMIT rows, never the whole slot.
      expect(mockGet).toHaveBeenCalledWith('/api/chat/slots/' + encodeURIComponent('test-slot') + '?limit=200')
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

      expect((screen.getByLabelText('Chat message') as HTMLTextAreaElement).disabled).toBe(false)
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
    expect(screen.getByText('Run tests')).toBeInTheDocument()
    expect(screen.getByText('Skip')).toBeInTheDocument()
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
    const input = screen.getByLabelText('Chat message') as HTMLTextAreaElement

    // Chip clicks are debounced 220ms (so a double-click can still fire the
    // distinct "send now" gesture) whenever onSend is supplied, as it is here.
    await act(async () => {
      fireEvent.click(screen.getByText('Run tests'))
      vi.advanceTimersByTime(250)
    })

    expect(input.value).toBe('Run tests')
    expect(mockPost).not.toHaveBeenCalledWith('/api/chat', expect.anything())
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
    const input = screen.getByLabelText('Chat message') as HTMLTextAreaElement

    // Chip clicks are debounced 220ms (so a double-click can still fire the
    // distinct "send now" gesture) whenever onSend is supplied, as it is here.
    // The chip is addressed by BUTTON role: once the first pick lands in the
    // draft, a bare text query would match the textarea's content too.
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
    const input = screen.getByLabelText('Chat message') as HTMLTextAreaElement
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

