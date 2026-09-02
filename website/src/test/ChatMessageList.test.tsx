import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React, { type ReactNode } from 'react'
import type { ChatMessage } from '../types'
import type { TurnItem } from '../pages/chat/types'

// ── Mocks for heavy child components ──
// Each mock types only the props it actually reads.

vi.mock('../pages/chat/AssistantMessage', () => ({
  default: (props: { content: string; isStreaming?: boolean }) => (
    <div data-testid="assistant-message" data-streaming={String(props.isStreaming)}>
      {props.content}
    </div>
  ),
}))

vi.mock('../pages/chat/UserMessage', () => ({
  default: (props: { content: string }) => (
    <div data-testid="user-message">{props.content}</div>
  ),
}))

vi.mock('../pages/chat/CollapsibleToolGroup', () => ({
  default: ({ children, count, hasPermission, pendingPermCount, canTrust, onApproveBatch, permissionMetas }: {
    children?: ReactNode; count?: number; hasPermission?: boolean; pendingPermCount?: number; canTrust?: boolean
    onApproveBatch?: (decision: string) => Promise<unknown>
    permissionMetas?: Record<string, unknown>[]
  }) => (
    <div
      data-testid="collapsible-tool-group"
      data-count={count}
      data-has-permission={String(hasPermission)}
      data-pending-perm-count={pendingPermCount}
      data-perm-metas-count={permissionMetas ? permissionMetas.length : undefined}
      data-can-trust={String(!!canTrust)}
      data-has-batch={String(!!onApproveBatch)}
    >
      {onApproveBatch && (
        <button data-testid="ctg-batch-approve" onClick={() => { void onApproveBatch('approved') }}>batch</button>
      )}
      {children}
    </div>
  ),
}))

vi.mock('../pages/chat/TurnBlock', () => ({
  default: ({ turn, renderItem }: {
    turn: { items: TurnItem[] }; renderItem: (item: TurnItem, i: number) => ReactNode
  }) => (
    <div data-testid="turn-block">
      {turn.items.map((item: TurnItem, i: number) => (
        <div key={i}>{renderItem(item, i)}</div>
      ))}
    </div>
  ),
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span data-testid="markdown">{content}</span>,
}))

import ChatMessageList from '../app-sdk/ChatMessageList'

beforeEach(() => {
  vi.restoreAllMocks()
})

function msg(role: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { role, content, cls: '', ...extra }
}

describe('ChatMessageList', () => {
  describe('empty state', () => {
    it('renders nothing when messages list is empty', () => {
      const { container } = render(<ChatMessageList messages={[]} running={false} />)
      // Should render the fragment with no children
      expect(container.innerHTML).toBe('')
    })
  })

  describe('single messages', () => {
    it('renders a single user message', () => {
      render(<ChatMessageList messages={[msg('user', 'Hello')]} running={false} />)
      expect(screen.getByTestId('user-message')).toBeInTheDocument()
      expect(screen.getByTestId('user-message').textContent).toBe('Hello')
    })

    it('renders a single assistant message', () => {
      render(<ChatMessageList messages={[msg('assistant', 'Hi there')]} running={false} />)
      expect(screen.getByTestId('assistant-message')).toBeInTheDocument()
      expect(screen.getByTestId('assistant-message').textContent).toBe('Hi there')
    })

    it('renders streaming message with isStreaming flag', () => {
      render(<ChatMessageList messages={[msg('streaming', 'partial...')]} running={true} />)
      const el = screen.getByTestId('assistant-message')
      expect(el.getAttribute('data-streaming')).toBe('true')
    })

    it('renders error messages', () => {
      render(<ChatMessageList messages={[msg('error', 'Something went wrong')]} running={false} />)
      expect(screen.getByText('Something went wrong')).toBeInTheDocument()
    })

    it('renders inject messages with cron label using Clock icon (not emoji)', () => {
      const cronMsg = msg('inject', '[Cron notification from "test"]\nDo the thing\n[End of cron notification]', {
        meta: { cronLabel: 'test' },
      })
      render(<ChatMessageList messages={[cronMsg]} running={false} />)
      // The Clock icon from lucide-react renders as an SVG
      // and the cron label text should be present
      expect(screen.getByText('test')).toBeInTheDocument()
      // The content should have the cron wrappers stripped
      expect(screen.getByTestId('markdown').textContent).toBe('Do the thing')
    })

    it('renders inject without cron label as plain content', () => {
      const injectMsg = msg('inject', 'Injected text')
      render(<ChatMessageList messages={[injectMsg]} running={false} />)
      expect(screen.getByTestId('markdown').textContent).toBe('Injected text')
    })

    it('renders stop_event messages via kind field', () => {
      // The row draws the shared StopEventCard, which reads `meta.state` — a
      // stop event's `content` is the card's own JSON envelope, never prose.
      const stopMsg = msg('user', 'Session stopped', { kind: 'stop_event', meta: { state: 'stopped' } })
      render(<ChatMessageList messages={[stopMsg]} running={false} />)
      expect(screen.getByTestId('stop-event-card').getAttribute('data-state')).toBe('stopped')
    })

    it('renders stop_event messages via meta.kind', () => {
      const stopMsg = msg('user', 'Halted', { meta: { kind: 'stop_event', state: 'stop_failed_reset' } })
      render(<ChatMessageList messages={[stopMsg]} running={false} />)
      expect(screen.getByTestId('stop-event-card').getAttribute('data-state')).toBe('stop_failed_reset')
    })

    it('returns null for thinking messages rendered individually', () => {
      // Thinking messages are GROUPABLE — if alone they form a group.
      // But if we test the renderMessage path for role=thinking, it returns null.
      // In practice, a single thinking message becomes a group. Let's verify
      // that a list with just system/done/queued/file roles renders nothing.
      const msgs = [
        msg('system', 'system msg'),
        msg('done', 'done msg'),
        msg('queued', 'queued msg'),
        msg('file', '/path/to/file'),
      ]
      const { container } = render(<ChatMessageList messages={msgs} running={false} />)
      expect(container.innerHTML).toBe('')
    })
  })

  describe('tool messages', () => {
    it('renders tool_call and tool_result as pills', () => {
      const msgs = [
        msg('tool_call', 'Running command'),
        msg('tool_result', 'Command output'),
      ]
      render(<ChatMessageList messages={[msg('user', 'Q'), ...msgs]} running={false} />)
      // Tool call and tool result should render as buttons (ToolCallPill)
      const buttons = screen.getAllByRole('button')
      expect(buttons.length).toBeGreaterThanOrEqual(2)
    })

    it('renders tool messages starting with wrench emoji as pills', () => {
      render(
        <ChatMessageList
          messages={[msg('user', 'Q'), msg('tool', '🔧 read_file /foo.ts')]}
          running={false}
        />,
      )
      const btn = screen.getByRole('button')
      expect(btn.textContent).toContain('read_file /foo.ts')
    })
  })

  describe('tool pill parity with the main chat', () => {
    // The embed used to render EVERY tool call as one accent-purple spinning
    // wrench with the raw command truncated to 80 chars — visually unrelated to
    // a main session, where the same call shows its purpose, a status colour and
    // a file affordance. These lock the parity so it can't silently drift back.

    it('prefers the backend purpose over the raw command', () => {
      render(
        <ChatMessageList
          messages={[
            msg('user', 'Q'),
            msg('tool_call', 'Running: cd /very/long/path && python -c "..."', {
              meta: { purpose: 'Add teams_data dict guard in the parse block.' },
            } as Partial<ChatMessage>),
          ]}
          running
        />,
      )
      expect(screen.getByText('Add teams_data dict guard in the parse block.')).toBeInTheDocument()
    })

    it('does not spin any icon once the session is idle', () => {
      const { container } = render(
        <ChatMessageList messages={[msg('user', 'Q'), msg('tool_call', 'Running: ls')]} running={false} />,
      )
      expect(container.querySelectorAll('.animate-spin').length).toBe(0)
    })

    it('spins only while the session is running', () => {
      const { container } = render(
        <ChatMessageList messages={[msg('user', 'Q'), msg('tool_call', 'Running: ls')]} running />,
      )
      expect(container.querySelectorAll('.animate-spin').length).toBe(1)
    })

    it('marks a completed call done rather than perpetually busy', () => {
      const { container } = render(
        <ChatMessageList messages={[msg('user', 'Q'), msg('tool_result', 'output')]} running />,
      )
      expect(container.querySelectorAll('.animate-spin').length).toBe(0)
      expect(container.querySelector('.text-ok')).toBeTruthy()
    })

    it('offers a file affordance when the tool input names a safe path', () => {
      const onFileOpen = vi.fn()
      render(
        <ChatMessageList
          messages={[
            msg('user', 'Q'),
            msg('tool_call', '🔧 read_file', {
              meta: { input_preview: '{"path":"/home/me/project/loader.py"}' },
            } as Partial<ChatMessage>),
          ]}
          running={false}
          onFileOpen={onFileOpen}
        />,
      )
      const chip = screen.getByRole('button', { name: 'Open /home/me/project/loader.py' })
      chip.click()
      expect(onFileOpen).toHaveBeenCalledWith('/home/me/project/loader.py')
    })
  })

  describe('grouping: thinking + permission messages', () => {
    it('groups consecutive thinking messages into CollapsibleToolGroup', () => {
      const msgs: ChatMessage[] = [
        msg('user', 'Do stuff'),
        msg('thinking', 'Let me think...'),
        msg('thinking', 'Almost done...'),
        msg('assistant', 'Done.'),
      ]
      render(<ChatMessageList messages={msgs} running={false} />)
      const group = screen.getByTestId('collapsible-tool-group')
      expect(group).toBeInTheDocument()
      // count is the number of non-permission messages in the group
      expect(group.getAttribute('data-count')).toBe('2')
    })

    it('includes permission messages in groups (not skipped)', () => {
      const msgs: ChatMessage[] = [
        msg('user', 'Run a command'),
        msg('thinking', 'Analyzing...'),
        msg('permission', 'Allow read access?', { meta: { approval_id: 'a1' } }),
        msg('thinking', 'More thought...'),
        msg('assistant', 'Here you go.'),
      ]
      render(<ChatMessageList messages={msgs} running={false} />)
      const group = screen.getByTestId('collapsible-tool-group')
      expect(group).toBeInTheDocument()
      // has-permission should be true because there's an unresolved permission
      expect(group.getAttribute('data-has-permission')).toBe('true')
      expect(group.getAttribute('data-pending-perm-count')).toBe('1')
      // count is non-permission messages: 2 thinking messages
      expect(group.getAttribute('data-count')).toBe('2')
    })

    it('resolved permissions are not counted as pending', () => {
      const msgs: ChatMessage[] = [
        msg('user', 'Run it'),
        msg('thinking', 'think'),
        msg('permission', 'Allow?', { meta: { approval_id: 'a1', resolved: 'approved' } }),
        msg('assistant', 'Done'),
      ]
      render(<ChatMessageList messages={msgs} running={false} />)
      const group = screen.getByTestId('collapsible-tool-group')
      // All permissions are resolved, so has-permission should be false
      expect(group.getAttribute('data-has-permission')).toBe('false')
      expect(group.getAttribute('data-pending-perm-count')).toBe('0')
    })

    it('passes one meta per pending perm so the count promise stays honest (#6404)', () => {
      // A pending perm with NO meta must still contribute a metas entry, so
      // permissionMetas.length === pendingPermCount and CollapsibleToolGroup can
      // render its "No preview available" placeholder for it — otherwise the
      // "Review all N" note would promise more rows than render (silent-row gap).
      const msgs: ChatMessage[] = [
        msg('user', 'Run them'),
        msg('permission', 'Allow A?', { meta: { approval_id: 'a1' } }),
        msg('permission', 'Allow B?'),
        msg('permission', 'Allow C?', { meta: { approval_id: 'a3' } }),
        msg('assistant', 'Done'),
      ]
      render(<ChatMessageList messages={msgs} running={false} />)
      const group = screen.getByTestId('collapsible-tool-group')
      expect(group.getAttribute('data-pending-perm-count')).toBe('3')
      expect(group.getAttribute('data-perm-metas-count')).toBe('3')
    })

    it('threads canTrust to the group, and withholds it by default (#5434)', () => {
      const msgs: ChatMessage[] = [
        msg('user', 'Run a command'),
        msg('thinking', 'Analyzing...'),
        msg('permission', 'Allow read access?', { meta: { approval_id: 'a1' } }),
        msg('assistant', 'Here you go.'),
      ]
      // Fail-closed default: a host that says nothing gets no Trust tier —
      // most hosts resolve through the one-shot resolveApproval endpoint,
      // which has no trust verb (#5400, #5434).
      const { unmount } = render(<ChatMessageList messages={msgs} running={false} onApprove={() => {}} />)
      expect(screen.getByTestId('collapsible-tool-group').getAttribute('data-can-trust')).toBe('false')
      unmount()

      // A host whose onApprove records standing trust declares it explicitly.
      render(<ChatMessageList messages={msgs} running={false} onApprove={() => {}} canTrust />)
      expect(screen.getByTestId('collapsible-tool-group').getAttribute('data-can-trust')).toBe('true')
    })

    it('groups at end of messages list are flushed', () => {
      const msgs: ChatMessage[] = [
        msg('user', 'Start'),
        msg('thinking', 'pondering...'),
        msg('thinking', 'still pondering...'),
      ]
      render(<ChatMessageList messages={msgs} running={true} />)
      // The trailing group should still be rendered
      expect(screen.getByTestId('collapsible-tool-group')).toBeInTheDocument()
    })
  })

  describe('turn grouping', () => {
    it('wraps multi-item assistant turns in TurnBlock', () => {
      // A turn needs: working steps + more than 2 items
      const msgs: ChatMessage[] = [
        msg('user', 'Go'),
        msg('thinking', 'hm'),
        msg('thinking', 'ok'),
        msg('assistant', 'step 1'),
        msg('thinking', 'more'),
        msg('assistant', 'step 2'),
        msg('user', 'Next'),
      ]
      render(<ChatMessageList messages={msgs} running={false} />)
      // The items between the two user messages should form a turn
      // if there are > 2 items with working steps
      const turnBlocks = screen.queryAllByTestId('turn-block')
      // Whether it forms a TurnBlock depends on the item count after grouping
      // thinking group + assistant + thinking group + assistant = 4 items > 2
      expect(turnBlocks.length).toBeGreaterThanOrEqual(1)
    })

    it('does not wrap short sequences in TurnBlock', () => {
      const msgs: ChatMessage[] = [
        msg('user', 'Hi'),
        msg('assistant', 'Hello'),
        msg('user', 'Bye'),
      ]
      render(<ChatMessageList messages={msgs} running={false} />)
      // Only 1 item between user messages, <= 2, so no TurnBlock
      expect(screen.queryByTestId('turn-block')).toBeNull()
    })
  })

  describe('user messages as turn boundaries', () => {
    it('each user message starts a new boundary', () => {
      const msgs: ChatMessage[] = [
        msg('user', 'First'),
        msg('assistant', 'Reply 1'),
        msg('user', 'Second'),
        msg('assistant', 'Reply 2'),
      ]
      render(<ChatMessageList messages={msgs} running={false} />)
      const userMsgs = screen.getAllByTestId('user-message')
      expect(userMsgs).toHaveLength(2)
      expect(userMsgs[0].textContent).toBe('First')
      expect(userMsgs[1].textContent).toBe('Second')
    })
  })

  /**
   * A card-owned OAuth request is annotated by the backend, never dropped — the
   * Connections card reads its approval URL out of that message. Hiding it is
   * this component's call, and only when its host actually renders those cards.
   * The default must stay "render everything": the embed SDK has no cards, and
   * neither does an install with the gallery flag off.
   */
  describe('card-owned OAuth banners', () => {
    const cardOwned = msg('mcp_oauth', '🔐 notion requires authentication.', {
      meta: { server_name: 'notion', oauth_url: 'https://mcp.notion.com/authorize', card_owned: true },
    })

    it('renders a card-owned banner by default', () => {
      render(<ChatMessageList messages={[cardOwned]} running={false} />)
      expect(screen.getByRole('link', { name: /Authorize notion/i })).toBeInTheDocument()
    })

    it('hides a card-owned banner when the host renders the cards', () => {
      render(<ChatMessageList messages={[cardOwned]} running={false} hideCardOwnedOAuth />)
      expect(screen.queryByRole('link', { name: /Authorize notion/i })).toBeNull()
    })

    it('still renders an unannotated banner when the host renders the cards', () => {
      const handAdded = msg('mcp_oauth', '🔐 my-remote requires authentication.', {
        meta: { server_name: 'my-remote', oauth_url: 'https://mine.example.com/authorize' },
      })
      render(<ChatMessageList messages={[handAdded]} running={false} hideCardOwnedOAuth />)
      expect(screen.getByRole('link', { name: /Authorize my-remote/i })).toBeInTheDocument()
    })
  })
})

describe('ChatMessageList — batch approval wiring (Req 4.1-4.4)', () => {
  // The prior bug: onApproveBatch was wired only into hosts that never render
  // approval buttons. These tests exercise the REAL render path (renderItem)
  // through the live-approval component, which is where the wiring must land.
  const perm = (id: string) => msg('permission', 'needs approval', { meta: { approval_id: id } })

  it('wires onApproveBatch across ALL pending ids when a group has >1 pending approval', () => {
    const onApproveBatch = vi.fn(() => Promise.resolve())
    render(
      <ChatMessageList
        messages={[perm('req-a'), perm('req-b'), perm('req-c')]}
        running
        onApprove={() => Promise.resolve()}
        onApproveBatch={onApproveBatch}
      />,
    )
    const group = screen.getByTestId('collapsible-tool-group')
    expect(group).toHaveAttribute('data-pending-perm-count', '3')
    expect(group).toHaveAttribute('data-has-batch', 'true')
    fireEvent.click(screen.getByTestId('ctg-batch-approve'))
    // The host receives EVERY pending id, not just the last one.
    expect(onApproveBatch).toHaveBeenCalledWith(['req-a', 'req-b', 'req-c'], 'approved')
  })

  it('does not wire a batch handler when the host supplies none', () => {
    render(
      <ChatMessageList
        messages={[perm('req-a'), perm('req-b')]}
        running
        onApprove={() => Promise.resolve()}
      />,
    )
    expect(screen.getByTestId('collapsible-tool-group')).toHaveAttribute('data-has-batch', 'false')
  })
})
