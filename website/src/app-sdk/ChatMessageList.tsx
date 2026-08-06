/**
 * ChatMessageList — shared message rendering for ChatPage and ChatEmbed.
 *
 * Renders messages with the same turn grouping, collapsible tool groups,
 * and component hierarchy as ChatPage. No Redux, no React Router.
 *
 * ChatPage wraps this in Virtuoso for virtualized scrolling.
 * ChatEmbed wraps this in a simple scrollable div.
 */
import React, { useMemo, useCallback, memo } from 'react'
import { Clock, LoaderCircle, CircleSlash, CircleAlert, CircleDot, Lock, PanelRight } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { extractToolFilePath } from '../utils/toolFilePath'
import { isSafePath } from '../utils/safePath'
import AssistantMessage, { type TurnStats } from '../pages/chat/AssistantMessage'
import UserMessage from '../pages/chat/UserMessage'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import TurnBlock from '../pages/chat/TurnBlock'
import { renderMcpOAuthMessage } from '../pages/chat/McpOAuthBanner'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'
import { isSubagentCompletionMessage } from '../pages/chat/subagentCompletion'
import MarkdownRenderer from '../components/MarkdownRenderer'
import MessageErrorBoundary from '../components/MessageErrorBoundary'
import PastedChip from '../components/PastedChip'
import { type PasteBlock, findTokenRanges, recollapsePastes } from '../utils/pasteTokens'
import type { ChatMessage } from '../types'
import type { TurnItem, DisplayItem } from '../pages/chat/types'
import { fmtMessageTime, fmtMessageTimeFull } from '../pages/chat/messageTime'

// ── Types ──

export interface ChatMessageListProps {
  messages: ChatMessage[]
  running: boolean
  contentWidth?: string
  onApprove?: (approvalId: string, decision: string) => void
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  /** Optional host-injected renderer for tool messages (role 'tool'/'tool_call'/
   *  'tool_result'). Lets a Redux-connected host (e.g. the dashboard's split-view
   *  ChatPane) render the full slot-aware ToolCallLine while this component stays
   *  dependency-free for the embed SDK. When omitted, the bare ToolCallPill is used. */
  renderTool?: (message: ChatMessage) => React.ReactNode
}

// ── Stable helpers (outside component) ──

function renderUserContent(content: string, meta: Record<string, unknown> | undefined): React.ReactNode {
  // History load re-serves the fully-EXPANDED paste content alongside
  // meta.pastes. Handing a large paste (hundreds of KB / tens of thousands of
  // lines) straight to MarkdownRenderer parses + lays it out on the main thread
  // and freezes the tab. Re-collapse the message's own blocks back to
  // `[ Paste #N ]` chips so only the small token text is rendered. Mirrors
  // ChatPage.renderUserContentInner; kept minimal here to stay Redux-free.
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

const GROUPABLE = new Set(['thinking', 'permission'])

/**
 * Delegates to the shared footer formatter so an embedded app's transcript reads
 * IDENTICALLY to the main chat's. This was a second, hardcoded copy that never
 * printed a year at all — so an app showing a message from a previous year dated
 * it to the current one. `fmtMessageTime` elides the year only when it is safe.
 */
function formatTs(ts?: string): string | undefined {
  if (!ts) return undefined
  return fmtMessageTime(ts) || undefined
}

function msgKey(m: ChatMessage, i: number): string {
  return (m.ts || '') + '-' + i + '-' + m.role
}

// ── ToolCallPill (prop-driven, no Redux) ──

const ToolCallPill = memo(function ToolCallPill({ message, running, onFileOpen, autoDenied }: { message: ChatMessage; running: boolean; onFileOpen?: (path: string) => void; autoDenied?: boolean }) {
  const [expanded, setExpanded] = React.useState(false)
  const isDone = message.role === 'tool_result'
  const isRejected = message.meta?.resolved === 'rejected'
  const hasPendingPerm = message.role === 'permission' && !message.meta?.resolved

  // Prefer the backend-stamped purpose ("Add teams_data dict guard…") over the
  // raw command, matching the main chat. The raw label is the fallback, and is
  // no longer hard-truncated to 80 chars — CSS truncation keeps one line without
  // destroying the text for the expanded panel or the file probe.
  const rawLabel = (message.content || '').replace(/^🔧\s*/, '').split('\n')[0]
  const purpose = typeof message.meta?.purpose === 'string' ? message.meta.purpose : ''
  const label = purpose || rawLabel || message.role

  // Status icon + colour mirror ToolCallLine so an embedded transcript reads
  // with the same visual grammar as a main session: spinner while running,
  // green dot when done, amber alert for auto-denied (policy/hook block —
  // detected by the HOST from the hidden 🚫 sibling message and passed in,
  // since this pill only ever renders the visible 🔧 message), red slash when
  // user-rejected, amber lock when awaiting approval. Previously EVERY state
  // showed one accent-purple spinning wrench, so a finished call was
  // indistinguishable from an in-flight one.
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
  // Animate ONLY while the session is actually running. A tool call left
  // un-terminated by a dropped turn used to spin forever, so an idle transcript
  // still looked busy — the loading state has to reflect the session, not just
  // the message role.
  const iconClass = !isDone && !hasPendingPerm && !isRejected && !isAutoDenied && running ? 'animate-spin' : ''

  // File affordance: same pure helpers the main chat uses (no store needed).
  const filePath = React.useMemo(() => {
    const src = typeof message.meta?.input_preview === 'string' ? message.meta.input_preview : rawLabel
    const p = extractToolFilePath(src)
    return p && isSafePath(p) ? p : null
  }, [message.meta?.input_preview, rawLabel])

  return (
    <div className="animate-scale-in flex items-center gap-1.5 flex-wrap">
      <button
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
        className={`inline-flex items-center gap-1 text-[13px] font-mono px-2 py-0.5 rounded-md cursor-pointer transition-all max-w-[min(600px,90%)] hover:brightness-110 ${tone}`}
      >
        <Icon size={12} className={iconClass} />
        <span className="truncate">{label}</span>
      </button>
      {filePath && onFileOpen && (
        <button
          onClick={() => onFileOpen(filePath)}
          title={i18nT('appSdk.chatMessageList.open_path', { path: filePath })}
          aria-label={i18nT('appSdk.chatMessageList.open_path', { path: filePath })}
          className="inline-flex items-center gap-1 text-[12px] font-mono px-1.5 py-0.5 rounded-md border border-border text-muted cursor-pointer hover:text-text hover:border-border-strong transition-all"
        >
          {filePath.split('/').pop()}
          <PanelRight size={11} />
        </button>
      )}
      {expanded && message.content && (
        <pre className="w-full text-[11px] font-mono text-muted bg-bg-elevated rounded-md p-2 mt-1 ml-4 max-h-40 overflow-auto whitespace-pre-wrap break-all border border-border">
          {purpose && rawLabel && purpose !== rawLabel ? rawLabel + '\n\n' + message.content : message.content}
        </pre>
      )}
    </div>
  )
})

// ── Main component ──

const ChatMessageList = memo(function ChatMessageList({
  messages,
  running,
  contentWidth = '900px',
  onApprove,
  onFileOpen,
  renderTool,
}: ChatMessageListProps) {

  // Phase 1: Build raw items — skip permissions, group thinking
  const displayItems = useMemo<DisplayItem[]>(() => {
    const raw: TurnItem[] = []
    let group: ChatMessage[] = []
    let groupStart = 0

    for (let i = 0; i < messages.length; i++) {
      // A sub-agent completion the card cannot parse stays internal — the model
      // sees it, the reader does not.
      if (messages[i].role === 'subagent' && !isSubagentCompletionMessage(messages[i])) continue
      if (GROUPABLE.has(messages[i].role)) {
        if (!group.length) groupStart = i
        group.push(messages[i])
      } else {
        if (group.length) { raw.push({ kind: 'group', msgs: group, startIdx: groupStart }); group = [] }
        raw.push({ kind: 'single', msg: messages[i], idx: i })
      }
    }
    if (group.length) raw.push({ kind: 'group', msgs: group, startIdx: groupStart })

    // Phase 2: Group into turns (user message = boundary)
    const turns: DisplayItem[] = []
    let turnItems: TurnItem[] = []

    const hasWorkingSteps = (items: TurnItem[]) =>
      items.some(t =>
        (t.kind === 'single' && (t.msg.role === 'tool' || t.msg.role === 'assistant' || t.msg.role === 'streaming')) ||
        t.kind === 'group'
      )

    const flushTurn = (complete: boolean) => {
      if (!turnItems.length) return
      if (hasWorkingSteps(turnItems) && turnItems.length > 2) {
        turns.push({ kind: 'turn', items: turnItems, complete })
      } else {
        turns.push(...turnItems)
      }
      turnItems = []
    }

    for (const item of raw) {
      // A sub-agent completion is the next turn's input, so it opens a turn the
      // same way a user message does — the agent's reply belongs below the card.
      if (item.kind === 'single' && (item.msg.role === 'user' || item.msg.role === 'subagent')) {
        flushTurn(true)
        turns.push(item)
      } else {
        turnItems.push(item)
      }
    }
    flushTurn(!running)

    return turns
  }, [messages, running])

  // tool_call_ids whose call was blocked by a security-policy deny rule or
  // hook. The gateway appends a hidden "🚫 …" tool message sharing the visible
  // 🔧 pill's tool_call_id; the pill itself never sees it (only 🔧 messages
  // render), so the host computes the set once and passes a flag down. A
  // user-rejected call also has a 🚫 sibling but carries meta.resolved =
  // 'rejected' on its permission/pill state, which the pill checks first.
  const autoDeniedIds = useMemo(() => {
    const ids = new Set<string>()
    for (const m of messages) {
      const tcid = m.meta?.tool_call_id as string | undefined
      if (m.role === 'tool' && tcid && m.content?.startsWith('🚫')) ids.add(tcid)
    }
    return ids
  }, [messages])

  // Render a single message by role
  const renderMessage = useCallback((m: ChatMessage, i: number) => {
    const key = msgKey(m, i)
    const wrapper = (children: React.ReactNode, isUser = false) => (
      <div key={key} className="px-5 mx-auto w-full py-1" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
        <div className={`group flex flex-col min-w-0 ${isUser ? 'items-end' : ''}`}>
          <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden ${isUser ? 'items-end' : ''}`}>
            {children}
          </div>
        </div>
      </div>
    )

    if (m.kind === 'stop_event' || m.meta?.kind === 'stop_event') {
      return (
        <div key={key} className="px-5 mx-auto w-full py-1" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          <div className="text-danger text-[13px] font-mono px-3 py-2 rounded-md bg-danger-subtle inline-flex items-center gap-1.5">
            {m.content}
          </div>
        </div>
      )
    }

    if (isSubagentCompletionMessage(m)) {
      return (
        <SubagentCompletionCard
          key={key}
          message={m}
          onFileOpen={onFileOpen}
          disclosureKey={key}
        />
      )
    }

    if (m.role === 'user') {
      return wrapper(
        <UserMessage content={m.content} meta={m.meta} timestamp={formatTs(m.ts)} timestampTitle={fmtMessageTimeFull(m.ts)} renderContent={renderUserContent} />,
        true
      )
    }

    if (m.role === 'assistant' || m.role === 'streaming') {
      const isStreaming = m.role === 'streaming'
      let showFooter = false
      if (!isStreaming) {
        let nextRelevant = false
        for (let j = i + 1; j < messages.length; j++) {
          if (messages[j].role === 'user') { showFooter = true; nextRelevant = true; break }
          if (messages[j].role === 'assistant' || messages[j].role === 'streaming') { nextRelevant = true; break }
        }
        if (!nextRelevant) showFooter = !running
      }
      return wrapper(
        <div className="flex flex-col gap-0">
          <AssistantMessage
            content={m.content}
            isStreaming={isStreaming}
            timestamp={formatTs(m.ts)}
            timestampTitle={fmtMessageTimeFull(m.ts)}
            showFooter={showFooter}
            slotRunning={running}
            onFileOpen={onFileOpen}
            variants={m.variants}
            variantIdx={m.variant_idx}
            turnStats={(m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined}
          />
        </div>
      )
    }

    if (m.role === 'tool' && m.content?.startsWith('🔧')) {
      const tcid = m.meta?.tool_call_id as string | undefined
      return (
        <div key={key} className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          {renderTool ? renderTool(m) : <ToolCallPill message={m} running={running} onFileOpen={onFileOpen} autoDenied={!!tcid && autoDeniedIds.has(tcid)} />}
        </div>
      )
    }

    if (m.role === 'tool_call' || m.role === 'tool_result') {
      return (
        <div key={key} className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          {renderTool ? renderTool(m) : <ToolCallPill message={m} running={running} onFileOpen={onFileOpen} />}
        </div>
      )
    }

    if (m.role === 'inject') {
      const cronLabel = (m.meta?.cronLabel as string) || ''
      const cleanContent = cronLabel
        ? m.content.replace(/^\[Cron notification from ".*"\]\n/, '').replace(/\n\[End of cron notification\]$/, '')
        : m.content
      return wrapper(
        <>
          {cronLabel && <span className="text-muted text-[11px] font-medium px-1 mb-0.5"><Clock size={11} className="inline mr-0.5" />{cronLabel}</span>}
          <div className="msg-content px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap rounded-lg bg-warning-subtle text-fg border border-warning/30 rounded-bl-[4px] overflow-hidden min-w-0" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
            <MessageErrorBoundary rawContent={cleanContent}><MarkdownRenderer content={cleanContent} /></MessageErrorBoundary>
          </div>
        </>
      )
    }

    if (m.role === 'error') {
      return (
        <div key={key} className="px-5 mx-auto w-full py-1" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          <div className="bg-danger-subtle text-danger text-[13px] px-3 py-2 rounded-md border border-danger/15 self-center animate-scale-in">
            {m.content}
          </div>
        </div>
      )
    }

    if (m.role === 'notice') {
      return (
        <div key={key} className="px-5 mx-auto w-full py-1" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          <div className="bg-card text-muted text-[13px] px-3 py-2 rounded-md border border-border self-center animate-scale-in">
            {m.content}
          </div>
        </div>
      )
    }

    if (m.role === 'thinking' || m.role === 'system' || m.role === 'done' || m.role === 'queued') return null
    if (m.role === 'file') return null // TODO: file download links

    if (m.role === 'mcp_oauth') {
      const banner = renderMcpOAuthMessage(m)
      if (!banner) return null
      return (
        <div key={key} className="px-5 mx-auto w-full py-1" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          {banner}
        </div>
      )
    }

    return null
  }, [messages, running, contentWidth, onFileOpen, renderTool, autoDeniedIds])

  // Render a TurnItem (single or group)
  const renderItem = useCallback((item: TurnItem, _i: number) => {
    if (item.kind === 'single') {
      return renderMessage(item.msg, item.idx)
    }
    // Group of thinking/permission messages
    const nonPerm = item.msgs.filter(m => m.role !== 'permission')
    const perms = item.msgs.filter(m => m.role === 'permission')
    const unresolvedPerms = perms.filter(m => !m.meta?.resolved)
    const lastPerm = unresolvedPerms[unresolvedPerms.length - 1]

    const handleApprove = onApprove && lastPerm?.meta?.approval_id
      ? (decision: string) => onApprove(lastPerm.meta!.approval_id as string, decision)
      : undefined

    return (
      <div key={'grp-' + item.startIdx} className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
        <CollapsibleToolGroup
          count={nonPerm.length}
          autoExpand={running && item.startIdx >= messages.length - 5}
          hasPermission={unresolvedPerms.length > 0}
          isRunning={running}
          permissionMeta={lastPerm?.meta}
          pendingPermCount={unresolvedPerms.length}
          onApprove={handleApprove}
        >
          {/* Grouped messages (thinking, permission) return null from renderMessage
              intentionally — CollapsibleToolGroup handles their display via its
              own summary/expand UI, not via individual message rendering. */}
          {item.msgs.map((m, mi) => renderMessage(m, item.startIdx + mi))}
        </CollapsibleToolGroup>
      </div>
    )
  }, [renderMessage, running, messages.length, contentWidth, onApprove])

  // Render a DisplayItem (single, group, or turn)
  const renderDisplayItem = useCallback((item: DisplayItem, i: number) => {
    if (item.kind === 'turn') {
      return <TurnBlock key={'turn-' + i} turn={item} renderItem={renderItem} />
    }
    return renderItem(item, i)
  }, [renderItem])

  return (
    <>
      {displayItems.map(renderDisplayItem)}
    </>
  )
})

export default ChatMessageList
