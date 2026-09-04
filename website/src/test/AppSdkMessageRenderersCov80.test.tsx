import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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
// The real helper resolves FALSE on a refused write and rejects only on a genuine
// throw, so the mock is driven both ways below — a resolved false read as success
// is how a copy button claims to have filled an empty clipboard.
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(async () => true),
}))
import { copyToClipboard } from '../utils/clipboard'
const mockedCopy = vi.mocked(copyToClipboard)

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

/**
 * The expanded tool panel's own affordances (#5984). The panel is `border-box`,
 * so `max-h-40` (160px) less `p-2` and its borders leaves 142px at `leading-4`
 * -- EIGHT visible lines, measured in the built app. These pin the cue, the
 * raised cap, and -- the load-bearing one -- that copy takes the WHOLE panel
 * text rather than whatever the box scrolled into view.
 */
describe('ToolCallPill expanded output panel', () => {
  const LONG = Array.from({ length: 40 }, (_, i) => `zzq-line-${i}`).join('\n')

  /** Open the panel. The pill's own toggle is the first button in the row. */
  async function expandPanel() {
    await userEvent.click(screen.getAllByRole('button')[0])
  }

  it('offers the cue at NINE lines, which the panel cannot show', async () => {
    // The boundary a ten-line threshold missed: nine lines overflow (scrollHeight
    // 192 against clientHeight 158) yet sat below the old budget, so the output
    // was clipped in silence -- exactly the reported defect, one line down.
    const nine = Array.from({ length: 9 }, (_, i) => `zzq-n${i}`).join('\n')
    render(<ToolCallPill message={msg({ role: 'tool_result', content: nine })} running={false} />)
    await expandPanel()
    expect(nine.split('\n')).toHaveLength(9)
    expect(nine.length).toBeLessThan(200) // so the char budget cannot be what fires
    expect(screen.getByRole('button', { name: 'Show more' })).toBeInTheDocument()
  })

  it('offers the expand control only when the panel actually clips', async () => {
    const clipped = render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    expect(screen.getByRole('button', { name: 'Show more' })).toBeInTheDocument()
    clipped.unmount()

    render(<ToolCallPill message={msg({ role: 'tool_result', content: 'zzq-one\nzzq-two' })} running={false} />)
    await expandPanel()
    // Control on the same axis: prove the panel really OPENED, so an absent cue
    // cannot be read off a panel that never rendered.
    expect(document.querySelector('pre')?.textContent).toContain('zzq-two')
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
  })

  it('offers the cue for ONE line long enough to wrap past the box', async () => {
    // The line count alone cannot see this case, and it is the common one: a
    // single-line JSON blob wraps well past 160px. Exercises the char budget
    // specifically -- 1200 chars on one line, so the line test cannot fire.
    const oneLongLine = 'zzq-'.repeat(300)
    render(<ToolCallPill message={msg({ role: 'tool_result', content: oneLongLine })} running={false} />)
    await expandPanel()
    expect(oneLongLine.split('\n')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Show more' })).toBeInTheDocument()
  })

  it('prefixes the panel with the raw label the purpose replaced', async () => {
    render(<ToolCallPill
      message={msg({ role: 'tool_result', content: '🔧 zzq-raw\nzzq-detail', meta: { purpose: 'zzq-purpose' } })}
      running={false}
    />)
    await expandPanel()
    const shown = (document.querySelector('pre') as HTMLElement).textContent as string
    // starts-with, NOT contains: the content already holds 'zzq-raw' on its own
    // line, so a `toContain` assertion is satisfied with the prefix removed --
    // which is exactly why dropping it reddened nothing before this test.
    expect(shown.startsWith('zzq-raw\n\n')).toBe(true)
  })

  it("raises the cap to the main-chat sibling's height and back", async () => {
    render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    expect((document.querySelector('pre') as HTMLElement).className).toContain('max-h-40')

    await userEvent.click(screen.getByRole('button', { name: 'Show more' }))
    const grown = (document.querySelector('pre') as HTMLElement).className
    expect(grown).toContain('max-h-[500px]')
    expect(grown).not.toContain('max-h-40')

    await userEvent.click(screen.getByRole('button', { name: 'Show less' }))
    expect((document.querySelector('pre') as HTMLElement).className).toContain('max-h-40')
  })

  it('copies the whole panel text, not the portion scrolled into view', async () => {
    mockedCopy.mockResolvedValue(true)
    render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))

    expect(mockedCopy).toHaveBeenCalledTimes(1)
    // Exact equality, not `toContain`: a slice or a trim must fail this.
    expect(mockedCopy.mock.calls[0][0]).toBe(LONG)
    expect((mockedCopy.mock.calls[0][0] as string).split('\n')).toHaveLength(40)
  })

  it('copies exactly what the panel shows when a purpose replaced the label', async () => {
    mockedCopy.mockResolvedValue(true)
    render(<ToolCallPill
      message={msg({ role: 'tool_result', content: '🔧 zzq-raw\nzzq-detail', meta: { purpose: 'zzq-purpose' } })}
      running={false}
    />)
    await expandPanel()
    const shown = (document.querySelector('pre') as HTMLElement).textContent as string
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
    // Pinned against the panel's OWN text rather than against a restated
    // formula, so the two cannot drift apart if the prefix rule ever changes.
    expect(mockedCopy.mock.calls[0][0]).toBe(shown)
    // Non-vacuity: two equal empty strings would satisfy the line above.
    expect(shown).toContain('zzq-raw')
    expect(shown).toContain('zzq-detail')
  })

  it('confirms a successful copy on the control', async () => {
    mockedCopy.mockResolvedValue(true)
    render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied!' })).toBeInTheDocument())
  })

  it('reports a REFUSED clipboard write as a failure, not a success', async () => {
    mockedCopy.mockResolvedValue(false)
    render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
    // The failure must land on the shared error surface, not on an icon of this
    // file's own: `AUTOSDE.yaml`'s `errors-use-error-notice` is blocking, and a
    // title-only assertion passed happily while nothing was rendered at all.
    await waitFor(() => expect(screen.getByTestId('tool-panel-copy-error')).toBeInTheDocument())
    expect(screen.getByTestId('tool-panel-copy-error')).toHaveTextContent('Copy failed')
    expect(screen.queryByRole('button', { name: 'Copied!' })).toBeNull()
  })

  it('reports a THROWN clipboard write as a failure', async () => {
    mockedCopy.mockRejectedValue(new Error('zzq-denied'))
    render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(screen.getByTestId('tool-panel-copy-error')).toBeInTheDocument())
  })

  it('offers the agent hand-off on a copy failure, and the notice is dismissible', async () => {
    mockedCopy.mockResolvedValue(false)
    render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
    const notice = await waitFor(() => screen.getByTestId('tool-panel-copy-error'))
    // `askAgent` is an explicit decision here, so pin it: the panel holds no
    // unsaved input, and a reviewer cannot tell that from the hunk alone.
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // Dismissing clears the state rather than leaving a banner welded on.
    await userEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByTestId('tool-panel-copy-error')).toBeNull())
  })

  it('offers the cue on a NARROW panel the char budget cannot see', async () => {
    // The char budget is width-blind: 800 chars over 8 lines is ~100 mono
    // columns, which only holds above roughly 660px. On the companion-chat
    // sidebar this same pill ships on, a short single-line blob wraps past the
    // box and BOTH budgets stay quiet. jsdom reports 0 for either height, so the
    // real browser's measurement is stubbed to stand in for a narrow surface.
    const short = 'zzq-narrow'
    const sh = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(192)
    const ch = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(142)
    try {
      render(<ToolCallPill message={msg({ role: 'tool_result', content: short })} running={false} />)
      await expandPanel()
      // Both budgets must be out of reach, or this proves nothing.
      expect(short.length).toBeLessThan(800)
      expect(short.split('\n')).toHaveLength(1)
      await waitFor(() => expect(screen.getByRole('button', { name: 'Show more' })).toBeInTheDocument())
    } finally {
      sh.mockRestore(); ch.mockRestore()
    }
  })

  it('keeps the toggle once expanded, instead of measuring it away mid-read', async () => {
    // Measuring the EXPANDED box would report "fits" at 500px and remove the
    // control, stranding the reader with no way back to the collapsed view.
    const short = 'zzq-narrow'
    const sh = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(192)
    const ch = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get')
      .mockImplementation(function (this: HTMLElement) {
        // The expanded box is tall enough to hold it; the collapsed one is not.
        return this.className?.includes('max-h-[500px]') ? 500 : 142
      })
    try {
      render(<ToolCallPill message={msg({ role: 'tool_result', content: short })} running={false} />)
      await expandPanel()
      await userEvent.click(await waitFor(() => screen.getByRole('button', { name: 'Show more' })))
      expect(screen.getByRole('button', { name: 'Show less' })).toBeInTheDocument()
      await userEvent.click(screen.getByRole('button', { name: 'Show less' }))
      expect(screen.getByRole('button', { name: 'Show more' })).toBeInTheDocument()
    } finally {
      sh.mockRestore(); ch.mockRestore()
    }
  })

  it('does not report a copy failure until one happens', async () => {
    mockedCopy.mockResolvedValue(true)
    render(<ToolCallPill message={msg({ role: 'tool_result', content: LONG })} running={false} />)
    await expandPanel()
    expect(screen.queryByTestId('tool-panel-copy-error')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied!' })).toBeInTheDocument())
    expect(screen.queryByTestId('tool-panel-copy-error')).toBeNull()
  })
})
