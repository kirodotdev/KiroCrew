import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import type { ChatMessage } from '../types'
import type { PasteBlock } from '../utils/pasteTokens'
import { formatToken } from '../utils/pasteTokens'

/**
 * Every leaf the registry delegates to is stubbed, for two reasons: this module's
 * contract is WHICH entry claims a message and WHAT it hands the leaf, and the
 * real leaves (markdown, the assistant footer, the OAuth banner, the subagent card)
 * each have their own tests. `UserMessage` is the exception that still runs real
 * logic — its stub INVOKES `renderContent`, which is how `renderUserContent`
 * (the paste re-collapse) gets exercised through the entry that owns it.
 */
vi.mock('../components/MarkdownRenderer', () => ({
  // `softBreaks` is surfaced because it is part of WHAT an entry hands the leaf.
  default: ({ content, softBreaks }: { content: string; softBreaks?: boolean }) => (
    <div data-testid="md" data-soft-breaks={softBreaks ? '1' : '0'}>{content}</div>
  ),
}))
vi.mock('../components/MessageErrorBoundary', () => ({
  default: ({ children }: { children: ReactNode }) => <div data-testid="boundary">{children}</div>,
}))
vi.mock('../components/PastedChip', () => ({
  default: ({ block }: { block: PasteBlock }) => <span data-testid="chip">{`#${block.seq}`}</span>,
}))
vi.mock('../pages/chat/UserMessage', () => ({
  default: ({ content, meta, timestamp, renderContent }: {
    content: string
    meta?: Record<string, unknown>
    timestamp?: string
    renderContent: (c: string, m?: Record<string, unknown>) => ReactNode
  }) => (
    <div data-testid="user" data-ts={timestamp ?? ''}>{renderContent(content, meta)}</div>
  ),
}))
vi.mock('../pages/chat/AssistantMessage', () => ({
  default: ({ content, showFooter, isStreaming }: {
    content: string; showFooter: boolean; isStreaming: boolean
  }) => (
    <div
      data-testid="assistant"
      data-footer={String(showFooter)}
      data-streaming={String(isStreaming)}
    >{content}</div>
  ),
}))
vi.mock('../pages/chat/SubagentCompletionCard', () => ({
  default: () => <div data-testid="subagent" />,
}))
vi.mock('../pages/chat/subagentCompletion', () => ({
  isSubagentCompletionMessage: (m: ChatMessage) => !!m.meta?.zzqSubagent,
}))
vi.mock('../pages/chat/McpOAuthBanner', () => ({
  renderMcpOAuthMessage: (m: ChatMessage, hide: boolean) =>
    hide && m.meta?.card_owned ? null : <div data-testid="oauth" />,
}))

const {
  ToolCallPill, defaultMessageRenderers, resolveRenderer, mergeRenderers, GROUPED_ROLES,
} = await import('../app-sdk/messageRenderers')

const onFileOpen = vi.fn()

function msg(over: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'assistant', content: '', ...over } as ChatMessage
}

/** Render whatever the registry resolves for `m`, with the surrounding layout
 *  callbacks the list would supply. */
function renderRow(m: ChatMessage, ctx: Partial<Parameters<
  typeof defaultMessageRenderers[number]['render']
>[1]> = {}) {
  const entry = resolveRenderer(m, defaultMessageRenderers)
  const messages = ctx.messages ?? [m]
  const node = entry?.render(m, {
    index: ctx.index ?? messages.indexOf(m),
    messages,
    running: ctx.running ?? false,
    key: ctx.key ?? 'zzq-key',
    onFileOpen,
    hideCardOwnedOAuth: ctx.hideCardOwnedOAuth ?? false,
    autoDeniedIds: ctx.autoDeniedIds ?? new Set<string>(),
    renderTool: ctx.renderTool,
    wrapper: (children, isUser) => (
      <div data-testid="wrapper" data-user={String(!!isUser)}>{children}</div>
    ),
    row: (children, tight) => <div data-testid="row" data-tight={String(!!tight)}>{children}</div>,
  })
  return { entry, ...render(<>{node}</>) }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('messageRenderers — resolution order', () => {
  it('lets a shape match outrank the role that carries it', () => {
    // A stop event travels as role `system`, which the undrawn entry claims.
    const stop = msg({ role: 'system', content: 'zzq-stopped', kind: 'stop_event' })
    expect(resolveRenderer(stop, defaultMessageRenderers)?.id).toBe('stop_event')
    // …and the same shape declared through meta rather than the column.
    const viaMeta = msg({ role: 'system', content: 'zzq-stopped', meta: { kind: 'stop_event' } })
    expect(resolveRenderer(viaMeta, defaultMessageRenderers)?.id).toBe('stop_event')
  })

  it('claims a subagent completion by shape, whatever role carries it', () => {
    const m = msg({ role: 'user', content: 'zzq', meta: { zzqSubagent: true } })
    expect(resolveRenderer(m, defaultMessageRenderers)?.id).toBe('subagent_completion')
    renderRow(m)
    expect(screen.getByTestId('subagent')).toBeInTheDocument()
  })

  it('leaves a role no entry claims unresolved, distinct from deliberately undrawn', () => {
    expect(resolveRenderer(msg({ role: 'zzq-unknown' }), defaultMessageRenderers)).toBeUndefined()
    // Undrawn roles DO have an entry — that is what separates the two.
    const undrawn = resolveRenderer(msg({ role: 'thinking' }), defaultMessageRenderers)
    expect(undrawn?.id).toBe('undrawn')
    expect(undrawn?.render(msg({ role: 'thinking' }), {} as never)).toBeNull()
    expect(resolveRenderer(msg({ role: 'file' }), defaultMessageRenderers)?.id).toBe('file')
  })

  it('only claims a tool message that carries the visible marker', () => {
    expect(resolveRenderer(msg({ role: 'tool', content: '🔧 zzq' }), defaultMessageRenderers)?.id)
      .toBe('tool')
    // The hidden deny sibling is read for the flag and never drawn.
    expect(resolveRenderer(msg({ role: 'tool', content: '🚫 zzq' }), defaultMessageRenderers))
      .toBeUndefined()
  })
})

describe('messageRenderers — mergeRenderers', () => {
  const hostTool = { id: 'tool', roles: ['tool'], render: () => null }
  const hostSystem = { id: 'zzq-system', roles: ['system'], render: () => null }

  it('returns the defaults untouched when a host adds nothing', () => {
    expect(mergeRenderers(undefined)).toBe(defaultMessageRenderers)
    expect(mergeRenderers([])).toBe(defaultMessageRenderers)
  })

  it('replaces a default by reusing its id', () => {
    const merged = mergeRenderers([hostTool])
    expect(merged.filter((r) => r.id === 'tool')).toEqual([hostTool])
  })

  it('keeps host entries behind the shape-matched defaults', () => {
    // A host claiming `system` must not swallow the stop card: a role claim
    // cannot know about `kind`, so it must not outrank a kind check.
    const merged = mergeRenderers([hostSystem])
    const stop = msg({ role: 'system', content: 'zzq', kind: 'stop_event' })
    expect(resolveRenderer(stop, merged)?.id).toBe('stop_event')
    // But a plain system message now goes to the host rather than to `undrawn`.
    expect(resolveRenderer(msg({ role: 'system' }), merged)?.id).toBe('zzq-system')
  })

  it('freezes the grouped-role list so an app cannot mutate the host\'s copy', () => {
    expect([...GROUPED_ROLES]).toEqual(['thinking', 'permission'])
    expect(Object.isFrozen(GROUPED_ROLES)).toBe(true)
  })
})

describe('messageRenderers — conversational rows', () => {
  it('right-aligns a user row and renders its markdown', () => {
    renderRow(msg({ role: 'user', content: 'zzq-user-text', ts: '2026-01-01T00:00:00Z' }))
    expect(screen.getByTestId('wrapper').getAttribute('data-user')).toBe('true')
    expect(screen.getByTestId('md')).toHaveTextContent('zzq-user-text')
    // A timestamp is formatted through the shared footer formatter.
    expect(screen.getByTestId('user').getAttribute('data-ts')).not.toBe('')
  })

  it('omits the timestamp when the message carries none', () => {
    renderRow(msg({ role: 'user', content: 'zzq' }))
    expect(screen.getByTestId('user').getAttribute('data-ts')).toBe('')
  })

  it('re-collapses a history-loaded paste back to a chip', () => {
    // History re-serves the EXPANDED paste; handing hundreds of KB to the markdown
    // renderer freezes the tab, so the block is collapsed back to its token.
    const block: PasteBlock = { id: 'zzq-b1', seq: 1, lines: 4, content: 'zzq\nbig\npaste\nbody' }
    renderRow(msg({
      role: 'user',
      content: `before\n${block.content}\nafter`,
      meta: { pastes: [block] },
    }))
    expect(screen.getByTestId('chip')).toHaveTextContent('#1')
    // The surrounding prose is kept as plain segments, not markdown-parsed.
    expect(screen.queryByTestId('md')).toBeNull()
    expect(screen.getByTestId('boundary').textContent).toContain('before')
    expect(screen.getByTestId('boundary').textContent).toContain('after')
  })

  it('renders an already-tokenised message without re-collapsing', () => {
    const block: PasteBlock = { id: 'zzq-b2', seq: 3, lines: 9, content: 'zzq-body' }
    renderRow(msg({
      role: 'user',
      content: `${formatToken(block)} trailing`,
      meta: { pastes: [block] },
    }))
    expect(screen.getByTestId('chip')).toHaveTextContent('#3')
  })

  it('falls back to markdown when the recorded paste cannot be located', () => {
    const block: PasteBlock = { id: 'zzq-b3', seq: 2, lines: 5, content: 'zzq-absent-body' }
    renderRow(msg({ role: 'user', content: 'zzq-unrelated-text', meta: { pastes: [block] } }))
    expect(screen.queryByTestId('chip')).toBeNull()
    expect(screen.getByTestId('md')).toHaveTextContent('zzq-unrelated-text')
  })
})

describe('messageRenderers — the assistant footer rule', () => {
  const assistant = msg({ role: 'assistant', content: 'zzq-reply' })

  it('shows the footer once a user turn follows', () => {
    const messages = [assistant, msg({ role: 'user', content: 'zzq-next' })]
    renderRow(assistant, { messages })
    expect(screen.getByTestId('assistant').getAttribute('data-footer')).toBe('true')
  })

  it('withholds it when another assistant reply follows', () => {
    const messages = [assistant, msg({ role: 'assistant', content: 'zzq-more' })]
    renderRow(assistant, { messages })
    expect(screen.getByTestId('assistant').getAttribute('data-footer')).toBe('false')
  })

  it('skips over tool rows to find the next relevant turn', () => {
    const messages = [
      assistant,
      msg({ role: 'tool', content: '🔧 zzq' }),
      msg({ role: 'user', content: 'zzq-next' }),
    ]
    renderRow(assistant, { messages })
    expect(screen.getByTestId('assistant').getAttribute('data-footer')).toBe('true')
  })

  it('shows it on the last reply only once the session goes idle', () => {
    renderRow(assistant, { messages: [assistant], running: true })
    expect(screen.getByTestId('assistant').getAttribute('data-footer')).toBe('false')
  })

  it('shows it on the last reply of an idle session', () => {
    renderRow(assistant, { messages: [assistant], running: false })
    expect(screen.getByTestId('assistant').getAttribute('data-footer')).toBe('true')
  })

  it('never shows it while the reply is still streaming', () => {
    const streaming = msg({ role: 'streaming', content: 'zzq-partial' })
    renderRow(streaming, { messages: [streaming], running: false })
    const node = screen.getByTestId('assistant')
    expect(node.getAttribute('data-streaming')).toBe('true')
    expect(node.getAttribute('data-footer')).toBe('false')
  })
})

describe('messageRenderers — cards, pills and banners', () => {
  it('draws a stop event on the shared StopEventCard, reading its state off meta', () => {
    // A stop row's `content` is the card's own JSON envelope, never prose, so the
    // entry hands the whole message to the card and lets it read `meta.state`.
    // Recipe parity with the dashboard is pinned in AppSdkStopEventCardParity.
    const data = { kind: 'stop_event', id: 'stop-zzq', state: 'stopped', outcome: null }
    const json = JSON.stringify(data)
    const m = msg({ role: 'system', content: json, cls: json, meta: data })
    const { entry } = renderRow(m)
    expect(entry?.id).toBe('stop_event')
    const card = screen.getByTestId('stop-event-card')
    expect(screen.getByTestId('row')).toContainElement(card)
    expect(card.getAttribute('data-state')).toBe('stopped')
  })

  it('draws error and notice rows', () => {
    const failed = renderRow(msg({ role: 'error', content: 'zzq-error-text' }))
    expect(screen.getByTestId('row')).toHaveTextContent('zzq-error-text')
    failed.unmount()
    renderRow(msg({ role: 'notice', content: 'zzq-notice-text' }))
    expect(screen.getByTestId('row')).toHaveTextContent('zzq-notice-text')
  })

  it('strips the cron envelope from an injected message and labels it', () => {
    renderRow(msg({
      role: 'inject',
      content: '[Cron notification from "zzq-job"]\nzzq-injected-body\n[End of cron notification]',
      meta: { cronLabel: 'zzq-job' },
    }))
    expect(screen.getByTestId('md')).toHaveTextContent('zzq-injected-body')
    expect(screen.getByTestId('wrapper').textContent).toContain('zzq-job')
  })

  it('leaves an unlabelled injection verbatim', () => {
    renderRow(msg({ role: 'inject', content: '[Cron notification from "x"]\nzzq-raw' }))
    expect(screen.getByTestId('md').textContent).toContain('[Cron notification from "x"]')
  })

  // A note's [OPTIONS:] marker is consumed into the pill row, so the bubble must not
  // ALSO print it -- the user would see the same choices twice.
  it('strips the OPTIONS marker from the text a note bubble renders', () => {
    renderRow(msg({
      role: 'inject',
      cls: 'reconcile-note',
      content: 'zzq-note-prose [OPTIONS: Fix | Skip]',
    }))
    const rendered = screen.getByTestId('md').textContent ?? ''
    expect(rendered).not.toContain('[OPTIONS:')
    expect(rendered).toContain('zzq-note-prose')
  })

  it('keeps the marker verbatim on an inject row that is NOT a note', () => {
    renderRow(msg({ role: 'inject', content: 'zzq-cron-prose [OPTIONS: Fix | Skip]' }))
    const rendered = screen.getByTestId('md').textContent ?? ''
    expect(rendered).toContain('[OPTIONS: Fix | Skip]')
  })

  // A rehydrated note has no `cls` -- history persists it only for role="system" --
  // so provenance in `meta` is what keeps the strip alive across a restart.
  it('strips the marker from a note rehydrated from history without its class', () => {
    renderRow(msg({
      role: 'inject',
      cls: '',
      content: 'zzq-reloaded-prose [OPTIONS: Fix | Skip]',
      meta: { noteSession: 'chat-1844-1787619403' },
    }))
    const rendered = screen.getByTestId('md').textContent ?? ''
    expect(rendered).not.toContain('[OPTIONS:')
    expect(rendered).toContain('zzq-reloaded-prose')
  })

  it('keeps the marker on a classless inject row carrying no note provenance', () => {
    renderRow(msg({ role: 'inject', cls: '', content: 'zzq-bare-prose [OPTIONS: Fix | Skip]' }))
    const rendered = screen.getByTestId('md').textContent ?? ''
    expect(rendered).toContain('[OPTIONS: Fix | Skip]')
  })

  // Preserved whitespace inherits into the markdown blocks, where each stray
  // newline becomes a line box; jsdom has no layout, so assert the cause.
  it('does not preserve source whitespace on the markdown-rendering inject bubble', () => {
    renderRow(msg({ role: 'inject', content: 'zzq-body' }))
    const bubble = screen.getByTestId('md').closest('.msg-content')
    expect(bubble).not.toBeNull()
    expect(bubble!.className).not.toContain('whitespace-pre-wrap')
  })

  // The other half: with preserved whitespace gone, soft breaks are what keep a
  // multi-line notification on separate lines.
  it('asks for soft breaks so a multi-line inject body keeps its line breaks', () => {
    renderRow(msg({ role: 'inject', content: 'zzq-line-one\nzzq-line-two\nzzq-line-three' }))
    expect(screen.getByTestId('md').getAttribute('data-soft-breaks')).toBe('1')
  })

  it('drops an OAuth banner a Connections card already owns', () => {
    const m = msg({ role: 'mcp_oauth', meta: { card_owned: true, oauth_url: 'zzq' } })
    const owned = renderRow(m, { hideCardOwnedOAuth: true })
    expect(screen.queryByTestId('oauth')).toBeNull()
    expect(screen.queryByTestId('row')).toBeNull()
    owned.unmount()

    renderRow(m, { hideCardOwnedOAuth: false })
    expect(screen.getByTestId('oauth')).toBeInTheDocument()
  })

  it('routes tool rows through a host-supplied renderer when one is given', () => {
    const renderTool = vi.fn(() => <div data-testid="host-tool" />)
    renderRow(msg({ role: 'tool_result', content: 'zzq' }), { renderTool })
    expect(screen.getByTestId('host-tool')).toBeInTheDocument()
    expect(renderTool).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('row').getAttribute('data-tight')).toBe('true')
  })

  it('passes the auto-denied flag through for the call a gate blocked', () => {
    const renderTool = vi.fn(() => null)
    // The flag reaches ToolCallPill, not the host hook — with a host renderer the
    // host draws its own row, so assert the default path instead.
    renderRow(
      msg({ role: 'tool', content: '🔧 zzq-blocked', meta: { tool_call_id: 'zzq-tc' } }),
      { autoDeniedIds: new Set(['zzq-tc']) },
    )
    expect(screen.getByTestId('row').textContent).toContain('zzq-blocked')
    expect(renderTool).not.toHaveBeenCalled()
  })
})

describe('ToolCallPill', () => {
  it('prefers the backend purpose over the raw command', () => {
    render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 zzq-raw-command\nsecond line', meta: { purpose: 'zzq-purpose' } })}
      running={false}
    />)
    expect(screen.getByRole('button', { name: /zzq-purpose/ })).toBeInTheDocument()
  })

  it('falls back to the first line of the command, marker stripped', () => {
    render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 zzq-raw-command\nsecond line' })}
      running={false}
    />)
    const label = screen.getByRole('button').textContent as string
    expect(label).toContain('zzq-raw-command')
    expect(label).not.toContain('🔧')
    expect(label).not.toContain('second line')
  })

  it('falls back to the role when there is nothing to label with', () => {
    render(<ToolCallPill message={msg({ role: 'tool_call', content: '' })} running={false} />)
    expect(screen.getByRole('button')).toHaveTextContent('tool_call')
  })

  it('expands to the full command, prefixed by the raw label it replaced', async () => {
    render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 zzq-raw\nzzq-detail', meta: { purpose: 'zzq-purpose' } })}
      running={false}
    />)
    await userEvent.click(screen.getByRole('button'))
    const pre = document.querySelector('pre') as HTMLElement
    expect(pre.textContent).toContain('zzq-raw')
    expect(pre.textContent).toContain('zzq-detail')
  })

  it('animates only while the session is actually running', () => {
    // A tool call left un-terminated by a dropped turn must not spin forever and
    // make an idle transcript look busy.
    const running = render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 zzq' })} running
    />)
    expect(document.querySelector('.animate-spin')).not.toBeNull()
    running.unmount()

    render(<ToolCallPill message={msg({ role: 'tool', content: '🔧 zzq' })} running={false} />)
    expect(document.querySelector('.animate-spin')).toBeNull()
  })

  it('treats an auto-denied call as terminal, so it never spins', () => {
    render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 zzq' })} running autoDenied
    />)
    expect(document.querySelector('.animate-spin')).toBeNull()
    expect((screen.getByRole('button') as HTMLElement).className).toContain('text-warn')
  })

  it('tones a rejected, a finished and a pending-permission call differently', () => {
    const rejected = render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 zzq', meta: { resolved: 'rejected' } })}
      running
    />)
    expect((screen.getByRole('button') as HTMLElement).className).toContain('text-danger')
    rejected.unmount()

    // The backend persists the raw token, so a reject-once arrives here as
    // `rejected_once`. An equality match on 'rejected' would tone the most
    // deliberate denial a human can make as if nothing had been decided.
    const rejectedOnce = render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 zzq', meta: { resolved: 'rejected_once' } })}
      running
    />)
    expect((screen.getByRole('button') as HTMLElement).className).toContain('text-danger')
    rejectedOnce.unmount()

    const done = render(<ToolCallPill
      message={msg({ role: 'tool_result', content: 'zzq' })} running
    />)
    expect((screen.getByRole('button') as HTMLElement).className).toContain('text-ok')
    done.unmount()

    render(<ToolCallPill message={msg({ role: 'permission', content: 'zzq' })} running />)
    expect((screen.getByRole('button') as HTMLElement).className).toContain('text-warn')
  })

  it('offers a file affordance for a safe path, and calls back with it', async () => {
    render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 Read file', meta: { input_preview: JSON.stringify({ path: 'src/zzq/deep/file.ts' }) } })}
      running={false}
      onFileOpen={onFileOpen}
    />)
    const open = screen.getByRole('button', { name: /file\.ts/ })
    await userEvent.click(open)
    expect(onFileOpen).toHaveBeenCalledWith('src/zzq/deep/file.ts')
  })

  it('offers no file affordance without a handler to receive it', () => {
    render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 Read file', meta: { input_preview: JSON.stringify({ path: 'src/zzq/file.ts' }) } })}
      running={false}
    />)
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  it('refuses an unsafe path rather than offering to open it', () => {
    render(<ToolCallPill
      message={msg({ role: 'tool', content: '🔧 Read file', meta: { input_preview: JSON.stringify({ path: '../../etc/zzq-passwd' }) } })}
      running={false}
      onFileOpen={onFileOpen}
    />)
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })
})
