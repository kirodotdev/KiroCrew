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
import { Wrench, CheckCircle, XCircle, Clock } from 'lucide-react'
import AssistantMessage from '../pages/chat/AssistantMessage'
import UserMessage from '../pages/chat/UserMessage'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import TurnBlock from '../pages/chat/TurnBlock'
import { renderMcpOAuthMessage } from '../pages/chat/McpOAuthBanner'
import MarkdownRenderer from '../components/MarkdownRenderer'
import MessageErrorBoundary from '../components/MessageErrorBoundary'
import PastedChip from '../components/PastedChip'
import { type PasteBlock, findTokenRanges, recollapsePastes } from '../utils/pasteTokens'
import type { ChatMessage } from '../types'
import type { TurnItem, DisplayItem } from '../pages/chat/types'

// ── Types ──

export interface ChatMessageListProps {
  messages: ChatMessage[]
  running: boolean
  contentWidth?: string
  onApprove?: (approvalId: string, decision: string) => void
  onFileOpen?: (path: string) => void
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

function formatTs(ts?: string): string | undefined {
  if (!ts) return undefined
  return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function msgKey(m: ChatMessage, i: number): string {
  return (m.ts || '') + '-' + i + '-' + m.role
}

// ── ToolCallPill (prop-driven, no Redux) ──

const ToolCallPill = memo(function ToolCallPill({ message, running: _running }: { message: ChatMessage; running: boolean }) {
  const [expanded, setExpanded] = React.useState(false)
  const label = (message.content || '').replace(/^🔧\s*/, '').split('\n')[0].slice(0, 80)
  const isDone = message.role === 'tool_result'
  const isRejected = message.meta?.resolved === 'rejected'

  return (
    <div className="animate-scale-in">
      <button
        onClick={() => setExpanded(e => !e)}
        className={`inline-flex items-center gap-1 text-[13px] font-mono px-2 py-0.5 rounded-md cursor-pointer transition-all max-w-[min(600px,90%)] hover:brightness-110 ${
          isRejected ? 'text-danger bg-danger-subtle' :
          isDone ? 'text-ok bg-ok/5' :
          'text-accent bg-accent/5'
        }`}
      >
        {isRejected ? <XCircle size={12} /> : isDone ? <CheckCircle size={12} /> : <Wrench size={12} className="animate-spin" style={{ animationDuration: '2s' }} />}
        <span className="truncate">{label || message.role}</span>
      </button>
      {expanded && message.content && (
        <pre className="text-[11px] font-mono text-muted bg-bg-elevated rounded-md p-2 mt-1 ml-4 max-h-40 overflow-auto whitespace-pre-wrap break-all border border-border">
          {message.content}
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
      if (item.kind === 'single' && item.msg.role === 'user') {
        flushTurn(true)
        turns.push(item)
      } else {
        turnItems.push(item)
      }
    }
    flushTurn(!running)

    return turns
  }, [messages, running])

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

    if (m.role === 'user') {
      return wrapper(
        <UserMessage content={m.content} meta={m.meta} timestamp={formatTs(m.ts)} renderContent={renderUserContent} />,
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
            showFooter={showFooter}
            slotRunning={running}
            onFileOpen={onFileOpen}
            variants={m.variants}
            variantIdx={m.variant_idx}
          />
        </div>
      )
    }

    if (m.role === 'tool' && m.content?.startsWith('🔧')) {
      return (
        <div key={key} className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          {renderTool ? renderTool(m) : <ToolCallPill message={m} running={running} />}
        </div>
      )
    }

    if (m.role === 'tool_call' || m.role === 'tool_result') {
      return (
        <div key={key} className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
          {renderTool ? renderTool(m) : <ToolCallPill message={m} running={running} />}
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
  }, [messages, running, contentWidth, onFileOpen, renderTool])

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
