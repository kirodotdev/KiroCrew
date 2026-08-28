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
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import TurnBlock from '../pages/chat/TurnBlock'
import { isSubagentCompletionMessage } from '../pages/chat/subagentCompletion'
import {
  type MessageRenderer,
  type MessageRenderContext,
  GROUPED_ROLES,
  mergeRenderers,
  resolveRenderer,
} from './messageRenderers'
import type { ChatMessage } from '../types'
import type { TurnItem, DisplayItem } from '../pages/chat/types'

// ── Types ──

export interface ChatMessageListProps {
  messages: ChatMessage[]
  running: boolean
  contentWidth?: string
  /** Resolve a pending approval. MUST return the request's promise: rejection
   *  reaches the approval row's rollback and the buttons come back. The type
   *  deliberately has no `void` arm so a fire-and-forget handler — the exact
   *  shape behind #5524 — cannot compile against this boundary. */
  onApprove?: (approvalId: string, decision: string) => Promise<unknown>
  /** Resolve EVERY pending approval in a permission group with one decision
   *  (batch multi-select, Req 4.1-4.4). The host receives all pending approval
   *  ids and MUST route each through the SLOT-scoped approve endpoint (the same
   *  path `onApprove` uses when it records trust) — never the bare id-scoped
   *  one-shot resolve, which matches slot futures by bare id with no session
   *  check. Like `onApprove`, MUST return the settle promise so the row's
   *  rollback restores the buttons; it settles per id and surfaces any excluded
   *  call rather than aborting the whole batch. Wired only by hosts whose
   *  approve path is slot-scoped; left unset elsewhere. */
  onApproveBatch?: (approvalIds: string[], decision: string) => Promise<unknown>
  /** Offer the standing-trust tier on pending-approval rows. FAIL-CLOSED: set it
   *  only when `onApprove` routes to an endpoint that RECORDS standing trust
   *  (the slot approve endpoint carries the decision verbatim). Hosts resolving
   *  through the one-shot `resolveApproval` endpoint must leave it unset — that
   *  path has no trust verb, so a Trust offer there overstates the grant
   *  (#5400, #5434). */
  canTrust?: boolean
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  /** Optional host-injected renderer for tool messages (role 'tool'/'tool_call'/
   *  'tool_result'). Lets a Redux-connected host (e.g. the dashboard's split-view
   *  ChatPane) render the full slot-aware ToolCallLine while this component stays
   *  dependency-free for the embed SDK. When omitted, the bare ToolCallPill is used. */
  renderTool?: (message: ChatMessage) => React.ReactNode
  /** Drop mcp_oauth messages a Connections card owns (`meta.card_owned`). A prop
   *  rather than a config read so this component stays query-free for the embed
   *  SDK; the dashboard host passes its `connections_ui` flag. Default renders
   *  every banner, which is correct for any surface with no cards. */
  hideCardOwnedOAuth?: boolean
  /** Extra renderer entries, searched before the built-ins. An entry reusing a
   *  built-in id replaces it; one claiming an undrawn role adds a row type. */
  renderers?: readonly MessageRenderer[]
}

// ── Stable helpers (outside component) ──

function msgKey(m: ChatMessage, i: number): string {
  return (m.ts || '') + '-' + i + '-' + m.role
}

// ── Main component ──

const ChatMessageList = memo(function ChatMessageList({
  messages,
  running,
  contentWidth = '900px',
  onApprove,
  onApproveBatch,
  canTrust,
  onFileOpen,
  renderTool,
  hideCardOwnedOAuth = false,
  renderers,
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
      if (GROUPED_ROLES.includes(messages[i].role)) {
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

  // Resolve each row through the registry. Host entries are searched first, so
  // the same lookup serves a plain embed and a store-connected dashboard.
  const activeRenderers = useMemo(() => mergeRenderers(renderers), [renderers])

  const renderMessage = useCallback((m: ChatMessage, i: number) => {
    const key = msgKey(m, i)
    const wrapper = (children: React.ReactNode, isUser = false) => (
      <div key={key} className="px-4 mx-auto w-full py-1" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
        <div className={`group flex flex-col min-w-0 ${isUser ? 'items-end' : ''}`}>
          <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${isUser ? 'items-end' : ''}`}>
            {children}
          </div>
        </div>
      </div>
    )
    const row = (children: React.ReactNode, tight = false) => (
      <div key={key} className={`px-4 mx-auto w-full ${tight ? 'py-0' : 'py-1'}`} style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
        {children}
      </div>
    )

    const entry = resolveRenderer(m, activeRenderers)
    if (!entry) return null

    const ctx: MessageRenderContext = {
      index: i,
      messages,
      running,
      key,
      onFileOpen,
      hideCardOwnedOAuth,
      autoDeniedIds,
      renderTool,
      wrapper,
      row,
    }
    return entry.render(m, ctx)
  }, [messages, running, contentWidth, onFileOpen, renderTool, autoDeniedIds, hideCardOwnedOAuth, activeRenderers])


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

    // Batch resolver over EVERY pending id in this group (Req 4.1-4.4). Only
    // offered when the host supplied onApproveBatch AND there is MORE THAN ONE
    // pending approval — a single-id "batch" is never invoked (CollapsibleToolGroup
    // batches only when pendingPermCount > 1) yet a > 0 handler still flips the
    // (onApprove || onApproveBatch) render gates, so the gate matches its one
    // real trigger by requiring > 1 here. TOOL_DENY calls never surface as
    // pending permissions (backend gate; locked by the T5-guard test), so this
    // id list is deny-free.
    const batchIds = unresolvedPerms
      .map(m => m.meta?.approval_id as string | undefined)
      .filter((x): x is string => !!x)
    const handleApproveBatch = onApproveBatch && batchIds.length > 1
      ? (decision: string) => onApproveBatch(batchIds, decision)
      : undefined
    // Every pending call's meta, so the batch row can preview ALL N commands the
    // one click will approve — not just the newest (permissionMeta). The human
    // gate against an untrusted agent must show each command being approved.
    // Map 1:1 over unresolvedPerms (NO filter): a meta-less pending perm becomes
    // an empty {} so CollapsibleToolGroup renders its "No preview available"
    // placeholder row for it. Filtering here would make permissionMetas.length <
    // pendingPermCount, so the "Review all N" note would promise more rows than
    // render — the silent-row gap the placeholder exists to prevent.
    const batchMetas = unresolvedPerms.map(m => m.meta ?? {})

    return (
      <div key={'grp-' + item.startIdx} className="px-4 mx-auto w-full py-0" style={{ maxWidth: `var(--mc-content-width, ${contentWidth})` }}>
        <CollapsibleToolGroup
          count={nonPerm.length}
          autoExpand={(running && item.startIdx >= messages.length - 5) || !!handleApproveBatch}
          hasPermission={unresolvedPerms.length > 0}
          isRunning={running}
          permissionMeta={lastPerm?.meta}
          permissionMetas={batchMetas}
          pendingPermCount={unresolvedPerms.length}
          onApprove={handleApprove}
          onApproveBatch={handleApproveBatch}
          canTrust={canTrust}
        >
          {/* Grouped messages (thinking, permission) return null from renderMessage
              intentionally — CollapsibleToolGroup handles their display via its
              own summary/expand UI, not via individual message rendering. */}
          {item.msgs.map((m, mi) => renderMessage(m, item.startIdx + mi))}
        </CollapsibleToolGroup>
      </div>
    )
  }, [renderMessage, running, messages.length, contentWidth, onApprove, onApproveBatch, canTrust])

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
