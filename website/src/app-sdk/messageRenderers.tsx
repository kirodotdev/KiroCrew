/**
 * Message renderer registry — the role → renderer mapping for a chat transcript.
 *
 * Rendering policy lives here as DATA rather than as control flow inside
 * ChatMessageList, so a surface can add a row type (a queued card, a file chip)
 * or replace one (its own approval UI, its own tool row) without forking the
 * transcript. A host passes extra entries; they win over the defaults.
 *
 * This module must stay free of any store, router, or selector reach: the
 * consumers that most need a shared transcript run outside the dashboard's React
 * root and have no Redux store at all, so a renderer that reaches for a selector
 * is unusable to them. Presentational components from `pages/chat/` are fine
 * (this module already imports several); anything that genuinely needs live app
 * state is supplied BY the host as a registry entry instead.
 */
import React, { memo } from 'react'
import { Clock, LoaderCircle, CircleSlash, CircleAlert, CircleDot, Lock, PanelRight } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { isNoteRow } from '../lib/noteContract'
import { parseOptions } from './protocol'
import { extractToolFilePath } from '../utils/toolFilePath'
import { isSafePath } from '../utils/safePath'
import { isHiddenInvisibleAssistantRow } from '../utils/invisibleText'
import AssistantMessage, { type TurnStats } from '../pages/chat/AssistantMessage'
import { type FileChangeEntry } from '../components/FileChangeChips'
import UserMessage from '../pages/chat/UserMessage'
import { renderMcpOAuthMessage } from '../pages/chat/McpOAuthBanner'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'
import NudgeCard from '../pages/chat/NudgeCard'
import NoticeCard from '../pages/chat/NoticeCard'
import SectionMarkerRow from '../pages/chat/SectionMarkerRow'
import { ErrorCard } from '../pages/chat/ErrorCard'
import StopEventCard from '../pages/chat/StopEventCard'
import { isSubagentCompletionMessage } from '../pages/chat/subagentCompletion'
import { REASONING_ROLES } from '../pages/chat/groupDisplayItems'
import MarkdownRenderer from '../components/MarkdownRenderer'
import MessageErrorBoundary from '../components/MessageErrorBoundary'
import PastedChip from '../components/PastedChip'
import { type PasteBlock, findTokenRanges, recollapsePastes } from '../utils/pasteTokens'
import type { ChatMessage } from '../types'
import { fmtMessageTime, fmtMessageTimeFull } from '../pages/chat/messageTime'
import { turnHadPolicyBlock } from './turnPolicyBlock'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { isRejectedDecision } from '../utils/approvalDecision'

/** Everything a renderer may read. Passed per row so entries stay pure functions. */
export interface MessageRenderContext {
  /** Index of this message in `messages`. Needed by rows that look ahead. */
  index: number
  /** The whole transcript. The assistant footer rule depends on what follows. */
  messages: ChatMessage[]
  /** Whether the session is currently producing output. */
  running: boolean
  /** Stable React key the list computed for this row. */
  key: string
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  /** Drop mcp_oauth banners a Connections card already owns. */
  hideCardOwnedOAuth: boolean
  /** tool_call_ids whose call a policy or hook blocked. */
  autoDeniedIds: Set<string>
  /** Host-injected tool row, kept as a shorthand for replacing the tool entries. */
  renderTool?: (message: ChatMessage) => React.ReactNode
  /** Bubble layout used by conversational rows. `isUser` right-aligns. */
  wrapper: (children: React.ReactNode, isUser?: boolean) => React.ReactNode
  /** Full-width row layout used by cards, pills and banners. */
  row: (children: React.ReactNode, tight?: boolean) => React.ReactNode
}

export interface MessageRenderer {
  /** Stable identity. A host entry with the same id replaces the default. */
  id: string
  /** Roles this entry claims. `'*'` considers every role, gated by `match`. */
  roles: readonly string[]
  /** Extra guard, for the roles whose rendering depends on message content. */
  match?: (m: ChatMessage) => boolean
  /** Returning null draws nothing. That an ENTRY EXISTS is what separates a
   *  deliberately undrawn role from one no renderer claims. */
  render: (m: ChatMessage, ctx: MessageRenderContext) => React.ReactNode
}

function renderUserContent(content: string, meta: Record<string, unknown> | undefined): React.ReactNode {
  // History load re-serves the fully-EXPANDED paste content alongside
  // meta.pastes. Handing a large paste (hundreds of KB / tens of thousands of
  // lines) straight to MarkdownRenderer parses + lays it out on the main thread
  // and freezes the tab. Re-collapse the message's own blocks back to
  // `[ Paste #N ]` chips so only the small token text is rendered.
  const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
  if (pastes.length) {
    let text = content
    let ranges = findTokenRanges(text, pastes)
    if (!ranges.length) {
      const collapsed = recollapsePastes(content, pastes)
      if (collapsed !== content) { text = collapsed; ranges = findTokenRanges(text, pastes) }
    }
    if (ranges.length) {
      const out: React.ReactNode[] = []
      let last = 0
      ranges.forEach((r, i) => {
        const trimStart = text[r.start - 1] === '\n' ? r.start - 1 : r.start
        const trimEnd = text[r.end] === '\n' ? r.end + 1 : r.end
        if (trimStart > last) {
          const seg = text.slice(last, trimStart)
          if (seg) out.push(<span key={`t${i}`} style={{ whiteSpace: 'pre-wrap' }}>{seg}</span>)
        }
        out.push(<PastedChip key={`p${i}-${r.block.id}`} block={r.block} />)
        last = trimEnd
      })
      if (last < text.length) {
        const seg = text.slice(last)
        if (seg) out.push(<span key="tend" style={{ whiteSpace: 'pre-wrap' }}>{seg}</span>)
      }
      return <MessageErrorBoundary rawContent={text}>{out}</MessageErrorBoundary>
    }
  }
  return <MessageErrorBoundary rawContent={content}><MarkdownRenderer content={content} /></MessageErrorBoundary>
}

/**
 * Delegates to the shared footer formatter so an embedded app's transcript reads
 * IDENTICALLY to the main chat's. `fmtMessageTime` elides the year only when it
 * is safe, so a message from a previous year is never dated to the current one.
 */
function formatTs(ts?: string): string | undefined {
  if (!ts) return undefined
  return fmtMessageTime(ts) || undefined
}

/** Prop-driven tool row. The store-connected variant is a host entry. */
export const ToolCallPill = memo(function ToolCallPill({ message, running, onFileOpen, autoDenied }: { message: ChatMessage; running: boolean; onFileOpen?: (path: string) => void; autoDenied?: boolean }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [expanded, setExpanded] = React.useState(false)
  const isDone = message.role === 'tool_result'
  const isRejected = isRejectedDecision(message.meta?.resolved)
  const hasPendingPerm = message.role === 'permission' && !message.meta?.resolved

  // Prefer the backend-stamped purpose ("Add teams_data dict guard…") over the
  // raw command, matching the main chat. The raw label is the fallback, and is
  // not hard-truncated — CSS truncation keeps one line without destroying the
  // text for the expanded panel or the file probe.
  const rawLabel = (message.content || '').replace(/^🔧\s*/, '').split('\n')[0]
  const purpose = typeof message.meta?.purpose === 'string' ? message.meta.purpose : ''
  const label = purpose || rawLabel || message.role

  // Status icon + colour mirror the store-connected tool row so an embedded
  // transcript reads with the same visual grammar as a main session: spinner
  // while running, green dot when done, amber alert for auto-denied (a policy or
  // hook block, detected by the HOST from the hidden 🚫 sibling message and
  // passed in, since this pill only ever renders the visible 🔧 message), red
  // slash when user-rejected, amber lock when awaiting approval.
  const isAutoDenied = !isRejected && !!autoDenied
  // Auto-denied is TERMINAL even though the 🔧 message never becomes a
  // tool_result (isDone) — the gate blocked the call, nothing further runs —
  // so it must escape both the loader icon and the spin animation.
  const Icon = isRejected ? CircleSlash : isAutoDenied ? CircleAlert : isDone ? CircleDot : hasPendingPerm ? Lock : LoaderCircle
  const tone = isRejected
    ? 'text-danger bg-danger-subtle'
    : isAutoDenied
      ? 'text-warn bg-warn-subtle'
      : isDone
        ? 'text-ok bg-ok/5'
        : hasPendingPerm
          ? 'text-warn bg-warn-subtle'
          : 'text-accent bg-accent/5'
  // Animate ONLY while the session is actually running, so a tool call left
  // un-terminated by a dropped turn does not spin forever and make an idle
  // transcript look busy — the loading state reflects the session, not the role.
  const iconClass = !isDone && !hasPendingPerm && !isRejected && !isAutoDenied && running ? 'animate-spin' : ''

  // File affordance: same pure helpers the main chat uses (no store needed).
  const filePath = React.useMemo(() => {
    const src = typeof message.meta?.input_preview === 'string' ? message.meta.input_preview : rawLabel
    const p = extractToolFilePath(src)
    return p && isSafePath(p) ? p : null
  }, [message.meta?.input_preview, rawLabel])

  return (
    <div className="animate-scale-in flex items-center gap-2 flex-wrap">
      <button
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
        className={`inline-flex items-center gap-2 text-[13px] leading-5 font-mono px-2 py-0.5 rounded-md cursor-pointer transition-all max-w-[min(600px,90%)] hover:brightness-110 ${tone}`}
      >
        <Icon size={12} className={iconClass} />
        <span className="truncate">{label}</span>
      </button>
      {filePath && onFileOpen && (
        <button
          onClick={() => onFileOpen(filePath)}
          title={i18nT('appSdk.chatMessageList.open_path', { path: filePath })}
          aria-label={i18nT('appSdk.chatMessageList.open_path', { path: filePath })}
          className="inline-flex items-center gap-1 text-[12px] leading-5 font-mono px-1.5 py-0.5 rounded-md border border-border text-muted cursor-pointer hover:text-text hover:border-border-strong transition-all"
        >
          {filePath.split('/').pop()}
          <PanelRight size={11} />
        </button>
      )}
      {expanded && message.content && (
        <pre className="w-full text-[11px] leading-4 font-mono text-muted bg-bg-elevated rounded-md p-2 mt-1 ml-4 max-h-40 overflow-auto whitespace-pre-wrap break-all border border-border">
          {purpose && rawLabel && purpose !== rawLabel ? rawLabel + '\n\n' + message.content : message.content}
        </pre>
      )}
    </div>
  )
})

function toolRow(m: ChatMessage, ctx: MessageRenderContext, autoDenied?: boolean): React.ReactNode {
  return ctx.row(
    ctx.renderTool
      ? ctx.renderTool(m)
      : <ToolCallPill message={m} running={ctx.running} onFileOpen={ctx.onFileOpen} autoDenied={autoDenied} />,
    true,
  )
}

/**
 * The built-in registry, in resolution order. A stop event and a sub-agent
 * completion are recognised by shape rather than by role, so they claim `'*'`
 * and gate on `match`; they come first for that reason.
 */
export const defaultMessageRenderers: readonly MessageRenderer[] = [
  {
    id: 'stop_event',
    roles: ['*'],
    match: m => m.kind === 'stop_event' || m.meta?.kind === 'stop_event',
    // The shared StopEventCard, which reads `meta.state` and draws the stop's
    // actual outcome. It deliberately ignores `content`: a stop row's content is
    // the card's own JSON envelope, mirrored there by the gateway for consumers
    // that read only `content`
    // (`{"kind":"stop_event","id":…,"state":"stopping","outcome":null,…}`), so
    // the hand-rolled row this replaced printed that envelope into the
    // transcript verbatim. The label is the only human-readable rendering there
    // has ever been.
    render: (m, ctx) => ctx.row(<StopEventCard message={m} />),
  },
  {
    id: 'subagent_completion',
    roles: ['*'],
    match: isSubagentCompletionMessage,
    render: (m, ctx) => ctx.row(
      <SubagentCompletionCard
        key={ctx.key}
        message={m}
        onFileOpen={ctx.onFileOpen}
        disclosureKey={ctx.key}
      />,
      true,
    ),
  },
  {
    id: 'user',
    roles: ['user'],
    render: (m, ctx) => ctx.wrapper(
      <UserMessage
        content={m.content}
        meta={m.meta}
        timestamp={formatTs(m.ts)}
        timestampTitle={fmtMessageTimeFull(m.ts)}
        renderContent={renderUserContent}
      />,
      true,
    ),
  },
  {
    id: 'assistant',
    roles: ['assistant', 'streaming'],
    render: (m, ctx) => {
      // A quiet monitor-loop cycle replies with a bare zero-width space
      // (U+200B): invisible-only content would draw as an empty bubble.
      // Same skip as ChatPage's inline chain — see utils/invisibleText.
      if (isHiddenInvisibleAssistantRow(m)) return null
      const isStreaming = m.role === 'streaming'
      // The footer belongs to a FINISHED reply. It shows once the turn is over,
      // which is either because another user or assistant row follows, or
      // because nothing follows and the session has gone idle.
      let showFooter = false
      if (!isStreaming) {
        let nextRelevant = false
        for (let j = ctx.index + 1; j < ctx.messages.length; j++) {
          if (ctx.messages[j].role === 'user') { showFooter = true; nextRelevant = true; break }
          // A hidden invisible-only row draws nothing, so it cannot host the
          // footer; pass over it to the row that renders.
          if (isHiddenInvisibleAssistantRow(ctx.messages[j])) continue
          if (ctx.messages[j].role === 'assistant' || ctx.messages[j].role === 'streaming') { nextRelevant = true; break }
        }
        if (!nextRelevant) showFooter = !ctx.running
      }
      return ctx.wrapper(
        <div className="flex flex-col gap-0">
          <AssistantMessage
            content={m.content}
            isStreaming={isStreaming}
            timestamp={formatTs(m.ts)}
            timestampTitle={fmtMessageTimeFull(m.ts)}
            showFooter={showFooter}
            slotRunning={ctx.running}
            onFileOpen={ctx.onFileOpen}
            variants={m.variants}
            variantIdx={m.variant_idx}
            turnStats={(m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined}
            fileChanges={(m.meta as Record<string, unknown> | undefined)?.file_changes as FileChangeEntry[] | undefined}
            suppressSteerAck={turnHadPolicyBlock(ctx.messages, ctx.index)}
          />
        </div>,
      )
    },
  },
  {
    id: 'tool',
    roles: ['tool'],
    // A tool role also carries the hidden 🚫 deny sibling, which is read for the
    // auto-denied flag and never drawn.
    match: m => !!m.content?.startsWith('🔧'),
    render: (m, ctx) => {
      const tcid = m.meta?.tool_call_id as string | undefined
      return toolRow(m, ctx, !!tcid && ctx.autoDeniedIds.has(tcid))
    },
  },
  {
    id: 'tool_lifecycle',
    roles: ['tool_call', 'tool_result'],
    render: (m, ctx) => toolRow(m, ctx),
  },
  {
    id: 'inject',
    roles: ['inject'],
    render: (m, ctx) => {
      const cronLabel = (m.meta?.cronLabel as string) || ''
      const stripped = cronLabel
        ? m.content.replace(/^\[Cron notification from ".*"\]\n/, '').replace(/\n\[End of cron notification\]$/, '')
        : m.content
      // A note's marker is consumed into the pill row, so rendering it too would show the
      // same choices twice. Non-note inject rows keep it: there it is prose, not syntax.
      const cleanContent = isNoteRow(m) ? parseOptions(stripped).text : stripped
      return ctx.wrapper(
        <>
          {cronLabel && <span className="text-muted text-[11px] leading-4 font-medium px-1 mb-1"><Clock size={11} className="inline mr-0.5" />{cronLabel}</span>}
          <div className="msg-content px-4 py-3 text-sm leading-6 rounded-lg bg-warn-subtle text-text ring-1 ring-inset forced-colors:border ring-warn/30 rounded-bl-[4px] overflow-hidden min-w-0" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
            <MessageErrorBoundary rawContent={cleanContent}><MarkdownRenderer content={cleanContent} softBreaks /></MessageErrorBoundary>
          </div>
        </>,
      )
    },
  },
  {
    id: 'error',
    roles: ['error'],
    // The shared ErrorCard, deliberately without `onContinue`: omitting the
    // handler selects its settled (non-continuable) shape, and the app-sdk
    // surface has no turn to resume, so it must never grow the affordance.
    render: (m, ctx) => ctx.row(<ErrorCard content={m.content} />),
  },
  {
    id: 'notice',
    roles: ['notice'],
    render: (m, ctx) => ctx.row(<NoticeCard content={m.content} />),
  },
  {
    // Grouped and lifecycle-only roles have no row of their own: a thinking or
    // permission message is displayed by the group's own summary UI, and
    // system/done/queued carry state rather than something to read here.
    id: 'undrawn',
    // Reasoning roles derive from the shared classification (see
    // pages/chat/groupDisplayItems.ts) so this default cannot drift from the
    // surfaces that DO draw them; the lifecycle roles are local to this entry.
    roles: [...REASONING_ROLES, 'system', 'done', 'queued'],
    render: () => null,
  },
  {
    // TODO: file download links
    id: 'file',
    roles: ['file'],
    render: () => null,
  },
  {
    id: 'mcp_oauth',
    roles: ['mcp_oauth'],
    render: (m, ctx) => {
      const banner = renderMcpOAuthMessage(m, ctx.hideCardOwnedOAuth)
      if (!banner) return null
      return ctx.row(banner)
    },
  },
  {
    // Auto-nudge cycle marker. `onOpenLoop` (jump to the loop popover) is
    // ChatPage chrome and is deliberately absent here: the card renders its
    // full content without it, only the affordance is page-specific.
    id: 'nudge',
    roles: ['nudge'],
    render: (m, ctx) => ctx.row(<NudgeCard message={m} disclosureKey={ctx.key} />),
  },
  {
    // In the DEFAULT registry, not a host override: an unclaimed role resolves
    // to undefined here and draws NOTHING, so a host entry would miss surfaces.
    id: 'section_marker',
    roles: ['section_marker'],
    render: (m, ctx) =>
      ctx.row(
        <SectionMarkerRow
          label={m.meta?.label as string | undefined}
          fallback={m.content}
        />,
        true,
      ),
  },
]

/**
 * First entry that claims the role and passes its guard. Host entries are
 * searched before the defaults, so replacing a row is a matter of reusing its
 * id — or claiming a role the defaults leave undrawn.
 */
export function resolveRenderer(
  m: ChatMessage,
  renderers: readonly MessageRenderer[],
): MessageRenderer | undefined {
  return renderers.find(r =>
    (r.roles.includes('*') || r.roles.includes(m.role)) && (!r.match || r.match(m)),
  )
}

/**
 * Roles assembled into a collapsible group BEFORE per-row resolution, so the
 * transcript shows "worked through N steps" instead of a wall of rows.
 *
 * Frozen, and an array rather than a Set, because this crosses into apps through
 * the vendored SDK surface: a `ReadonlySet` is only a compile-time promise, and an
 * app is plain JavaScript that never sees our types — one `delete('permission')`
 * on a shared Set would stop the host grouping permissions and take the pending
 * approval UI with it. Two entries, so `includes` costs nothing.
 *
 * Consequence worth knowing when you register an entry: an entry claiming one of
 * these roles is still consulted, but its row renders INSIDE the group, and the
 * group keeps its own summary and approval affordance. Replacing the group itself
 * is not an extension point today — see the limitation note in
 * docs/app-kit/api-reference.md.
 */
export const GROUPED_ROLES: readonly string[] = Object.freeze([...REASONING_ROLES, 'permission'])

/**
 * Host entries sit between the SHAPE-matched defaults and the role-keyed ones.
 *
 * A shape-matched entry recognises a message by what it IS (`kind`), not by the
 * role carrying it — a stop event travels as role `system`, so a host claiming
 * `system` would otherwise swallow the stop card and Stop would draw the host's
 * row instead. A role claim cannot know about kind, so it must not outrank a
 * kind check. Overriding a shape-matched row stays possible, and stays explicit:
 * reuse its id.
 */
export function mergeRenderers(
  extra: readonly MessageRenderer[] | undefined,
): readonly MessageRenderer[] {
  if (!extra?.length) return defaultMessageRenderers
  const overridden = new Set(extra.map(r => r.id))
  const kept = defaultMessageRenderers.filter(r => !overridden.has(r.id))
  const shapeMatched = kept.filter(r => r.roles.includes('*'))
  const roleKeyed = kept.filter(r => !r.roles.includes('*'))
  return [...shapeMatched, ...extra, ...roleKeyed]
}
