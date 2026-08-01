import { useState, useRef, useCallback, useEffect, useLayoutEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query'
import { modelListRefetchInterval } from '../providers/modelListHealth'
import { useIsMobile } from '../hooks/useIsMobile'
import { useRailWidth } from '../hooks/useRailWidth'
import { SETTINGS_DEFAULT_MODEL_ID } from '../hooks/useSettingHighlight'
import { isTouchDevice } from '../utils/isTouchDevice'
import { useSwipeEdge } from '../hooks/useSwipeEdge'
import type { ResizeInfo } from '../utils/resizeImage'
import { useAppSelector, useAppDispatch, store } from '../store'
import { useConnected } from '../hooks/useConnected'
import { useChatPopouts } from '../hooks/useChatPopouts'
import {
  switchSlot, createSlot, deleteSlot, fetchHistory,
  appendMessage, resumeFromHistory, forkSlot,
  setSlotRunning, startLocalTurn, syncSlotRunningFromServer, setPendingInput, resolveByApprovalId, clearPendingPermissions, cancelQueuedMessage, editQueuedMessage,
  selectComposerBusy,
  setVoiceAudio,
  toggleActivity, openActivityPanel, openActivityToTab,
  setActiveSlot, truncateAfterIndex, replaceMessages,
  requestStop, pendingQuestionFor, clearFollowupCard, dismissFollowupItem,
  mcpAppKey,
} from '../store/chatSlice'
import { addNotification, removeNotificationByTs } from '../store/notificationsSlice'
import { onTerminalReady, sendToTerminalSession } from '../utils/terminalRegistry'
import { interceptSlashCommand } from './chat/ChatInput'
import { sseSlotTitle } from '../store/dashboardSlice'
import { api } from '../api/client'
import type { PlanStepInput } from '../api/client'
import { useProvider } from '../providers'
import { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import { fileReadUrl } from '../utils/fileReadUrl'
import { safeSetItem, safeSetSessionItem } from '../utils/safeStorage'
import { handleStopPress, isEscalationState } from '../utils/stopDebounce'
import { EmptyState, Btn, Input } from '../components/ui'
import { type FileChangeEntry } from '../components/FileChangeChips'
import PastedChip from '../components/PastedChip'
import SnipOverlay from '../components/SnipOverlay'
import { captureScreen, screenSnipSupported, currentTabCaptureDeps } from '../hooks/useScreenSnip'
import { useTouchedFiles } from '../hooks/useTouchedFiles'
import { useTheme } from '../hooks/useTheme'
import CollapsibleToolGroup from './chat/CollapsibleToolGroup'
import ThinkingBlock from './chat/ThinkingBlock'
import type { DisplayItem, TurnItem } from './chat/types'
import McpToolsPanel from './chat/McpToolsPanel'
import { deriveLoadedMcpTools } from '../lib/mcpLoadedTools'
import type { McpServer } from '../types'
import { useScrollManager } from './chat/useScrollManager'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { parseFiles, prepareSendPayload, resolveFileSegment, buildFileLabels, findUnreferencedAttachments } from '../utils/fileTokens'
import { type PasteBlock, expandAll as expandPasteTokens, findTokenRanges, pruneBlocks as pruneBlocksUtil, remapCarriedBlocks, saveStoredPaste, recollapsePastes } from '../utils/pasteTokens'
import { extractPromptFromToken, extractSlackContextFromToken } from '../utils/tokenPrompt'
/** Delay (ms) before scrolling to bottom after a state update, giving React time to commit. */
const SCROLL_AFTER_RENDER_MS = 100
// Canonical home is utils/navIntent (shared with the popout nav-intent
// applier); re-exported here for this page's historical importers.
export { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import { PREFILL_STORAGE_KEY, writePrefill } from '../utils/navIntent'
import WelcomeView from '../components/WelcomeView'
import { usePanelTabs, clearInlineDraft, getInlineDraft } from '../hooks/usePanelTabs'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useAgents } from '../hooks/useAgents'
import AgentDropdownList from '../components/AgentDropdownList'
import ProjectPicker from '../components/ProjectPicker'
import InboundLinkChip from '../components/InboundLinkChip'
import SessionActionsMenu from '../components/SessionActionsMenu'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent,
} from '../components/ui/dropdown-menu'
import ModelEffortDropdown from '../components/ModelEffortDropdown'

import ChatInput from '../components/ChatInput'
import SessionGridView from '../components/SessionGridView'
import { anchorForSlot, loadLayout, sessionSlots } from '../hooks/splitLayoutStore'
import { modelSupportsEffort } from '../lib/effort'
import FollowUpCard from '../components/FollowUpCard'
import PendingQuestionCard from '../components/PendingQuestionCard'
import type { FollowupItem } from '../store/chatSlice'

// Stable identity for the "no follow-up cards" case: returning a fresh {} from
// the selector would make it a new reference on every store update.
const EMPTY_FOLLOWUPS: Record<string, { items: FollowupItem[]; ts: number }> = {}
import ReasoningEffortDropdown from '../components/ReasoningEffortDropdown'
import FlyingQuote from '../components/FlyingQuote'
import { useMessageSearch } from '../hooks/useMessageSearch'
import SearchHighlightContext, { MessageSearchScope } from '../hooks/SearchHighlightContext'
import SearchBar from '../components/SearchBar'
import SearchResultsList from '../components/SearchResultsList'
import { pickSearchScrollBehavior, scrollCurrentMatchIntoView, pollRowSettled, glideOnceStep, attachUserScrollIntent } from '../utils/searchScroll'
import QueueStack, { SubagentDeliveryProgress, isSystemDelivery, isNonInteractiveQueued } from '../components/QueueStack'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import { TipCard, useTipTrigger } from '../components/TipCard'
import { useVoiceInput, voiceInputSupported } from '../hooks/useVoiceInput'
import VoiceDisabledModal from '../components/VoiceDisabledModal'
import { ChatFooter, AssistantMessage, UserMessage, PinnedPrompt } from './chat'
import type { TurnStats } from './chat/AssistantMessage'
import MarkdownRenderer from '../components/MarkdownRenderer'
import MessageErrorBoundary from '../components/MessageErrorBoundary'
import TypewriterText from '../components/TypewriterText'
import { useChatNavigation } from '../hooks/useChatNavigation'
import SubagentProgressBar from './chat/SubagentProgressBar'
import TaskProgressBar from './chat/TaskProgressBar'
import SidePanel, { CHAT_PANE_MIN_W, sidePanelFillWidth } from './chat/SidePanel'
import { groupDisplayItems, applyRunningState } from './chat/groupDisplayItems'
import { setSessionPreviewPending, normalizeUrl, PREVIEW_FOCUS_EVENT, PREVIEW_SNIP_EVENT, PREVIEW_ENABLE_BROWSE_EVENT, BROWSE_MODE_EVENT } from '../components/WebPreviewPanel'
import { detectPreviewUrl, previewFeedDecision } from '../utils/detectPreviewUrl'
import { fileLandingSlot } from '../utils/uploadRouting'
import ChatSidebar, { SIDEBAR_MIN, SIDEBAR_MAX } from './ChatSidebar'
import { toSlug } from '../utils/shareUrl'
import { DRAFT_SAVE_DEBOUNCE_MS, loadDrafts, saveDrafts as persistDrafts, setDraft } from '../utils/chatDrafts'
import { loadFileDrafts, saveFileDrafts as persistFileDrafts, setFileDraft } from '../utils/chatFileDrafts'
import { loadPasteDrafts, savePasteDrafts as persistPasteDrafts, setPasteDraft } from '../utils/chatPasteDrafts'
import { findPinnedPromptIdx, findNextPromptIdx, computePinPush, promptPreview } from '../utils/pinnedPrompt'
import {
  loadSeenPullRequestLinks,
  partitionSourceLinks,
  persistSeenPullRequestLinks,
  PullRequestLinkIndex,
  recordNewPullRequestLinks,
} from '../utils/pullRequestLinks'
import { deriveFollowUpOptions } from '../utils/deriveFollowUpOptions'
import OverlayDrawer from '../components/OverlayDrawer'
import { loadChatConfig, CONTENT_WIDTH, type ChatConfig } from './chat/ChatSettings'
import { useKnowledgeFetch, extractKnowledgeQuery, expandKnowledgeBlock } from './chat/useKnowledgeFetch'
import { KnowledgePicker } from './chat/KnowledgePicker'
import { BookOpen, EyeOff, Loader, PanelLeftOpen, PanelLeftClose, Pen, ChevronDown, ChevronRight, Plug, ArrowDown, MessageSquare, MessageSquareDot, Sparkles, VenetianMask, Clock, Undo2, Columns2, ExternalLink, PanelRight, Paperclip } from 'lucide-react'

import InfoTip from '../components/InfoTip'
import { FileCard } from '../components/FileCard'
import SlotTagPopover from '../components/SlotTagPopover'
import { TagPopoverProvider } from '../hooks/useTagPopover'

import { AnimatePresence, motion } from 'framer-motion'
import DetailPanel from '../components/DetailPanel'

import type { ChatMessage, Artifact } from '../types'

import ToolCallLine from './chat/ToolCallLine'
import WorkflowRunCard, { extractWorkflowRunId } from './chat/WorkflowRunCard'
import SubagentRunCard, { extractSpawnRunLaunch } from './chat/SubagentRunCard'
import WorkflowCompletionCard, { isWorkflowCompletionMessage } from './chat/WorkflowCompletionCard'
import { renderMcpOAuthMessage } from './chat/McpOAuthBanner'
import TurnBlock from './chat/TurnBlock'
import Clickable from '../components/Clickable'
import StopEventCard from './chat/StopEventCard'
import NudgeCard, { nudgeMatchesLoop } from './chat/NudgeCard'
import RecoveryCard, { parseRecoveryMessage } from './chat/RecoveryCard'
import WorkflowProgressBar from './chat/WorkflowProgressBar'
import { tryQuickSend } from '../lib/quickSend'
import { rewindWithRollback } from '../lib/rewindCall'


import { i18nT } from '../i18n/t'
/**
 * Human-readable reason from a rejected thunk. `unwrap()` rejects with RTK's
 * SERIALIZED error — a plain object, never an `Error` instance — so an
 * `instanceof Error` test always fails and every user would read the developer
 * fallback. Read `message` structurally instead, with a plain-language fallback.
 */
/** Unique `ts` for a client-side notification that the feed can still PARSE.
 *  `addNotification` dedupes on `ts`, so two entries in the same millisecond would
 *  see the second silently dropped — which for a payload-carrying entry discards
 *  the user's message. The disambiguator goes in FRACTIONAL digits because
 *  `parseTs` only accepts `\d+(\.\d+)?`; a `<ms>-<n>` form falls through to
 *  `new Date(string)`, which is Invalid Date in V8 → "Invalid Date" headers and
 *  "NaNd ago" in the bell feed. */
let notificationTsSeq = 0
const uniqueNotificationTs = (): string => `${Date.now()}.${notificationTsSeq++}`


const createFailReason = (e: unknown): string => {
  const msg = typeof e === 'object' && e !== null ? (e as { message?: unknown }).message : undefined
  return typeof msg === 'string' && msg.trim() ? msg : 'the server did not respond'
}

export function ChatHeaderMenu({ activeSlot, agent, onReveal, onRename, mode }: {
  activeSlot: string | null; agent?: string; onReveal?: () => void; onRename?: () => void; mode?: string
}) {
  // Controlled open state: lets the colour-swatch row (not a Radix menu item)
  // close the menu after a pick, via the onColorPicked hook passed below.
  const [open, setOpen] = useState(false)
  // MCP server list is fetched lazily when its submenu opens (was a hover gate
  // before; now driven by the Radix Sub's open state).
  const [mcpOpen, setMcpOpen] = useState(false)
  const { data: servers = [] } = useQuery<{ name: string; enabled?: boolean }[]>({
    queryKey: ['mcp-servers', agent],
    queryFn: () => api.mcpActive(agent || undefined),
    enabled: mcpOpen,
  })
  // Tool Search mode for this session's MCP tools (shared ['kirocrewConfig']
  // cache). When on, tool specs are deferred (search-and-call), so every server
  // shows as connected but its tools load only when used; when off, every spec
  // is sent each turn. Explains the "why are they all loaded?" question.
  const { data: toolSearchOn = true } = useQuery<{ agent?: { tool_search?: boolean } }, Error, boolean>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (c) => c.agent?.tool_search ?? true,
    enabled: mcpOpen,
  })
  // Per-tool loaded/deferred state is derived client-side (no endpoint): the
  // full server list carries each server's tool names + disabledTools, and the
  // "loaded this session" set comes from scanning this slot's tool_search
  // results in the chat store. See deriveLoadedMcpTools for the caveats.
  const { data: fullServers = [] } = useQuery<McpServer[]>({
    queryKey: ['mcp-servers-full'],
    queryFn: () => api.mcpServers(),
    enabled: mcpOpen,
  })
  const toolsByServer = useMemo(
    () => Object.fromEntries(fullServers.map(s => [s.name, { tools: s.tools, disabledTools: s.disabledTools }])),
    [fullServers],
  )
  const sessionMessages = useAppSelector(s => s.chat.messages)
  const loadedTools = useMemo(() => deriveLoadedMcpTools(sessionMessages), [sessionMessages])

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button className="px-0.5 py-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none transition-all" aria-label={i18nT('pages.chatPage.session_options')}>
          <ChevronDown size={14} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[180px]">
        {activeSlot && (
        <SessionActionsMenu
          variant="dropdown"
          slotKey={activeSlot}
          mode={mode}
          // MCP servers: stateful (lazy fetch gated on the sub's open state), so
          // it stays here as an info slot rather than a generic capability.
          infoSlots={[
            <DropdownMenuSub key="mcp" onOpenChange={setMcpOpen}>
              <DropdownMenuSubTrigger>
                <Plug size={13} className="shrink-0 text-muted" />
                <span className="flex-1">{i18nT('pages.chatPage.mcp_servers')}</span>
                <ChevronRight size={12} className="text-muted" />
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent className="min-w-[240px] max-w-[300px] max-h-[340px] overflow-y-auto px-3 py-2">
                <McpToolsPanel
                  servers={servers}
                  toolsByServer={toolsByServer}
                  loaded={loadedTools}
                  toolSearchOn={toolSearchOn}
                  loading={servers.length === 0}
                />
              </DropdownMenuSubContent>
            </DropdownMenuSub>,
          ]}
          onReveal={onReveal}
          onRename={onRename}
          // The header controls its own menu, so close it after a colour pick.
          onColorPicked={() => setOpen(false)}
        />
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Stable key for a single TurnItem — the leading row of a turn OR a top-level
 *  single/group. A `single` and the `turn` it leads resolve to the SAME key so
 *  a mid-stream regroup (single promoted into a grouped turn once it gains
 *  working steps) does NOT change the row's virtual key → no remount / silent
 *  re-measure. `msgKey` supplies the per-message identity (clientTs → ts →
 *  minted id; never the array index — see stableMsgKey). Groups key on their
 *  first message's start index, which is stable for surviving rows (trailing
 *  truncation removes later groups whole rather than renumbering earlier ones). */
export function turnLeadKey(it: TurnItem, msgKey: (m: ChatMessage) => string): string {
  return it.kind === 'single' ? `row-${msgKey(it.msg)}` : `grp-${it.startIdx}`
}

/** Virtualizer / HeightCache key for a display row. Pure (identity injected)
 *  so the steer-reconcile-stability and regroup-stability guarantees are
 *  unit-testable. A `turn` inherits the key of its leading item so promoting a
 *  single into a turn (and vice-versa) keeps the row identity — and thus its
 *  cached height and DOM node — stable. */
export function virtualKeyFor(
  it: DisplayItem,
  index: number,
  msgKey: (m: ChatMessage) => string,
): string {
  if (it.kind === 'turn') {
    const first = it.items[0]
    if (!first) return `turn-empty-${index}`
    return turnLeadKey(first, msgKey)
  }
  return turnLeadKey(it, msgKey)
}

/** Render user message content with file chips and image markdown. Handles:
 *  - Fresh messages: meta.files present, displayTxt has @relative/path tokens
 *  - Replayed history: no meta.files, content has [attached_file N] /full/path
 *  - Mixed content: images + file attachments in the same message */
function KnowledgeBubbleChip({ knowledge }: { knowledge: { items: number; tokens: number; titles: string[]; content?: { title: string; text: string }[] } }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <span className="block mb-1">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="inline-flex items-center gap-1 text-[11px] text-accent bg-accent/10 rounded px-1.5 py-0.5 border-none cursor-pointer hover:bg-accent/20 transition-colors"
        aria-expanded={expanded}
        aria-label={expanded ? i18nT('pages.chatPage.collapse_knowledge_context') : i18nT('pages.chatPage.expand_knowledge_context')}
      >
        <BookOpen size={12} className="shrink-0" /> {knowledge.items} {i18nT('pages.chatPage.knowledge')} {knowledge.items === 1 ? 'item' : 'items'} · {knowledge.tokens.toLocaleString()} {i18nT('pages.chatPage.tokens')}
      </button>
      {expanded && knowledge.content && (
        <div className="mt-1 max-h-[300px] overflow-auto rounded border border-border bg-bg-elevated p-2 text-[11px]">
          {knowledge.content.map((item, i) => (
            <div key={i} className="mb-2 last:mb-0">
              <div className="font-medium text-text-strong">{item.title}</div>
              <pre className="mt-0.5 whitespace-pre-wrap text-muted font-mono leading-[1.4]" style={{ wordBreak: 'break-word' }}>{item.text}</pre>
            </div>
          ))}
        </div>
      )}
    </span>
  )
}

export function renderUserContent(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void) {
  // Per-message containment (React #290 defense-in-depth): a render crash in a
  // user/inject bubble must degrade to a per-message fallback, not unwind to
  // the root boundary and blank the whole dashboard.
  return (
    <MessageErrorBoundary rawContent={content}>
      {renderUserContentInner(content, meta, onFileOpen)}
    </MessageErrorBoundary>
  )
}

function renderUserContentInner(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void) {
  const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
  const knowledge = meta?.knowledge as { items: number; tokens: number; titles: string[]; content?: { title: string; text: string }[] } | undefined

  const knowledgeBadge = knowledge ? (
    <KnowledgeBubbleChip knowledge={knowledge} />
  ) : null

  if (!pastes.length) return <>{knowledgeBadge}{renderFileSegment(content, meta, onFileOpen, 'seg')}</>

  // History load re-serves the fully-EXPANDED content (what the LLM saw), so a
  // message whose bubble was a `[ Paste #N ]` chip when sent comes back as the
  // raw paste text with no token in it. If mergePreservedPastes couldn't
  // re-collapse it (no optimistic bubble, side-table entry evicted/missing),
  // handing that raw text — potentially hundreds of KB / tens of thousands of
  // lines — to renderFileSegment → MarkdownRenderer parses and lays it out on
  // the main thread and freezes the tab. Re-collapse deterministically from the
  // blocks that travel with the message so the chip is restored regardless of
  // external state. See recollapsePastes / Mesh big-paste freeze.
  let text = content
  let ranges = findTokenRanges(text, pastes)
  if (!ranges.length) {
    const collapsed = recollapsePastes(content, pastes)
    if (collapsed !== content) {
      text = collapsed
      ranges = findTokenRanges(text, pastes)
    }
  }
  if (!ranges.length) return <>{knowledgeBadge}{renderFileSegment(text, meta, onFileOpen, 'seg')}</>

  // Paste chips are inline by nature, so to keep them flowing with the
  // surrounding text (e.g. "hey [chip] thanks"), render each text segment
  // inline — preserves whitespace and doesn't wrap text in a <p> the way
  // MarkdownRenderer does. Trade-off: block-level markdown (lists, code
  // blocks, headings) inside a message that also contains a paste will
  // render as literal text. That's a rare combination for user messages.
  const out: React.ReactNode[] = []
  let lastIdx = 0
  ranges.forEach((r, i) => {
    // Consume one newline on each side of the token so the chip (inline) and
    // its expanded block absorb the line-break that ChatInput.handlePaste
    // forces around the token. Without this, expanding the chip adds an extra
    // visible line (its own block-level display + the still-rendered \n).
    const trimStart = text[r.start - 1] === '\n' ? r.start - 1 : r.start
    const trimEnd = text[r.end] === '\n' ? r.end + 1 : r.end
    if (trimStart > lastIdx) {
      const seg = text.slice(lastIdx, trimStart)
      if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, `t${i}`))
    }
    out.push(<PastedChip key={`p${i}-${r.block.id}`} block={r.block} />)
    lastIdx = trimEnd
  })
  if (lastIdx < text.length) {
    const seg = text.slice(lastIdx)
    if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, 'tend'))
  }

  // Attachments never referenced by any segment (e.g. an upload with no inline
  // token in the caption) belong to the MESSAGE, not any one segment — render
  // them once here as cards so a multi-segment paste message can't duplicate
  // them (see resolveFileSegment: cardPaths is deliberately segment-scoped).
  // findUnreferencedAttachments owns the referenced/unreferenced decision with
  // the SAME original-list token indexing resolveFileSegment uses (single
  // source of truth; token N indexes the original list, not image-filtered).
  const orderedFiles = parseFiles(text, meta)
  const unreferenced = orderedFiles.length ? findUnreferencedAttachments(text, orderedFiles) : []
  if (unreferenced.length) {
    const labels = buildFileLabels(unreferenced)
    out.push(
      <div key="msg-cards" className="flex flex-col gap-1.5 mt-1">
        {unreferenced.map((p, i) => (
          <FileAttachmentCard key={`msg-c${i}`} fullPath={p} label={labels.get(p) || p} onFileOpen={onFileOpen} />
        ))}
      </div>,
    )
  }
  return knowledgeBadge ? <>{knowledgeBadge}{out}</> : out
}

/** Inline-flow renderer for a text segment adjacent to a paste chip.
 *  Handles @-file tokens as inline chips; other text is rendered as a
 *  whitespace-preserving span (no markdown). */
function renderInlineSegment(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, keyBase: string) {
  const parsedFiles = parseFiles(content, meta)
  if (!parsedFiles.length) {
    return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{content}</span>
  }
  // Inline-flow variant (adjacent to a paste chip): keep everything inline.
  // Non-image attachments referenced in the text render as inline chips; any
  // standalone-token upload in this segment also renders as an inline chip
  // appended to it (this path can't host block cards without breaking the
  // inline flow). Never-referenced attachments are handled once at message
  // level. Pass the ORIGINAL ordered list so token indices line up.
  const { display, mentionMap, cardPaths, labels } = resolveFileSegment(content, parsedFiles)
  if (!mentionMap.size && !cardPaths.length) {
    return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{display}</span>
  }

  const keys = [...mentionMap.keys()].slice(0, 20)
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = tokPattern
    ? display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))
    : [display]
  const chipCls = 'inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors'
  return (
    <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        const tok = part.match(/^@(.+)$/)?.[1]
        const fullPath = tok && mentionMap.get(tok)
        if (fullPath) {
          return (
            <Clickable key={`${keyBase}-f${i}`} className={chipCls} title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={`Open file ${fullPath}`}>@{tok}</Clickable>
          )
        }
        return <span key={`${keyBase}-p${i}`}>{part}</span>
      })}
      {cardPaths.map((p, i) => (
        <Clickable key={`${keyBase}-uc${i}`} className={chipCls} title={p} onClick={() => onFileOpen(p)} aria-label={`Open file ${p}`}>@{labels.get(p) || p}</Clickable>
      ))}
    </span>
  )
}

/** Block card for a single user-attached (non-image) file. Clickable to open
 *  the file via the shared onFileOpen callback. Styled after the agent-side
 *  download card (see components/FileCard.tsx) but carries no size/mime — a
 *  user attachment only has a path here. */
function FileAttachmentCard({ fullPath, label, onFileOpen }: { fullPath: string; label: string; onFileOpen: (path: string) => void }) {
  return (
    <Clickable
      className="flex items-center gap-2.5 max-w-full bg-card border border-border rounded-lg px-3 py-2 text-sm no-underline text-text hover:border-accent transition-colors cursor-pointer animate-scale-in"
      title={fullPath}
      onClick={() => onFileOpen(fullPath)}
      aria-label={`Open file ${fullPath}`}
    >
      <Paperclip size={15} className="shrink-0 text-muted" />
      <span className="font-medium truncate">{label}</span>
    </Clickable>
  )
}

/** File-card + markdown rendering for a text segment (no paste tokens inside).
 *
 *  Attachment display is resolved by the shared resolveFileSegment helper
 *  (utils/fileTokens.ts), the single owner of attachment-marker knowledge —
 *  the same helper backs renderInlineSegment, so the two paths never diverge.
 *  It ALWAYS rewrites the LLM-facing `[attached_file N] /path` plumbing to an
 *  `@label` token (so raw tokens never leak as text) and recovers pre-existing
 *  `@relative` mentions. This handles the persisted-message shape where the
 *  server stores the token form in `content` AND keeps `meta.files` at once.
 *  Files referenced inline stay inline chips; the rest become block cards.
 *  Images keep their inline `![image](path)` markdown and are excluded here. */
function renderFileSegment(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, keyBase: string) {
  const parsedFiles = parseFiles(content, meta)

  // No attachments — plain markdown (bold, code, links, etc.).
  // softBreaks: preserve Shift+Enter line breaks as <br> (see MarkdownRenderer).
  if (!parsedFiles.length) {
    return <MarkdownRenderer content={content} softBreaks />
  }

  // Pass the ORIGINAL ordered list (images included) so [attached_file N] token
  // indices line up; resolveFileSegment filters images out of its output.
  const { display, mentionMap, cardPaths, labels } = resolveFileSegment(content, parsedFiles)

  // renderFileSegment handles the WHOLE message (non-paste path), so every
  // attachment belongs to this segment. Cards = standalone-upload tokens in the
  // text PLUS any attachment never referenced at all (e.g. optimistic
  // empty-caption bubble whose content carries no token yet). The
  // never-referenced set is computed by the shared findUnreferencedAttachments
  // (same original-list indexing), deduped against tokens already carded here.
  const carded = new Set(cardPaths)
  const allCardPaths = [
    ...cardPaths,
    ...findUnreferencedAttachments(display, parsedFiles).filter(p => !carded.has(p)),
  ]

  const cards = allCardPaths.length ? (
    <div key={`${keyBase}-cards`} className="flex flex-col gap-1.5 mt-1 first:mt-0">
      {allCardPaths.map((p, i) => (
        <FileAttachmentCard key={`${keyBase}-c${i}`} fullPath={p} label={labels.get(p) || p} onFileOpen={onFileOpen} />
      ))}
    </div>
  ) : null

  // No inline @-mentions: caption (if any) is plain markdown, then the cards.
  if (!mentionMap.size) {
    const caption = display.trim()
    return <>{caption ? <MarkdownRenderer key={`${keyBase}-cap`} content={caption} softBreaks /> : null}{cards}</>
  }

  // Inline-mention path: the caption keeps files inline, so render it as a
  // single inline flow — text runs as whitespace-preserving spans (NOT block
  // MarkdownRenderer, which wraps each run in a <p> and would break the line
  // around the chip) and each @token as an inline chip. Block markdown (bold,
  // lists) inside a caption that also carries an inline mention renders as
  // literal text — a rare combination, same trade-off as renderInlineSegment.
  // Cap tokens to prevent ReDoS from many alternations.
  const keys = [...mentionMap.keys()].slice(0, 20)
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))
  const body = (
    <span key={`${keyBase}-body`} style={{ whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        const tok = part.match(/^@(.+)$/)?.[1]
        const fullPath = tok && mentionMap.get(tok)
        if (fullPath) {
          return (
            <Clickable key={`${keyBase}-f${i}`} className="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors"
              title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={`Open file ${fullPath}`}>@{tok}</Clickable>
          )
        }
        return part ? <span key={`${keyBase}-p${i}`}>{part}</span> : null
      })}
    </span>
  )
  return <>{body}{cards}</>
}

/** Stable empty set so the mcpApps-derived selector returns a referentially
 *  equal value when the slot has no app renders (avoids useless re-renders). */
const EMPTY_APP_ID_SET: ReadonlySet<string> = new Set()

export default function ChatPage({ mode, embedded, embedMode, popout, noUrlSync }: { mode?: string; embedded?: boolean; embedMode?: 'chat' | 'sessions'; popout?: boolean; noUrlSync?: boolean } = {}) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const location = useLocation()
  const queryClient = useQueryClient()
  const provider = useProvider()
  const [searchParams, setSearchParams] = useSearchParams()
  const slots = useAppSelector(s => s.dashboard.slots)
  // Unified chat view: show both default and orchestrator slots together.
  // App-owned worker slots (s.app) are excluded by the sidebar itself.
  const filteredSlots = useMemo(
    () => slots.filter(s => {
      const sk = s.surface ?? s.mode ?? ''
      return sk === '' || sk === 'orchestrator'
    }),
    [slots],
  )
  const filteredSlotsRef = useRef(filteredSlots)
  filteredSlotsRef.current = filteredSlots
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  // Unified view: unread keys for all chat-like slots (both default and orchestrator).
  const surfaceUnreadSlots = useMemo(
    () => {
      if (unreadSlots.length === 0) return []
      const visibleKeys = new Set(filteredSlots.map(s => s.key))
      return unreadSlots.filter(k => visibleKeys.has(k))
    },
    [unreadSlots, filteredSlots],
  )
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const connected = useConnected()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  // tool_call_ids in THIS slot that have a live MCP App render payload. Passed
  // to TurnBlock so app-bearing rows (which mount an interactive iframe) never
  // fold into a collapsible pane — collapsing hides the app, and re-expanding
  // remounts the iframe and loses in-canvas state. Kept here rather than inside
  // TurnBlock because that component is also rendered by app-sdk/ChatEmbed with
  // no Redux Provider mounted. The custom equality fn keeps the derived Set
  // referentially stable across unrelated chat-state updates.
  const appToolCallIds = useAppSelector(s => {
    const apps = s.chat.mcpApps
    if (!activeSlot || !apps) return EMPTY_APP_ID_SET
    const prefix = mcpAppKey(activeSlot, '')
    const ids = Object.keys(apps).filter(k => k.startsWith(prefix)).map(k => k.slice(prefix.length))
    return ids.length ? new Set(ids) : EMPTY_APP_ID_SET
  }, (a, b) => a.size === b.size && [...a].every(id => b.has(id)))
  const messages = useAppSelector(s => s.chat.messages)
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const knowledgeFetch = useKnowledgeFetch(activeSlot)
  const knowledgeFetchRef = useRef(knowledgeFetch)
  knowledgeFetchRef.current = knowledgeFetch
  // User-sent messages (oldest → newest) for ↑/↓ prompt history in the input.
  // Deduplicate consecutive identical prompts to match shell/REPL behavior.
  // `messages` gets a new reference on every streaming chunk; preserve the
  // previous array when user-message content is unchanged so `sentMessages`
  // stays referentially stable and doesn't re-run downstream effects.
  const sentMessagesRef = useRef<string[]>([])
  const sentMessagesSlotRef = useRef<string | null>(null)
  // Per-slot timestamp (ms) of the last soft-stop press, used to arm the
  // force-kill. A force press (second click while soft_pending) arriving
  // within FORCE_KILL_ARMING_MS of that slot's soft stop is treated as an
  // accidental rapid double-tap and ignored, so users can't hard-kill by
  // mashing Stop. Keyed by slot so switching slots can't measure one slot's
  // press against another slot's timestamp.
  const softStopAtMapRef = useRef<Map<string, number>>(new Map())
  const sentMessages = useMemo(() => {
    const out: string[] = []
    for (const m of messages) {
      if (m.role !== 'user') continue
      const text = m.rawText ?? m.content
      if (!text || text === out[out.length - 1]) continue
      out.push(text)
    }
    // Reset the cached reference when switching slots — otherwise two
    // conversations with matching length+tail would share the prior array.
    if (sentMessagesSlotRef.current !== activeSlot) {
      sentMessagesSlotRef.current = activeSlot ?? null
      sentMessagesRef.current = out
      return out
    }
    // Append-only within a slot — full element-wise compare (array is small).
    const prev = sentMessagesRef.current
    if (prev.length === out.length && prev.every((v, i) => v === out[i])) {
      return prev
    }
    sentMessagesRef.current = out
    return out
  }, [messages, activeSlot])
  const slotRunning = useAppSelector(s => s.chat.slotRunning)
  // Shared composer-busy rule (chatSlice.selectComposerBusy). Drives the
  // composer's busy/queue affordance so a message sent during a sub-agent run
  // reads as "will queue".
  const composerBusy = useAppSelector(s => selectComposerBusy(s, s.chat.activeSlot))
  const slotStopping = useAppSelector(s => s.chat.slotStopping)
  const slotLoading = useAppSelector(s => s.chat.slotLoading)
  const pendingQuestion = useAppSelector(s => pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot))
  const pendingFollowup = useAppSelector(s => (s.chat.activeSlot ? s.chat.followups?.[s.chat.activeSlot] : undefined))
  const followupTsBySlot = useAppSelector(s => s.chat.followups) ?? EMPTY_FOLLOWUPS
  // The ambient tip yields to functional surfaces that own the above-composer band
  const tipSuppressed = useAppSelector(s =>
    s.chat.messages.some(m => m.role === 'queued') ||
    // Question card only renders for its OWNING slot (see the render-site
    // slot check below) -- suppression must match, or a question pending in
    // another running slot suppresses tips here forever (Codex round-31,
    // same slot-ownership family as the workflowRuns fix in round-16).
    !!pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot) ||
    // The follow-up card occupies the same above-composer band. Cards are
    // slot-keyed, so read only the ACTIVE slot's entry — a card parked in
    // another session must not suppress tips here.
    (!!s.chat.activeSlot && !!s.chat.followups?.[s.chat.activeSlot]) ||
    // Active subagents render the progress bar in the same above-composer
    // zone the floating tip occupies — the tip always yields (Raymond's
    // hard constraint: never crowd the queue/subagent surfaces).
    Object.values(s.chat.subagents).some(a => a.status === 'running' || a.status === 'tool' || a.status === 'pending') ||
    // Workflow runs render WorkflowProgressBar in the same band (Codex
    // round-15) — but only runs belonging to THIS slot show a bar here, so
    // filter by ownership or a terminal run parked in another slot would
    // suppress tips everywhere forever (Codex round-16).
    Object.values(s.chat.workflowRuns ?? {}).some(r => runBelongsToSlot(r.sessionKey, s.chat.activeSlot) && (r.status === 'running' || r.status === 'finished' || r.status === 'failed' || r.status === 'cancelled'))
  ) || knowledgeFetch.loading || knowledgeFetch.results.length > 0
  // Split View state is declared up here (not at its usage site) because the
  // tip hook below must know about it: in split mode SessionGridView replaces
  // the composer, TipCard never renders, and an unblocked hook would fetch a
  // tip + record it as shown, silently burning the 6h cadence (Codex round-25).
  const [splitMode, setSplitMode] = useState(false)
  const [splitAnchor, setSplitAnchor] = useState<string | null>(null)
  // Temporary sessions ("no memory reads or writes") must never show
  // memory-personalized tips (Codex round-22).
  const tipTemporary = useAppSelector(s => s.dashboard.slots.find(sl => sl.key === s.chat.activeSlot)?.memory_mode === 'temporary')
  const tipBlocked = tipTemporary || splitMode || embedMode === 'sessions'
  const { tip: activeTip, dismiss: dismissTip } = useTipTrigger(!!slotRunning, tipSuppressed, activeSlot, tipBlocked)
  const slotState = useAppSelector(s => s.chat.slotState)
  const contextPct = useAppSelector(s => s.chat.slotContextPct[s.chat.activeSlot ?? ''] ?? 0)
  const contextTokens = useAppSelector(s => s.chat.slotContextTokens?.[s.chat.activeSlot ?? ''])
  // Length only. The two arrays themselves are mutated per streamed sub-agent /
  // tool chunk, and their only consumer is the Activity panel (SidePanel), which
  // is closed by default and now subscribes to them itself. Subscribing to the
  // arrays here re-rendered this whole component per chunk for data it never
  // read. The touched-file scan below needs the entries, but only when the log
  // GREW, so it reads them from the store at effect time instead.
  const toolLogLen = useAppSelector(s => s.chat.toolLog.length)
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  const slotHasMore = useAppSelector(s => s.chat.slotHasMore)
  const history = useAppSelector(s => s.chat.history)
  const historyHasMore = useAppSelector(s => s.chat.historyHasMore)

  const drafts = useRef<Record<string, string>>(null!)
  if (drafts.current === null) drafts.current = loadDrafts()
  const fileDrafts = useRef<Record<string, string[]>>(null!)
  if (fileDrafts.current === null) fileDrafts.current = loadFileDrafts()
  // Per-slot collapsed-paste blocks backing the `[ Paste #N · M lines ]` tokens
  // in `input`. Persisted (localStorage, same TTL as text drafts) so the chip
  // survives slot switches / refresh instead of degrading to literal text.
  const pasteDrafts = useRef<Record<string, PasteBlock[]>>(null!)
  if (pasteDrafts.current === null) pasteDrafts.current = loadPasteDrafts()
  const saveDraftsTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const saveDrafts = useCallback(() => { persistDrafts(drafts.current); persistFileDrafts(fileDrafts.current); persistPasteDrafts(pasteDrafts.current) }, [])
  const saveDraftsDebounced = useCallback(() => {
    if (saveDraftsTimer.current) clearTimeout(saveDraftsTimer.current)
    saveDraftsTimer.current = setTimeout(() => { saveDraftsTimer.current = null; saveDrafts() }, DRAFT_SAVE_DEBOUNCE_MS)
  }, [saveDrafts])
  const flushDrafts = useCallback(() => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    saveDrafts()
  }, [saveDrafts])
  // Outgoing-slot flush key, advanced inside the slot-change effect after it
  // flushes that slot's draft. Distinct from composerSlotRef (the live persist
  // key); both must trail their writes or the draft smear returns.
  const prevSlot = useRef<string | null>(null)
  // Latest-value ref for `activeSlot`, updated every render. Used by async
  // upload callbacks (takeScreenshot, uploadFiles) to detect when the user
  // has switched slots between the initial click and the promise resolving,
  // so the uploaded file lands in the original slot's draft instead of
  // silently appearing in whatever slot is now active.
  const activeSlotRef = useRef(activeSlot); activeSlotRef.current = activeSlot
  // The slot the live composer state belongs to; the per-composer persist
  // effects key off this, not `activeSlot`. Advanced by a dedicated effect
  // declared AFTER those effects so a batched keystroke+switch can't smear one
  // slot's draft onto another. See that advance effect for the full rationale.
  const composerSlotRef = useRef(activeSlot)
  const [input, setInput] = useState(() => activeSlot ? drafts.current[activeSlot] ?? '' : '')

  // History suggestions ("Continue a previous chat?") shown above the input on the welcome screen.
  const sendingRef = useRef(false)
  const [historyQuery, setHistoryQuery] = useState('')
  const [historyDismissed, setHistoryDismissed] = useState(false)
  useEffect(() => {
    const q = input.trim()
    if (!q) { setHistoryQuery(''); setHistoryDismissed(false); return }
    setHistoryDismissed(false)
    const t = setTimeout(() => setHistoryQuery(q.toLowerCase()), 300)
    return () => clearTimeout(t)
  }, [input])
  const historySuggestions = useMemo(() =>
    historyQuery && history.length
      ? history.filter(s => (s.title || '').toLowerCase().includes(historyQuery) || s.key.toLowerCase().includes(historyQuery)).slice(0, 5)
      : [],
    [historyQuery, history])
  /* `!pendingQuestion`: the welcome hero is vertically centred in the empty
     transcript, which is the same space the question card occupies above the
     composer -- with both mounted they visibly overlap. An agent that asks
     before producing any output is a real case (it happens on the very first
     turn), so the card wins and the welcome content stands down. */
  const isWelcomeState = messages.length === 0 && !slotRunning && !slotLoading && !sendingRef.current && !knowledgeFetch.results.length && !knowledgeFetch.loading && !knowledgeFetch.pendingKnowledge && !pendingQuestion
  const showHistorySuggestions = isWelcomeState && historySuggestions.length > 0 && !historyDismissed
  useEffect(() => {
    if (!showHistorySuggestions) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setHistoryDismissed(true) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [showHistorySuggestions])
  // Browse mode is per-session (keyed by slot), not page-global: enabling it in
  // one session must not bleed into another. ChatPage never remounts on slot
  // switch, so a single boolean would leak across every session. Kept in-memory
  // only (resets on reload), matching the prior non-persisted behavior.
  const [browseModeBySlot, setBrowseModeBySlot] = useState<Record<string, boolean>>({})
  const browseMode = activeSlot ? (browseModeBySlot[activeSlot] ?? false) : false
  const toggleBrowseMode = () => {
    const slot = activeSlotRef.current
    if (!slot) return
    setBrowseModeBySlot(prev => ({ ...prev, [slot]: !(prev[slot] ?? false) }))
  }
  // Broadcast the active slot's browse-mode ("Let the agent use the browser") so the Browser
  // panel's live mirror can show "Let the agent act" only while it's OFF.
  // (browseModeRef, kept in sync with browseMode below, is reused by the
  // browse-frame effect to replay state to a late-mounting panel.)
  useEffect(() => {
    window.dispatchEvent(new CustomEvent(BROWSE_MODE_EVENT, { detail: { on: browseMode } }))
  }, [browseMode])
  // The Browser panel's "Let the agent act" button requests turning browse mode
  // ON for the active slot (idempotent — never toggles it back off).
  useEffect(() => {
    const onEnable = (e: Event) => {
      // Prefer the slot carried by the panel (the browsing session whose page is
      // shown); fall back to the active slot only if none was supplied. This
      // keeps the [BROWSE] grant attributed to the correct session.
      const slot = (e as CustomEvent<{ slot?: string }>).detail?.slot || activeSlotRef.current
      if (!slot) return
      setBrowseModeBySlot(prev => (prev[slot] ? prev : { ...prev, [slot]: true }))
    }
    window.addEventListener(PREVIEW_ENABLE_BROWSE_EVENT, onEnable)
    return () => window.removeEventListener(PREVIEW_ENABLE_BROWSE_EVENT, onEnable)
  }, [])
  const pendingInput = useAppSelector(s => s.chat.pendingInput)

  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])

  const { agents: installedAgents, defaultAgent } = useAgents(refreshTrigger)
  const { open: agentDropdown, setOpen: setAgentDropdown, filter: agentFilter, setFilter: setAgentFilter, dropdownRef: agentDropdownRef, inputRef: agentInputRef, filtered: filteredAgentsByName } = useFilteredDropdown(installedAgents)
  const filteredAgents = filteredAgentsByName
  const { data: availableModels = [{ name: 'auto', description: 'Default' }] } = useQuery({
    queryKey: ['available-models', provider.id],
    queryFn: async () => {
      const models = await provider.fetchAvailableModels()
      return [{ name: 'auto', description: 'Default' }, ...models.filter(m => m.name !== 'auto')]
    },
    refetchInterval: modelListRefetchInterval,
  })
  const { open: modelDropdown, setOpen: setModelDropdown, filter: modelFilter, setFilter: setModelFilter, dropdownRef: modelDropdownRef, inputRef: modelInputRef, filtered: filteredModels } = useFilteredDropdown(availableModels)
  // Roving-focus keyboard nav for the agent + model dropdowns (shared with StyledSelect/AgentSelector).
  const { onListKeyDown: onAgentListKeyDown } = useListboxKeyboard({
    open: agentDropdown,
    dropdownRef: agentDropdownRef,
    inputRef: agentInputRef,
    hasFilterInput: true,
    filteredCount: filteredAgents.length,
    onEnterSingleMatch: () => {
      const a = filteredAgents[0]
      if (a) { switchAgent(a.name); setAgentDropdown(false) }
    },
    closeToTrigger: () => setAgentDropdown(false),
  })
  const { onListKeyDown: onModelListKeyDown } = useListboxKeyboard({
    open: modelDropdown,
    dropdownRef: modelDropdownRef,
    inputRef: modelInputRef,
    hasFilterInput: true,
    filteredCount: filteredModels.length,
    onEnterSingleMatch: () => { switchModel(filteredModels[0].name); setModelDropdown(false) },
    closeToTrigger: () => setModelDropdown(false),
  })
  const [pendingAgent, _setPendingAgent] = useState('')  // agent for next new slot
  const pendingAgentRef = useRef('')
  const setPendingAgent = useCallback((v: string) => { pendingAgentRef.current = v; _setPendingAgent(v) }, [])
  const [pendingModel, _setPendingModel] = useState('')  // model for next new slot
  const pendingModelRef = useRef('')
  const setPendingModel = useCallback((v: string) => { pendingModelRef.current = v; _setPendingModel(v) }, [])
  const pendingProjectRef = useRef('')
  const setPendingProject = useCallback((v: string) => { pendingProjectRef.current = v }, [])

  // Sync pendingModel with default agent's model on initial load
  const _initAgent = pendingAgent || defaultAgent || 'default'
  const _initMc = installedAgents.find(a => a.name === _initAgent)
  const _initTemplateName = installedAgents.length > 0 ? provider.resolveAgentTemplate(_initMc || { name: _initAgent }) : ''
  const { data: _initResolvedModel } = useQuery({
    queryKey: ['resolved-model', _initTemplateName, provider.id],
    queryFn: () => provider.resolveModel(_initTemplateName),
    enabled: !!_initTemplateName && !pendingModel,
  })
  useEffect(() => { if (_initResolvedModel && !pendingModel) setPendingModel(_initResolvedModel) }, [_initResolvedModel]) // eslint-disable-line react-hooks/exhaustive-deps
  const [modelBtnRect, setModelBtnRect] = useState<DOMRect | null>(null)
  const planActionMutation = useMutation({
    mutationFn: ({ slot, action }: { slot: string; action: string }) => api.planAction(slot, action),
  })
  // Mid-turn steer is a POST write, so it goes through useMutation for
  // consistent error/loading-state handling (fire-and-forget: no onSuccess).
  const steerMutation = useMutation({
    mutationFn: (text: string) => api.steerChat(text, activeSlot!),
    onError: (e) => { console.error('steer failed', e) },
  })
  const [reasoningEffortDropdown, setReasoningEffortDropdown] = useState(false)
  const [reasoningEffortBtnRect, setReasoningEffortBtnRect] = useState<DOMRect | null>(null)
  const reasoningEffortDropdownRef = useRef<HTMLDivElement>(null)
  const [autoNudgeOpen, setAutoNudgeOpen] = useState(false)
  const [autoNudgeLoop, setAutoNudgeLoop] = useState<AutoNudgeLoop | null>(null)
  const approvalMode = useAppSelector(s => s.dashboard.approvalMode)

  // ── Reasoning effort dropdown click-outside ──
  useEffect(() => {
    if (!reasoningEffortDropdown) return
    const handler = (e: MouseEvent) => {
      if (reasoningEffortDropdownRef.current?.contains(e.target as Node)) return
      if (reasoningEffortBtnRect) {
        const r = reasoningEffortBtnRect
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
      }
      setReasoningEffortDropdown(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [reasoningEffortDropdown, reasoningEffortBtnRect])

  // ── Auto-nudge: fetch loop state for active slot, subscribe to WS updates ──
  useEffect(() => {
    // Clear stale state and close the popover on slot switch so it remounts
    // with fresh useState initializers sourced from the new slot's loop.
    // Otherwise the popover's internal message/idleSecs/maxCycles retain
    // values from the previously-active slot and a Start click would arm the
    // wrong nudge on the new session.
    setAutoNudgeLoop(null)
    setAutoNudgeOpen(false)
    if (!activeSlot) return
    let cancelled = false
    fetch(`/api/autonudge/slot/${encodeURIComponent(activeSlot)}`)
      .then(r => r.json())
      .then(d => { if (!cancelled) setAutoNudgeLoop(d.loop || null) })
      .catch(() => {})
    const onEvent = (e: Event) => {
      const detail = (e as CustomEvent).detail as { slot?: string; loop?: AutoNudgeLoop; event?: string }
      if (!detail || detail.slot !== activeSlot) return
      setAutoNudgeLoop(detail.event === 'removed' ? null : (detail.loop ?? null))
    }
    window.addEventListener('autonudge_state', onEvent)
    return () => { cancelled = true; window.removeEventListener('autonudge_state', onEvent) }
  }, [activeSlot])
  const {
    scrollerRef,
    scrollToDisplayIndex,
  } = useScrollManager()

  // Single scroll controller: the virtualizer (`virt`, created below) owns
  // follow + scroll-to-bottom. These refs bridge the early effects/handlers
  // (declared before `virt` in source order) to the virtualizer's API without
  // a temporal-dead-zone hazard — they are populated right after `virt` is
  // created and only read inside callbacks/effects that run post-render.
  const isAtBottomRef = useRef(true)
  const vScrollToBottomRef = useRef<(behavior?: ScrollBehavior) => void>(() => {})
  const mountIndexRef = useRef<(index: number) => boolean>(() => false)
  const scrollToIndexSmoothRef = useRef<(index: number, opts?: { align?: 'start' | 'center'; offset?: number }) => void>(() => {})

  const [prefillHint, setPrefillHint] = useState(false)
  const autoSendRef = useRef<string | null>(null)
  const [autoSendTick, setAutoSendTick] = useState(0)
  const newSessionRef = useRef(false)
  // True while the challenge-redirect token effect is creating/linking its
  // session. Blocks the auto-select effect from switching to a different slot
  // (which would orphan the freshly slack-linked session and break mirroring).
  const tokenConsumingRef = useRef(
    typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('token'),
  )
  const inputRef = useRef(input)
  inputRef.current = input
  const browseModeRef = useRef(browseMode)
  browseModeRef.current = browseMode
  // Holds the exact text a widget action pre-filled into the composer, so the
  // eventual user-initiated send can be tagged meta.origin='widget' for
 // forensic attribution. Set on widget pre-fill, consumed
  // and cleared in send(). A genuine from-scratch turn never sets this.
  const widgetPrefillRef = useRef<string | null>(null)

  // Auto-dismiss prefill hint after 10 seconds
  useEffect(() => {
    if (!prefillHint) return
    const t = setTimeout(() => setPrefillHint(false), 10000)
    return () => clearTimeout(t)
  }, [prefillHint])

  // Consume pendingInput from Redux (e.g. from "Chat" button on Projects page)
  useEffect(() => {
    if (pendingInput) {
      dispatch(setPendingInput(null))
      const shouldAutoSend = embedded ? false : searchParams.get('autoSend') === '1'
      const wantNew = embedded ? false : searchParams.get('newSession') === '1'
      if (!embedded && (searchParams.get('prefill') || shouldAutoSend)) setSearchParams({}, { replace: true })
      if (shouldAutoSend) { autoSendRef.current = pendingInput; newSessionRef.current = wantNew } else {
        if (activeSlot) { setDraft(drafts.current, activeSlot, pendingInput); saveDraftsDebounced() }
        setInput(pendingInput)
        setPrefillHint(true)
      }
    }
  }, [pendingInput, activeSlot, dispatch, searchParams, setSearchParams, saveDraftsDebounced, embedded])

  // Consume chat launch intent from app-sdk (useChatLauncher writes to window.__mc_chat_launch)
  useEffect(() => {
    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts?: number; agent?: string; message?: string }
    }
    const intent = launchWindow.__mc_chat_launch
    if (!intent || Date.now() - (intent.ts ?? 0) > 10_000) return
    delete launchWindow.__mc_chat_launch
    if (intent.agent) setPendingAgent(intent.agent)
    if (intent.message) { autoSendRef.current = intent.message; newSessionRef.current = true }
    // setPendingAgent is a stable useState setter, so including it keeps this a
    // mount-only "consume the one-shot window global" effect.
  }, [setPendingAgent])

  // Consume ?prefill= — the no-main-window fallback path for navigation
  // intents forwarded from a popout (see utils/popoutController.ts). The
  // fallback opens `/chat?sid=<slot>&prefill=<prompt>` in a fresh tab, which
  // has no sessionStorage of its own yet: seed PREFILL_STORAGE_KEY from the
  // param so the slot-restore effect prefills the composer when the ?sid slot
  // activates, then strip the param (keep ?sid) so the prompt doesn't leak
  // into history/bookmarks or re-seed on refresh.
  useEffect(() => {
    if (embedded) return
    const sp = new URLSearchParams(window.location.search)
    const prefill = sp.get('prefill')
    if (prefill === null) return
    const sid = sp.get('sid') || sp.get('slot')
    if (sid && prefill) {
      safeSetSessionItem(
        PREFILL_STORAGE_KEY,
        JSON.stringify({ slotKey: sid, prompt: prefill, ts: Date.now() }),
      )
    }
    sp.delete('prefill')
    const qs = sp.toString()
    window.history.replaceState({}, '', window.location.pathname + (qs ? `?${qs}` : ''))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Consume prompt from token payload (channel challenge-and-redirect flow).
  // The prompt is HMAC-signed in the token — server validates the signature
  // and sets the session cookie before the SPA loads. No auto-send — the user
  // must press Enter to confirm.
  //
  // Three cases, driven by signed claims in the token:
  //  1. session_key present → the originating Slack thread is already linked to
  //     a dashboard session; reconnect to THAT session instead of making a new
  //     one (fixes "thread reply spawns a disconnected session").
  //  2. channel + thread_ts present (no session_key) → fresh thread; create a
  //     new session and auto-link it back to that Slack thread so agent
  //     responses flow into the thread.
  //  3. neither → plain new session (e.g. a top-level channel message).
  // In all cases the prompt is seeded via PREFILL_STORAGE_KEY (the channel the
  // slot-restore effect honors) AND set directly once the target slot is
  // active, so the previous slot's draft can't clobber it.
  useEffect(() => {
    // tokenConsumingRef is initialized true when a token is in the URL; every
    // early return below MUST clear it, or the auto-select guard stays engaged
    // for the whole session and blocks slot selection.
    if (embedded) { tokenConsumingRef.current = false; return }
    const token = new URLSearchParams(window.location.search).get('token')
    if (!token) { tokenConsumingRef.current = false; return }
    // Always strip token from URL to prevent leakage via referrer/history
    window.history.replaceState({}, '', window.location.pathname)
    const prompt = extractPromptFromToken(token)
    if (!prompt) { tokenConsumingRef.current = false; return }
    const { sessionKey, channel, threadTs } = extractSlackContextFromToken(token)
    // Backend session keys are history keys (dashboard:chat-…); the frontend
    // slot key is the bare form.
    const targetSlot = sessionKey ? sessionKey.replace(/^dashboard:/, '') : null
    tokenConsumingRef.current = true
    ;(async () => {
     try {
      let slotKey: string | null = null
      if (targetSlot) {
        // Case 1: reconnect to the existing linked session.
        try {
          await dispatch(switchSlot(targetSlot)).unwrap()
          slotKey = targetSlot
        } catch {
          // Session vanished (deleted/expired) — fall back to a new one.
        }
      }
      if (!slotKey) {
        // No targetSlot (or reconnect failed): create the session HERE and,
        // for a fresh thread, slack-link it so responses mirror to Slack.
        try {
          const slot = await dispatch(createSlot({ mode })).unwrap()
          slotKey = slot?.key ?? null
        } catch {
          // ignore — fall back to prefilling the current slot
        }
        // Case 2: auto-link the new session back to the originating thread so
        // responses flow into Slack. Best-effort; failure just leaves it
        // unlinked.
        if (slotKey && channel && threadTs) {
          try { await api.slackLink(slotKey, channel, threadTs) } catch { /* non-fatal */ }
        }
      }
      // We have created/reconnected AND made the target slot active. Critically,
      // clear newSessionRef and pin activeSlot to this slot so send() reuses it
      // on Enter — otherwise send()'s forceNew path would spawn a SECOND,
      // unlinked slot and break Slack mirroring.
      if (slotKey) {
        newSessionRef.current = false
        dispatch(switchSlot(slotKey))
        safeSetSessionItem(
          PREFILL_STORAGE_KEY,
          JSON.stringify({ slotKey, prompt, ts: Date.now() }),
        )
      }
      setInput(prompt)
      setPrefillHint(true)
      autoSendRef.current = prompt
      setAutoSendTick(t => t + 1)
     } finally {
      // Release the auto-select guard once the session is created/linked (or
      // failed), so normal slot selection resumes.
      tokenConsumingRef.current = false
     }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Persist the composer text against the slot it BELONGS to (composerSlotRef),
  // not the live activeSlot (see the composerSlotRef note above).
  useEffect(() => { inputRef.current = input; const s = composerSlotRef.current; if (s) { setDraft(drafts.current, s, input); saveDraftsDebounced() } }, [input, saveDraftsDebounced]) // eslint-disable-line react-hooks/exhaustive-deps -- draft key is composerSlotRef; slot-change effect handles the transition
  // Per-slot draft: save current → restore target (persisted to localStorage)
  useEffect(() => {
    // Re-hydrate from localStorage — only pull in keys we don't already have
    // in-memory, so unflushed drafts from rapid slot switches aren't clobbered.
    const stored = loadDrafts()
    for (const [k, v] of Object.entries(stored)) { if (!(k in drafts.current)) drafts.current[k] = v }
    const storedFiles = loadFileDrafts()
    for (const [k, v] of Object.entries(storedFiles)) { if (!(k in fileDrafts.current)) fileDrafts.current[k] = v }
    const storedPastes = loadPasteDrafts()
    for (const [k, v] of Object.entries(storedPastes)) { if (!(k in pasteDrafts.current)) pasteDrafts.current[k] = v }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    prevSlot.current = activeSlot
    const raw = sessionStorage.getItem(PREFILL_STORAGE_KEY)
    const draftFallback = activeSlot ? drafts.current[activeSlot] ?? '' : ''
    if (raw) {
      try {
        const { slotKey, prompt, ts } = JSON.parse(raw)
        if (Date.now() - (ts ?? 0) > 30_000) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
        else if (slotKey === activeSlot) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(prompt) }
        else { setInput(draftFallback) }
      } catch { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
    } else { setInput(draftFallback) }
    // Restore the incoming slot's staged file attachments (copy so the
    // live state array and the stored draft don't share a reference).
    setPendingFiles(activeSlot ? (fileDrafts.current[activeSlot] ?? []).slice() : [])
    // Restore the incoming slot's collapsed-paste blocks (deep copy so the live
    // state and the stored draft don't share references). Without this the
    // token text rehydrates from the text draft but its backing block is gone,
    // leaving a dead `[ Paste #N · M lines ]` literal in the input.
    setPasteBlocks(activeSlot
      ? (pasteDrafts.current[activeSlot] ?? []).map(b => ({ ...b }))
      : [])
    knowledgeFetchRef.current.clearResults()
    setUploadError('')
    flushDrafts()
  }, [activeSlot, flushDrafts])
  // Persist drafts on unmount (navigating away from chat page)
  useEffect(() => () => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    flushDrafts()
  }, [flushDrafts])
  // Flush pending draft save on tab close / refresh (debounce may not fire)
  useEffect(() => {
    const h = () => {
      if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
      if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
      if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
      flushDrafts()
    }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [flushDrafts])
  const [agentBtnRect, setAgentBtnRect] = useState<DOMRect | null>(null)
  const [projectPickerOpen, setProjectPickerOpen] = useState(false)
  const [projectBtnRect, setProjectBtnRect] = useState<DOMRect | null>(null)

  // Prevent Chrome from navigating to dropped files.
  // Must be on document to catch drops anywhere on the page.
  useEffect(() => {
    const preventNav = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes('Files')) {
        e.preventDefault()
        e.dataTransfer.dropEffect = 'copy'
      }
    }
    document.addEventListener('dragover', preventNav)
    document.addEventListener('drop', preventNav)
    return () => {
      document.removeEventListener('dragover', preventNav)
      document.removeEventListener('drop', preventNav)
    }
  }, [])

  const [dragOver, setDragOver] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  const [snipFrame, setSnipFrame] = useState<HTMLCanvasElement | null>(null)
  // The slot that INITIATED the current snip. getDisplayMedia + cropping is
  // async and the user may switch slots meanwhile, so the cropped image must
  // land in the slot that started the capture — not whatever is active when the
  // crop completes. Threaded into uploadFiles as an explicit target.
  const snipSlotRef = useRef<string | null>(null)
  const pendingFilesRef = useRef(pendingFiles)
  useEffect(() => {
    pendingFilesRef.current = pendingFiles
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setFileDraft(fileDrafts.current, s, pendingFiles)
      saveDraftsDebounced()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- draft key is
    // composerSlotRef; slot-change effect handles that transition
  }, [pendingFiles, saveDraftsDebounced])
  // Collapsed paste blocks backing the `[ Paste #N · M lines ]` tokens in
  // `input`. Persisted per-slot via chatPasteDrafts (localStorage, 30-day TTL)
  // so they survive slot switches / refresh; cleared on send and slot delete.
  const [pasteBlocks, setPasteBlocks] = useState<PasteBlock[]>([])
  const pasteBlocksRef = useRef(pasteBlocks)
  useEffect(() => {
    pasteBlocksRef.current = pasteBlocks
    // Live-persist the composer's blocks so a slot switch / refresh restores
    // them alongside the text draft (mirrors the pendingFiles effect above).
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setPasteDraft(pasteDrafts.current, s, pasteBlocks)
      saveDraftsDebounced()
    }
    // draft key is composerSlotRef; slot-change effect handles that transition.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pasteBlocks, saveDraftsDebounced])
  // Advance the composer draft key AFTER the three persist effects above. React
  // runs effects in declaration order, so on a slot switch each persist effect
  // has already written its changed value against the OUTGOING slot before this
  // repoints the key at the incoming one. Declared last on purpose. Moving it
  // earlier (or back into the slot-change effect) would let a file/paste change
  // batched with the switch smear onto the new slot.
  useEffect(() => { composerSlotRef.current = activeSlot }, [activeSlot])
  const [uploadError, setUploadError] = useState('')
  // Resize details keyed by uploaded server path. Rendered as a badge on the
  // attachment chip itself (FilePreviewStrip) instead of a banner — the info
  // describes one staged file, so it lives on that file's chip. Keyed by the
  // unique upload path, entries stay valid across slot switches (drafts
  // restore chips per slot) and stale keys are harmless.
  const [resizedInfo, setResizedInfo] = useState<Record<string, ResizeInfo>>({})
  const isMac = useAppSelector(s => s.dashboard.status?.platform) === 'darwin'
  const { data: sttCfg } = useQuery({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig() as Promise<{ streaming?: boolean; enabled?: boolean; dictation_panel?: boolean }>,
  })
  const sttStreaming = !!sttCfg?.streaming
  const sttEnabled = !!sttCfg?.enabled
  // Default true so the panel is the standard recording surface; the backend
  // sends an explicit boolean, so `undefined` here means "config not loaded yet"
  // rather than "off", and a pre-load recording would otherwise flash the bar.
  const sttDictationPanel = sttCfg?.dictation_panel !== false
  // Treat "config not loaded yet" as disabled so the guard never lets a
  // recording start before STT is confirmed on. Stable boolean so toggleVoice's
  // deps don't churn on every sttCfg object identity from a refetch.
  const sttConfigLoaded = !!sttCfg
  // Opened when the user clicks the mic while STT is disabled — points them at
  // the setting that turns it on instead of starting a recording that would
  // never be transcribed.
  const [voiceSetupOpen, setVoiceSetupOpen] = useState(false)
  const frozenInputRef = useRef<string | null>(null)
  // Drops late-arriving partials/finals from a previous slot or stopped
  // session. `stop()` is async (up to 5s for backend close) — without
  // this guard, a delayed onFinal would overwrite text the user has
  // already typed in the new slot.
  const sttDisarmedRef = useRef(false)
  const voice = useVoiceInput(
    useCallback((text: string) => {
      if (sttDisarmedRef.current) return
      const base = frozenInputRef.current ?? ''
      setInput(base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text)
      frozenInputRef.current = null
    }, []),
    {
      streaming: sttStreaming,
      onPartial: useCallback((text: string) => {
        if (sttDisarmedRef.current) return
        // Snapshot BEFORE setInput so the updater stays pure (no ref
        // mutation inside a function React may invoke twice).
        if (frozenInputRef.current === null) frozenInputRef.current = inputRef.current
        const base = frozenInputRef.current
        setInput(base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text)
      }, []),
    }
  )
  // Keep a ref to the latest `voice` so effects that intentionally omit
  // `voice` from their deps always invoke the current instance — otherwise
  // they'd capture a stale `toggle`/`recording` whenever `voice` identity
  // changes (e.g. when `sttStreaming` flips).
  const voiceRef = useRef(voice)
  useEffect(() => { voiceRef.current = voice }, [voice])
  // Same reason as voiceRef: send() deliberately keeps a minimal dep array (with
  // an exhaustive-deps suppression), so reading `sttStreaming` directly there
  // would close over the value from the render that created that send().
  const sttStreamingRef = useRef(sttStreaming)
  useEffect(() => { sttStreamingRef.current = sttStreaming }, [sttStreaming])
  // Re-arm when the user explicitly (re)starts recording — wrap toggle.
  // Depend on the individual stable members actually read so this callback
  // is only re-created when they change. `[voice]` would recreate every
  // render (hooks don't memoize their return by default), re-rendering all
  // child components that receive `toggleVoice` as a prop.
  const toggleVoice = useCallback(() => {
    // Starting a recording while server-side STT is disabled would capture
    // audio that never gets transcribed. Point the user at the enable setting
    // instead. Guard on !recording so this only gates the *start* — stopping
    // an in-progress recording is always allowed.
    if (!voice.recording && (!sttConfigLoaded || !sttEnabled)) {
      setVoiceSetupOpen(true)
      return
    }
    if (!voice.recording) {
      sttDisarmedRef.current = false
      // Reset stale snapshot from a prior session that ended without
      // finals — otherwise onPartial sees a non-null ref, skips
      // re-snapshotting, and text typed between sessions is dropped.
      frozenInputRef.current = null
    }
    voice.toggle()
    // Depend on the individual stable members actually read, not the whole
    // `voice` object — `[voice]` would recreate this callback every render and
    // re-render every child that receives `toggleVoice` (see comment above).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.recording, voice.toggle, sttEnabled, sttConfigLoaded])
  // Stop any in-flight recording and drop the frozen prefix when the user
  // switches slots so a late-arriving transcript can't leak into the wrong
  // session. Disarm first so any in-flight final from the previous slot is
  // silently dropped when it eventually arrives.
  useEffect(() => {
    frozenInputRef.current = null
    sttDisarmedRef.current = true
    if (voiceRef.current.recording) voiceRef.current.toggle()
  }, [activeSlot])
  // (Streaming-off teardown now lives in useVoiceInput — see its effect on
  // [streamEnabled, streamRecording, streamStop]. Routing through voice.toggle
  // here is racy because `useVoiceInput` flips its returned `recording` to the
  // batch value on the same render that `streamEnabled` goes false.)

  const tabsCtl = usePanelTabs(activeSlot)
  // Which file (if any) the Files tab is showing inline — kept PER SLOT (above
  // the SidePanel subtree so it survives panel collapse). Per-slot (not a single
  // value reset on switch) so it stays consistent with the per-slot tab buckets
  // AND the per-(slot,path) draft store: switching A→B→A restores A's inline
  // editor rather than resetting it, so handleFileOpen's one-editor-per-path
  // guard still recognizes the file as open inline after a round-trip (no
  // competing document tab, no stale-draft overwrite).
  const [inlinePreviewBySlot, setInlinePreviewBySlot] = useState<Record<string, string | null>>({})
  const inlinePreviewPath = inlinePreviewBySlot[activeSlot ?? ''] ?? null
  const inlinePreviewPathRef = useRef(inlinePreviewPath); inlinePreviewPathRef.current = inlinePreviewPath
  const setInlinePreviewPath = useCallback((p: string | null) => {
    setInlinePreviewBySlot(m => ({ ...m, [activeSlotRef.current ?? '']: p }))
  }, [])
  // Find/search pane state. Declared above handleFileOpen / handleOpenDiff so
  // those handlers can call search.close() directly when opening a dock panel
  // (the right-hand dock is a single slot and the file/diff panes are
  // render-gated behind !search.isOpen).
  const search = useMessageSearch(messages, activeSlot)
  const touchedFiles = useTouchedFiles(activeSlot ?? undefined)
  const sourceLinkIndex = useRef(new PullRequestLinkIndex())
  // Self-managed GitLab hosts the operator authorized (config-only, read-only
  // here). Without them a pasted self-hosted MR link is not a Changes source.
  // No refetchInterval: polling this shared ['dashboardConfig'] key turned every
  // same-key observer into a poller and wrote a dashboard_config_read SEL entry
  // on each tick. Instead the WS 'slots' push carries the allowlist generation
  // (see useWebSocket), which invalidates this query only when the allowlist
  // actually changes — an edit on disk still propagates, without the churn.
  const { data: sourceHostCfg } = useQuery<{ gitlab_hosts?: string[] }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })
  const indexedSourceLinks = sourceLinkIndex.current.update(
    activeSlot,
    messages,
    sourceHostCfg?.gitlab_hosts ?? [],
  )
  // One scan, one dedup map, two panels: the extractor returns pull requests and
  // issues together (they share the per-role cap), and the two side-panel tabs
  // consume the halves. useMemo keyed on the index's own result identity — the
  // index returns the SAME array reference until the transcript actually changes,
  // so the halves stay reference-stable and don't retrigger the reconciliation
  // effects below on every render.
  const { changes: sourceLinks, issues: issueLinks } = useMemo(
    () => partitionSourceLinks(indexedSourceLinks),
    [indexedSourceLinks],
  )
  const [selectedSourceUrl, setSelectedSourceUrl] = useState('')
  const [selectedIssueUrl, setSelectedIssueUrl] = useState('')

  // Add and focus the per-slot Changes / Issues tabs for newly detected URLs,
  // but leave panel visibility under explicit user control. Both kinds share one
  // seen-url bookkeeping set (it is keyed by url, and the cap is a per-slot
  // budget), so each kind is recorded separately only to learn WHICH tab to open.
  const [seenSourceUrls] = useState(loadSeenPullRequestLinks)
  useEffect(() => {
    const newChanges = recordNewPullRequestLinks(seenSourceUrls, activeSlot, sourceLinks)
    const newIssues = recordNewPullRequestLinks(seenSourceUrls, activeSlot, issueLinks)
    if (!newChanges && !newIssues) return
    persistSeenPullRequestLinks(seenSourceUrls)
    if (newChanges) tabsCtl.openView('changes')
    if (newIssues) tabsCtl.openView('issues')
    // tabsCtl is intentionally not a dependency: this effect reacts only to
    // source discovery, not tab focus or panel visibility changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, sourceLinks, issueLinks, seenSourceUrls])

  useEffect(() => {
    // An uncached slot temporarily has no messages while its history hydrates.
    // Preserve the persisted strip until that source-of-truth load settles.
    if (slotLoading) return
    if (sourceLinks.length === 0) {
      // Changes is a permanently pinned tab (SidePanel.syncPinned) — never
      // auto-close it here. Just clear the source selection; the tab stays put
      // and renders its empty state until sources are detected again.
      setSelectedSourceUrl('')
      return
    }
    if (!sourceLinks.some(source => source.url === selectedSourceUrl)) {
      setSelectedSourceUrl(sourceLinks[0].url)
    }
    // React to indexed sources, selection, and hydration completion only;
    // tabsCtl changes identity as tabs move and must not retrigger source
    // reconciliation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceLinks, selectedSourceUrl, slotLoading])

  useEffect(() => {
    // Same first-wins / clear-on-empty reconciliation as the Changes selection
    // above, and the same hydration guard: an uncached slot has no messages
    // while its history loads, so an early clear would drop a valid selection.
    if (slotLoading) return
    if (issueLinks.length === 0) {
      setSelectedIssueUrl('')
      return
    }
    if (!issueLinks.some(issue => issue.url === selectedIssueUrl)) {
      setSelectedIssueUrl(issueLinks[0].url)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [issueLinks, selectedIssueUrl, slotLoading])

  const addSourceCommentToChat = useCallback((text: string) => {
    setInput(previous => previous.trim() ? `${previous.trimEnd()}\n\n${text}` : text)
  }, [])

  // Auto-track files touched by tool calls (read, write, grep, glob)
  const lastToolLen = useRef(0)
  useEffect(() => {
    // Read the log at effect time rather than subscribing to it: this effect only
    // runs when the length changed, and the append-only log's tail is what it wants.
    const toolLog = store.getState().chat.toolLog
    if (toolLog.length <= lastToolLen.current) { lastToolLen.current = toolLog.length; return }
    const newEntries = toolLog.slice(lastToolLen.current)
    lastToolLen.current = toolLog.length
    for (const e of newEntries) {
      if (e.type !== 'tool' || !e.input) continue
      const name = e.text?.replace(/^🔧\s*/, '') ?? ''
      // Extract paths from tool input JSON preview
      try {
        const inp = e.input
        let paths: string[] = []
        if (/^(read|write)$/i.test(name)) {
          // read: {"operations":[{"path":"/..."}]}  write: {"path":"/..."}
          const pm = inp.match(/"path"\s*:\s*"(\/[^"]+)"/g)
          if (pm) paths = pm.map(m => m.match(/"(\/[^"]+)"$/)?.[1]).filter(Boolean) as string[]
        } else if (/^grep$/i.test(name)) {
          const pm = inp.match(/"path"\s*:\s*"(\/[^"]+)"/)
          if (pm?.[1]) paths = [pm[1]]
        } else if (/^glob$/i.test(name)) {
          const pm = inp.match(/"path"\s*:\s*"(\/[^"]+)"/)
          if (pm?.[1]) paths = [pm[1]]
        }
        for (const p of paths) {
          if (touchedFiles.shouldScanAdd(e.ts)) touchedFiles.addFile(p, 'tool')
        }
      } catch { /* ignore parse errors */ }
    }
    // Keyed on toolLog.length (the log is append-only; lastToolLen slices just
    // the new entries) and on the specific touchedFiles methods used. Depending
    // on the whole `toolLog`/`touchedFiles` objects would reprocess on unrelated
    // identity changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toolLogLen, touchedFiles.addFile, touchedFiles.shouldScanAdd])

  const { colorTheme } = useTheme()
  // Mirror colorTheme into a ref so the `send` callback (which does not depend
  // on colorTheme, to avoid re-creating on every theme switch) can always read
  // the current theme without going stale — otherwise a theme change with no
  // activeSlot change sends the previous theme's color_theme to the backend,
  // mis-injecting the persona.
  const colorThemeRef = useRef(colorTheme)
  useEffect(() => { colorThemeRef.current = colorTheme }, [colorTheme])
  // Read file content via queryClient.fetchQuery so we get React Query's
  // caching/deduplication on repeated opens (re-opening the same file is
  // instant for ~10s) AND proper error semantics (queryFn throws → catch
  // block runs). useMutation was the wrong tool for a read operation.
  // The `ok` flag gates whether the file is recorded in history — 404s and
  // other HTTP failures show a placeholder in the panel but should NOT
  // pollute the history list with files that don't exist on disk.
  const handleFileOpen = useCallback(async (filePath: string, opts?: { replaceId?: string }) => {
    // One editor per path: if this file is already open INLINE in the Files tab,
    // route back to that inline editor (focus the Files view) instead of
    // spawning a competing document tab — two live editors for one on-disk file
    // would have independent dirty buffers and could silently overwrite each
    // other. (Uses a ref so this callback stays identity-stable.)
    if (filePath === inlinePreviewPathRef.current) {
      dispatch(openActivityPanel())
      tabsCtl.setActive('files')
      search.close()
      return
    }
    // Plugin host integration: notify the IntelliJ plugin (if active) so
    // it can open the file natively in the IDE editor. If the plugin
    // handles file opens, skip the dashboard's DiffPanel — the user wanted
    // IDE-native, not in-dashboard.
    try { window.dispatchEvent(new CustomEvent('kirocrew-file-open', { detail: { path: filePath } })) } catch { /* ignore */ }
    if ((window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles) return
    try {
      const [{ text, ok }] = await Promise.all([
        queryClient.fetchQuery({
          queryKey: ['file-read', filePath],
          queryFn: async () => {
            const url = fileReadUrl(filePath)
            const res = await fetch(url)
            const text = res.ok
              ? await res.text()
              : res.status === 404 ? i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
              : i18nT('pages.chatPage.unable_to_read_file')
            return { text, ok: res.ok }
          },
          staleTime: 10_000,
        }),
        queryClient.prefetchQuery({
          queryKey: ['file-diff', filePath],
          queryFn: () => api.fileDiff(filePath),
        }),
      ])
      tabsCtl.openFile(filePath, text, activeSlotRef.current ?? null, opts)
      dispatch(openActivityPanel())
      // The right-hand dock is a single slot; the file viewer is render-gated
      // behind !search.isOpen. Close the find pane so the opened file actually
      // shows instead of being silently suppressed.
      search.close()
      if (ok) touchedFiles.addFile(filePath, 'history')
    } catch {
      tabsCtl.openFile(filePath, i18nT('pages.chatPage.error_reading_file'), activeSlotRef.current ?? null, opts)
      dispatch(openActivityPanel())
      search.close()
    }
    // Depend on the stable members, not the whole hook objects:
    //   search.close      — useCallback([]) in useMessageSearch; the `search`
    //                       object changes identity on every search-state change
    //                       (isOpen/term/matches).
    //   touchedFiles.addFile — useTouchedFiles memoizes on `files`, so its object
    //                       changes identity every time a file lands, including
    //                       mid-run when the tool-log scan above calls addFile.
    // Either whole-object dep churned this callback and the onFileOpen prop on
    // every row. (tabsCtl still churns on tab changes, but those are user actions,
    // not per-chunk.)
  }, [queryClient, tabsCtl, dispatch, search.close, touchedFiles.addFile])

  // Open an artifact as a side-panel tab — the artifact twin of
  // handleFileOpen, and the single entry point every in-chat artifact
  // affordance routes through (the Artifacts tab's rows and `/artifacts/<slug>`
  // links inside messages). Before this existed those surfaces hard-navigated
  // to the standalone detail page, which tore down the chat to show a document
  // the panel can render inline and made artifacts the only panel-capable
  // content that could not be flipped between like files.
  const handleArtifactOpen = useCallback(async (slug: string) => {
    if (!slug) return
    const slot = activeSlotRef.current ?? null
    // Opening an artifact is an act of session involvement: record the
    // `referenced` breadcrumb so a merely-read (or merely-linked) artifact
    // joins "This session" instead of sitting in the library section forever.
    // Deliberately fire-and-forget and deliberately NOT awaited — the panel
    // must open at click speed, and the store already enforces
    // one-breadcrumb-per-session so a double click cannot spam the event log.
    // The 403 an incognito slot returns is expected, not an error to surface.
    if (slot) {
      api.recordArtifactReference(slug, slot)
        .then(() => {
          // Re-run the involvement scan so the row moves sections live.
          queryClient.invalidateQueries({ queryKey: ['session-artifact-records', slot] })
        })
        .catch(() => { /* best-effort breadcrumb */ })
    }
    // Seed the tab from the artifact list cache when it is already warm so the
    // body paints immediately; ArtifactPanel's own query is authoritative and
    // overrides kind/content once it resolves, so a miss here costs a spinner,
    // not correctness.
    let kind: Artifact['kind'] = 'markdown'
    let content = ''
    try {
      const art = await queryClient.fetchQuery<Artifact>({
        queryKey: ['artifact', slug],
        queryFn: () => api.artifact(slug),
        staleTime: 10_000,
      })
      kind = art.kind
      content = art.content ?? ''
    } catch { /* fall through — the panel's own query renders the error state */ }
    tabsCtl.openArtifact({ slug, kind }, content, slot)
    dispatch(openActivityPanel())
    // Same single-slot constraint as handleFileOpen: the right-hand dock is
    // render-gated behind !search.isOpen, so an open find pane would silently
    // swallow the tab we just focused.
    search.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient, tabsCtl, dispatch, search.close])

  // Open the Monaco diff panel from a file-change chip click. Closes the
  // markdown viewer and the activity panel so panels stay mutually exclusive.
  const handleOpenDiff = useCallback((filePath: string, modified: string, original: string) => {
    // If the IntelliJ plugin's file bridge is active, dispatch the event
    // with before/after content so the plugin can show a native IntelliJ
    // diff viewer (with syntax highlighting). Skip the dashboard's
    // own DiffPanel in that case — the plugin sets the flag on page load.
    try {
      window.dispatchEvent(new CustomEvent('kirocrew-file-open', {
        detail: { path: filePath, before: original, after: modified },
      }))
    } catch { /* ignore */ }
    if ((window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles) return
    // Brand-new file (no prior content): a diff would render as one big green
    // all-additions block, which hurts readability. Open the normal readable
    // file view instead — there's no meaningful "before" to compare against.
    // Identical content (no-op): the diff editor shows two identical panes with
    // zero signal — fall through to the readable file view as well.
    if (!original || !original.trim() || original === modified) { handleFileOpen(filePath); return }
    tabsCtl.openDiff(filePath, modified, original)
    dispatch(openActivityPanel())
    // Diff pane is render-gated behind !search.isOpen (single right-dock slot);
    // close the find pane so the diff shows instead of opening underneath it.
    search.close()
    // Depend on the stable `search.close`, not the whole `search` object (see
    // handleFileOpen above) — avoids recreating this callback on search-state
    // changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabsCtl, dispatch, search.close, handleFileOpen])

  // Auto-surface files modified by the agent (carried in m.meta.file_changes)
  // into the activity Files tab so the user sees a unified list. Skip files
  // referenced by messages older than the last 'tool' watermark — once the
  // user clears suggested files, those entries stay gone unless the agent
  // touches them again in a newer turn.
  useEffect(() => {
    for (const m of messages) {
      const ts = typeof m.ts === 'string' ? Date.parse(m.ts) : (m.ts as unknown as number) || 0
      if (!touchedFiles.shouldScanAdd(ts)) continue
      const fc = (m.meta as Record<string, unknown> | undefined)?.file_changes as Array<{ path: string }> | undefined
      if (fc) for (const f of fc) touchedFiles.addFile(f.path, 'tool')
    }
  }, [messages.length]) // eslint-disable-line react-hooks/exhaustive-deps

  const { data: forkCfg } = useQuery<{ tail_fork_enabled?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000 })
  const handleFork = useCallback(async (visibleIndex: number) => {
    if (!activeSlot) return
    try {
      // Fork WITHOUT a prompt: an unsent composer draft must never be
      // auto-submitted into the freshly forked session. The
      // per-slot draft mechanism saves the source slot's composer text on
      // slot-switch, so the user's parked draft stays safe in the original
      // session and the fork opens with an empty composer.
      //
      // B3 cold-cache fix (D2): forkCfg is undefined until the dashboardConfig
      // query resolves for the first time. Use the cache when warm; otherwise
      // fetch a fresh value directly so direction never silently falls back
      // to an undefined config (which previously downgraded an intended
      // tail-fork to a head-fork whenever the query had errored/settled with
      // no data, not just while it was loading).
      const resolvedCfg = forkCfg ?? await api.dashboardConfig()
      const direction = resolvedCfg?.tail_fork_enabled ? 'tail' : 'head'
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, direction })).unwrap()
      if (result.ok) {
        await dispatch(switchSlot(result.key))
      } else {
        alert(i18nT('pages.chatPage.fork_failed_error', { error: result.error || i18nT('pages.chatPage.unknown_error') }))
      }
    } catch (e) {
      alert(i18nT('pages.chatPage.fork_failed_error', { error: e instanceof Error ? e.message : String(e) }))
    }
  }, [activeSlot, dispatch, forkCfg])

  const handlePlanFromHere = useCallback(async (visibleIndex: number) => {
    if (!activeSlot) return
    try {
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, mode: 'orchestrator' })).unwrap()
      if (result.ok) {
        await dispatch(switchSlot(result.key))
        // Unified view: the forked orchestrator slot lives in the same sidebar.
        if (!mode) navigate('/chat')
      } else {
        alert(i18nT('pages.chatPage.plan_from_here_failed_error', { error: result.error || i18nT('pages.chatPage.unknown_error') }))
      }
    } catch (e) {
      alert(i18nT('pages.chatPage.plan_from_here_failed_error', { error: e instanceof Error ? e.message : String(e) }))
    }
  }, [activeSlot, dispatch, mode, navigate])

  const handleFileSave = useCallback(async (filePath: string, content: string) => {
    // Capture the slot BEFORE awaiting: if the user switches chats mid-save, the
    // draft we reconcile must be the one that owned this save, not whatever slot
    // is active when the write resolves.
    const requestSlot = activeSlotRef.current ?? ''
    const res = await fetch('/api/file-write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath, content }),
    })
    if (!res.ok) throw new Error(`Save failed: ${res.status}`)
    // Reconcile the inline-preview draft for the SAVING slot (drafts are
    // slot+path keyed). Clear it ONLY if it still equals what we just saved —
    // if the user typed more while the write was in flight, the draft now holds
    // newer content and must be preserved, not dropped.
    if (getInlineDraft(requestSlot, filePath) === content) clearInlineDraft(requestSlot, filePath)
  }, [])

  const takeScreenshot = useCallback(async () => {
    // Capture the slot at click-time. If the user switches away before the
    // screenshot promise resolves, we must land the file in the slot the user
    // was looking at when they clicked — not whatever slot is now active.
    const requestSlot = activeSlotRef.current
    setUploading(true)
    try {
      const { path } = await api.screenshot()
      if (path) {
        if (activeSlotRef.current === requestSlot) {
          setPendingFiles(prev => [...prev, path])
        } else if (requestSlot) {
          // Slot changed during the await — divert the file into the request
          // slot's persisted draft so it's waiting when the user goes back.
          const cur = fileDrafts.current[requestSlot] ?? []
          setFileDraft(fileDrafts.current, requestSlot, [...cur, path])
          saveDrafts()
        }
      }
    } catch { /* user cancelled */ }
    setUploading(false)
  }, [saveDrafts])

  /** Screen capture entry: cross-platform snip+crop when supported, else native macOS screenshot. */
  const handleCapture = useCallback(async () => {
    snipSlotRef.current = activeSlotRef.current
    if (!screenSnipSupported) { takeScreenshot(); return }
    const canvas = await captureScreen()
    if (canvas) setSnipFrame(canvas)
  }, [takeScreenshot])

  // The Web Preview tab's crop button asks for an area screenshot via a window
  // event. Same crop→attach pipeline as the composer button, but capture pre-
  // targets THIS tab (preferCurrentTab) so the browser prompt is a single
  // "Share this tab?" confirm instead of the full source picker. (Desktop app:
  // no prompt either way via setDisplayMediaRequestHandler.)
  useEffect(() => {
    const onSnip = async () => {
      snipSlotRef.current = activeSlotRef.current
      if (!screenSnipSupported) { takeScreenshot(); return }
      const canvas = await captureScreen(currentTabCaptureDeps())
      if (canvas) setSnipFrame(canvas)
    }
    window.addEventListener(PREVIEW_SNIP_EVENT, onSnip)
    return () => window.removeEventListener(PREVIEW_SNIP_EVENT, onSnip)
  }, [takeScreenshot])

  /** Upload files via browser File API (cross-platform) */
  const uploadFiles = useCallback(async (files: File[], targetSlot?: string | null) => {
    if (!files.length) return
    // Same slot-capture pattern as takeScreenshot — see note there. An explicit
    // targetSlot (e.g. the slot that initiated a snip) overrides the live slot
    // so an async capture lands where it started, not where the user switched to.
    const requestSlot = targetSlot !== undefined ? targetSlot : activeSlotRef.current
    setUploadError('')
    if (files.length > 20) { setUploadError(i18nT('pages.chatPage.too_many_files_max_20')); return }
    const big = files.find(f => f.size > 50 * 1024 * 1024)
    if (big) { setUploadError(`File too large: ${big.name} (max 50 MB)`); return }
    setUploading(true)
    try {
      const res = await api.uploadFiles(files)
      if (res.error) {
        setUploadError(i18nT('pages.chatPage.upload_failed_error', { error: res.error }))
      } else if (res.paths?.length) {
        const landing = fileLandingSlot(requestSlot, activeSlotRef.current)
        if (landing.target === 'pending') {
          setPendingFiles(prev => [...prev, ...res.paths])
        } else if (landing.target === 'draft') {
          const cur = fileDrafts.current[landing.slot] ?? []
          setFileDraft(fileDrafts.current, landing.slot, [...cur, ...res.paths])
          saveDrafts()
        }
      }
      if (!res.error && res.resizedByPath && Object.keys(res.resizedByPath).length) {
        setResizedInfo(prev => ({ ...prev, ...res.resizedByPath }))
      }
    } catch { setUploadError(i18nT('pages.chatPage.upload_failed_check_file_type_and_size_max_50_mb')) }
    setUploading(false)
  }, [saveDrafts])

  // Deliver an optimize result to the session that started it when the user
  // navigated away before the request settled. ChatInput only calls this for
  // the cross-session case (it writes the result itself when the originating
  // session is still on screen). Same slot-capture pattern as uploadFiles /
  // the send-failure draft restore: persist into the originating slot's draft
  // unconditionally (recoverable on disk + shown when the user returns), and
  // only splice into the live input when that slot is what's currently on
  // screen — compared against activeSlotRef.current, never the stale closure.
  const handleOptimizeResult = useCallback((slot: string | null, optimized: string) => {
    if (!slot) return
    setDraft(drafts.current, slot, optimized)
    saveDrafts()
    if (slot === activeSlotRef.current) setInput(optimized)
  }, [saveDrafts])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation(); setDragOver(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length) {
      uploadFiles(files)
    }
  }, [uploadFiles])

  // Scroll to bottom helper — delegates to the virtualizer (single controller).
  const scrollBottom = useCallback((instant: boolean = false) => {
    vScrollToBottomRef.current(instant ? 'auto' : 'smooth')
  }, [])

  // Scroll compensation for the in-flow tip (Raymond 2026-07-21): mounting the
  // tip shrinks the scroll viewport but does not itself re-anchor the scroll
  // position, so when the user is parked at the bottom of a streaming turn the
  // last line of text gets clipped under the container edge -- visually
  // indistinguishable from the tip covering it. Re-anchor on mount AND on
  // dismiss (double rAF: let the band's layout commit before measuring).
  useEffect(() => {
    if (!isAtBottomRef.current) return
    const raf = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (isAtBottomRef.current) scrollBottom(true)
      })
    })
    return () => cancelAnimationFrame(raf)
  }, [activeTip, scrollBottom])

  // Navigate to a (possibly off-window) display index: mount it first via the
  // virtualizer so the DOM-based scroll can find it, then scroll next frame.
  // Tracks the in-flight row-mount poll (below) so a newer navigation cancels
  // the previous one. Without this, an earlier far-jump loop whose target
  // finally mounts would scroll to that stale destination, yanking away from
  // the newer target (rapid stepping / click-then-click). cancelAnimationFrame(0)
  // is a no-op, so 0 is a safe initial value.
  const navScrollRafRef = useRef(0)
  // Cancel handle for the in-flight settle poll, so a newer navigation or an
  // unmount terminates it rather than letting it run to the wall-clock backstop.
  const navPollCancelRef = useRef<(() => void) | null>(null)
  const navToDisplayIndex = useCallback((
    idx: number,
    opts?: { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number },
  ) => {
    cancelAnimationFrame(navScrollRafRef.current)
    // Signal WidgetFrames that a jump is starting so the span of widgets
    // mountIndex is about to union doesn't all build their iframes in one
    // frame (see PROGRAMMATIC_BUILD_DELAY_MS in WidgetFrame).
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    const jumpedFar = mountIndexRef.current(idx)
    // A FAR jump replaces the window, so the rows between the old viewport and
    // the target are NOT mounted — a smooth glide would scrub the scroller
    // through blank spacer (the "occasional flicker" on the ↑/jump pills when
    // the target is past a long turn). Teleport instantly instead: the target
    // block is already mounted so it shows immediately, and overflow-anchor
    // keeps it stable as its rows measure. NEAR jumps keep their smooth glide
    // (mountIndex unioned the whole path, so there's nothing blank to scrub).
    const behavior: ScrollBehavior = jumpedFar ? 'auto' : (opts?.behavior ?? 'smooth')
    // mountIndex queues a React state update (the virtualizer's window range).
    // A FAR jump REPLACES the window, so the target row is NOT painted into the
    // DOM within a single frame — one rAF then a DOM query misses it. Poll for
    // the row and scroll once it mounts, then keep re-scrolling (re-reading the
    // live offset each frame) until the row's measured height SETTLES — a far
    // row must mount + measure, and a widget target keeps growing for ~450ms as
    // its iframe builds (PROGRAMMATIC_BUILD_DELAY_MS). The OLD frame-count
    // ceiling (~0.5s) gave up before the widget settled, so the jump silently
    // no-op'd and only worked on a second click once cached. Condition-based
    // instead: retry until the target reports a stable (non-estimated) height,
    // with a ~2s wall-clock backstop so a genuinely unreachable target still
    // terminates instead of spinning. While the row is missing we do NOTHING —
    // we never teleport to top (the "far jump jumps to top, second click works"
    // bug). navScrollRafRef holds the in-flight frame so a newer navigation
    // cancels this loop (rapid stepping / click-then-click).
    const rowEl = (): HTMLElement | null =>
      (scrollerRef.current?.querySelector(`[data-display-index="${idx}"]`) as HTMLElement | null) ?? null
    navPollCancelRef.current?.()
    // The poll re-scrolls every frame for up to CONVERGE_MAX_MS (~2s). If the
    // user tries to scroll during that window, continuing to step would drag
    // the viewport back to the target and fight their input — so user scroll
    // ABORTS the convergence, exactly as scrollCurrentMatchIntoView does. (The
    // retired frame-count ceiling was short enough (~0.5s) to mask this; the
    // longer, condition-based window makes it reachable.) The shared
    // attachUserScrollIntent covers scrollbar drag and keyboard scrolling too,
    // not just wheel/touch.
    const scrollEl = scrollerRef.current
    const onUserScroll = () => { navPollCancelRef.current?.() }
    const detachUserScroll = attachUserScrollIntent(scrollEl ?? undefined, onUserScroll)
    navPollCancelRef.current = pollRowSettled({
      measure: () => {
        const el = rowEl()
        return el ? el.getBoundingClientRect().height : null
      },
      // Only the FIRST step may glide — see glideOnceStep. Re-issuing a smooth
      // scroll cancels and restarts the animation, so stepping every frame
      // through the quiet window would leave a NEAR jump stuttering until the
      // poll ends (the same restart trap fix #4 removed from the streaming pin).
      step: glideOnceStep(
        (b) => { scrollToDisplayIndex(idx, { ...opts, behavior: b }) },
        behavior,
      ),
      raf: (cb) => (navScrollRafRef.current = requestAnimationFrame(cb)),
      now: () =>
        typeof performance !== 'undefined' && typeof performance.now === 'function'
          ? performance.now()
          : Date.now(),
      onEnd: () => { detachUserScroll(); navPollCancelRef.current = null },
    })
  }, [scrollToDisplayIndex, scrollerRef])

  // Stop any in-flight settle poll on unmount. Without this the loop keeps
  // ticking rAFs against a null scroller until the ~2s backstop (harmless but
  // pointless work after the page is gone).
  useEffect(() => () => {
    navPollCancelRef.current?.()
    navPollCancelRef.current = null
    cancelAnimationFrame(navScrollRafRef.current)
  }, [])

  const displayItemsRef = useRef<DisplayItem[]>([])
  // Pinned-prompt banner. `pinFoldRef` is a zero-height sentinel sitting
  // directly under the title row: its top edge is the fold line the banner
  // sticks to, and it is always mounted so the fold stays measurable even when
  // nothing is pinned yet. `pinCardRef` is measured for the push geometry.
  const pinFoldRef = useRef<HTMLDivElement | null>(null)
  const pinCardRef = useRef<HTMLDivElement | null>(null)
  const pinEnabledRef = useRef(true)
  const [pinned, setPinned] = useState<{ idx: number; text: string; full: string; push: number; bannerH: number } | null>(null)
  const [pinExpanded, setPinExpanded] = useState(false)
  // Recompute which prompt is pinned, and how far the incoming prompt has
  // pushed it out, from the current scroll position.
  const updatePinnedPrompt = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    // Measure with getBoundingClientRect (viewport-relative) so the origin
    // matches the scroller regardless of which ancestor is the items'
    // offsetParent — consistent with useScrollManager, which also deliberately
    // avoids offsetTop. The fold sits BELOW the scroller's top edge (under the
    // title row), which is what the sentinel gives us.
    const items = el.querySelectorAll('[data-display-index]')
    const foldY = pinFoldRef.current?.getBoundingClientRect().top
      ?? el.getBoundingClientRect().top
    // First row whose bottom is still below the fold = the topmost row not yet
    // fully scrolled past it.
    let foldIdx = -1
    let foldRowTop = 0
    for (const item of items) {
      const htmlItem = item as HTMLElement
      const rect = htmlItem.getBoundingClientRect()
      if (rect.bottom > foldY) {
        foldIdx = parseInt(htmlItem.getAttribute('data-display-index') || '0', 10)
        foldRowTop = rect.top
        break
      }
    }

    if (!pinEnabledRef.current || foldIdx < 0) { setPinned(null); return }
    const list = displayItemsRef.current
    const pinIdx = findPinnedPromptIdx(list, foldIdx, foldRowTop < foldY)
    const pinItem = pinIdx >= 0 ? list[pinIdx] : undefined
    if (!pinItem || pinItem.kind !== 'single') { setPinned(null); return }
    // The incoming prompt pushes the banner out; when its row is not mounted it
    // is still far below the fold, so there is nothing to push against yet.
    const nextIdx = findNextPromptIdx(list, pinIdx)
    const nextEl = nextIdx >= 0
      ? el.querySelector(`[data-display-index="${nextIdx}"]`) as HTMLElement | null
      : null
    const nextTop = nextEl ? nextEl.getBoundingClientRect().top : null
    const bannerH = pinCardRef.current?.getBoundingClientRect().height ?? 0
    const push = computePinPush(bannerH, foldY, nextTop)
    const full = pinItem.msg.content
    const text = promptPreview(full)
    setPinned(prev => (prev && prev.idx === pinIdx && prev.push === push
      && prev.text === text && prev.bannerH === bannerH)
      ? prev
      : { idx: pinIdx, text, full, push, bannerH })
  }, [scrollerRef])
  // rAF-throttle the per-scroll recompute: updatePinnedPrompt does a
  // querySelectorAll + getBoundingClientRect loop (a forced layout read), and a
  // fling fires scroll dozens of times/sec. Coalesce to at most once per frame,
  // mirroring the virtualizer's own scroll-listener throttle so this handler
  // doesn't reintroduce scroll-time main-thread cost.
  const pinRafRef = useRef(false)
  const onScrollPin = useCallback(() => {
    if (pinRafRef.current) return
    pinRafRef.current = true
    requestAnimationFrame(() => {
      pinRafRef.current = false
      updatePinnedPrompt()
    })
  }, [updatePinnedPrompt])
  /** Jump the transcript back to the pinned prompt, landing it just below the
   *  banner so the prompt is read in context — which also un-pins the banner,
   *  since its prompt is no longer above the fold. */
  const scrollToPinnedPrompt = useCallback((target: number) => {
    // Signal WidgetFrames that a programmatic jump is starting so any widget
    // the smooth scroll sweeps PAST defers building its (expensive) Tailwind
    // iframe until the glide settles (see PROGRAMMATIC_BUILD_DELAY_MS in
    // WidgetFrame). Without this, the native smooth scroll crosses the span
    // fast enough to mount+build several widget iframes synchronously mid-glide
    // — a 100ms+ 'message' handler stall. navToDisplayIndex already emits this
    // for its mountIndex path; the smooth path had dropped it.
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    // Clear the header chrome the row would otherwise land behind: the fold
    // inset plus the banner's own height plus a small gap. Measured rather
    // than hardcoded so it tracks a wrapped title row or a taller banner.
    const el = scrollerRef.current
    const foldTop = pinFoldRef.current?.getBoundingClientRect().top
    const srTop = el?.getBoundingClientRect().top
    const bannerH = pinCardRef.current?.getBoundingClientRect().height ?? 0
    const chrome = (foldTop != null && srTop != null) ? (foldTop - srTop) + bannerH + 8 : 72
    // Human-like smooth scroll (no wide window pre-mount) — see
    // scrollToIndexSmooth. Avoids leaving a broad span of animated widgets
    // mounted+oscillating after the jump.
    scrollToIndexSmoothRef.current(target, { align: 'start', offset: -chrome })
  }, [scrollerRef])

  // Sticky-bottom scroll state is owned by the virtualizer (`virt.isAtBottom`,
  // wired below). No local mirror — a single source of truth avoids the
  // dual-controller drift that caused the follow/yank regressions.

  // New content while following is handled inside the virtualizer (RO re-pin
  // for in-place growth + append layout-effect pin for new items), so ChatPage
  // no longer runs its own message-length scroll effect.
  useEffect(() => { dispatch(fetchHistory(false)) }, [dispatch])
  // Persist active slot to localStorage for refresh recovery (per-mode)
  const slotStorageKey = `mc-active-slot-${mode || 'chat'}`
  const slotStorageKeyRef = useRef(slotStorageKey); slotStorageKeyRef.current = slotStorageKey
  useEffect(() => {
    if (activeSlot && filteredSlots.some(s => s.key === activeSlot)) {
      safeSetItem(slotStorageKey, activeSlot)
    }
  }, [activeSlot, slotStorageKey, filteredSlots])
  useEffect(() => () => { if (activeSlotRef.current && filteredSlotsRef.current.find(s => s.key === activeSlotRef.current)) safeSetItem(slotStorageKeyRef.current, activeSlotRef.current) }, [])
  // Handle ?sid= (or legacy ?slot=) query parameter — activate the given session
  // Capture initial ?sid= at mount time before any effect can overwrite it
  // noUrlSync also disables the sid-READ paths, not just the URL write. The host
  // route (e.g. /artifacts/:slug) is not required to be sid-free: land on
  // /artifacts/foo?sid=other and an ungated read effect would switchSlot() the
  // embedded panel onto an unrelated session, so the composer would send into
  // it. Zeroing the ref here neutralizes the mount-activation effect AND the 5s
  // "session not found" timeout that keys off it; the POP effect reads
  // searchParams live and is gated separately below.
  const initialSidRef = useRef(noUrlSync ? null : (searchParams.get('sid') || searchParams.get('slot')))
  const initialMsgRef = useRef(searchParams.get('msg'))
  const initialNewRef = useRef(searchParams.get('new') === '1')
  // Deep-link mount activation in progress — stops the sync effect from stripping
  // ?sid before activation lands. Cleared once activeSlot is truthy.
  const pendingSidRef = useRef(!!initialSidRef.current)
  // Back/Forward (POP) in flight — set ONLY by the POP effect. Kept separate from
  // pendingSidRef so a deep-link load doesn't trip the POP bail and freeze the
  // first sidebar switch. (Mesh chat-switch bug.)
  const popInFlightRef = useRef(false)
  // react-router reports the initial render as navigationType 'POP'. That first
  // run is the deep-link load (owned by initialSidRef), not a real Back/Forward —
  // skip it so the POP effect doesn't wrongly arm popInFlightRef on mount.
  const popReadyRef = useRef(false)
  // Last history entry key honored by the POP effect — distinguishes a genuine
  // Back/Forward (new location.key) from a re-render where navigationType is
  // still stuck at 'POP'.
  const lastLocKeyRef = useRef<string | null>(null)
  const [sidError, setSidError] = useState('')
  const [highlightTs, setHighlightTs] = useState<string | null>(null)
  // Embed ?new=1: create a new chat slot and navigate to it
  const embedNewSlotMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ mode })).unwrap(),
    onSuccess: (slot) => {
      if (slot?.key) navigate(`/embed/chat/${slot.key}`, { replace: true })
    },
  })
  useEffect(() => {
    if (!initialNewRef.current || !embedMode) return
    initialNewRef.current = false
    embedNewSlotMutation.mutate()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // On mount, URL ?sid= drives which session is active (URL wins over localStorage)
  useEffect(() => {
    if (embedded && !embedMode) return
    if (!connected) return  // offline: defer URL-driven switchSlot until reconnect
    const urlSlot = initialSidRef.current
    if (!urlSlot || filteredSlots.length === 0) return
    // The deep-link ?sid only sets the INITIAL active slot. The slot list can
    // populate AFTER the user has already clicked a different session in the
    // sidebar (switchSlot.pending sets activeSlot synchronously); without this
    // guard the delayed activation would override that click and snap the UI
    // back to the deep-linked session (Mesh chat-switch-after-deeplink bug).
    if (activeSlot) { initialSidRef.current = null; return }
    if (filteredSlots.some(s => s.key === urlSlot)) {
      initialSidRef.current = null
      dispatch(switchSlot(urlSlot))
    }
    // Don't error immediately — slot may arrive via SSE shortly
    // embedded/embedMode are read in the guard above; they are stable for the
    // session, so listing them satisfies the linter without changing behavior.
  }, [filteredSlots, activeSlot, dispatch, connected, embedded, embedMode])
  // React to ?sid= changes AFTER mount — required for plugin tab switching
  // where the URL is updated via react-router navigate() (soft nav). The
  // mount-only initialSidRef approach above misses these updates because
  // the component doesn't remount across soft navs. Without this effect
  // the "activeSlot → URL" sync below would rewrite the URL back to the
  // current activeSlot instead of switching to the slot the URL is asking
  // for.
  //
  // Embed mode: react to ANY ?sid change (the host app drives the URL).
  // Main dashboard: react ONLY to a genuine Back/Forward (navigationType POP).
  // Our own activeSlot→URL writes are PUSH/REPLACE, so they never re-enter here
  // — that is what avoids the activeSlot↔URL ping-pong the old embed-only gate
  // guarded against. A session switch pushes a ?sid history entry (sync effect
  // below), so native browser/Electron Back/Forward (and Alt+←/→) retrace the
  // sessions you've visited.
  //
  // Also gated on `connected`: when offline the switchSlot dispatch fails
  // (fetchSlotDetail rejects) and clears messages, leaving an activeSlot
  // with empty messages — the WelcomeView fallback then renders. Defer
  // the switch until reconnect so cached state stays put.
  useEffect(() => {
    // noUrlSync: the host page owns the URL and the panel's session is chosen by
    // the host, never by a query param. This effect otherwise treats embedMode as
    // "the host drives ?sid" and would switch the panel onto whatever session the
    // host route happens to carry.
    if (noUrlSync) return
    // Embed: host app drives the URL — react to any ?sid change.
    // Main dashboard: honor only a genuine Back/Forward POP. react-router reports
    // the initial render as 'POP' and stays 'POP' until our own switch navigates
    // (PUSH/REPLACE); a real Back/Forward is a POP that follows one of those. So
    // arm on the first non-POP nav and only honor POP once armed — this ignores
    // the mount POP (deep-link load, owned by initialSidRef) so it can't wrongly
    // arm popInFlightRef and freeze the next switch.
    if (!embedMode) {
      if (navigationType !== 'POP') { popReadyRef.current = true; lastLocKeyRef.current = location.key; return }
      if (!popReadyRef.current) return
      // navigationType stays 'POP' after a Back/Forward until our own navigate()
      // runs. Without this guard the effect re-fires on every activeSlot change
      // (a sidebar click) while still 'POP', reads the stale URL sid, and reverts
      // the click — locking the URL to one chat. location.key changes only on a
      // genuine history navigation, so honor a POP exactly once per new entry.
      if (location.key === lastLocKeyRef.current) return
      lastLocKeyRef.current = location.key
    }
    if (!connected) return
    const urlSid = searchParams.get('sid') || searchParams.get('slot')
    if (!urlSid || urlSid === activeSlot) return
    if (filteredSlots.some(s => s.key === urlSid)) {
      popInFlightRef.current = true
      dispatch(switchSlot(urlSid))
    }
  }, [searchParams, filteredSlots, activeSlot, dispatch, embedMode, navigationType, location.key, connected, noUrlSync])
  // Timeout: if slot never appears after 5s, show error.
  // Gated on `connected` so the timer only runs while the gateway is reachable
  // — otherwise an offline tab would burn its 5s while the resolve effects
  // above are deferred, fire a false "Session not found", clear initialSidRef,
  // and the resolve never happens once the gateway comes back. Re-runs the
  // effect when connected flips so the timer starts fresh on reconnect.
  useEffect(() => {
    if (!connected) return
    const urlSlot = initialSidRef.current
    if (!urlSlot) return
    const timer = setTimeout(() => {
      if (initialSidRef.current) {
        initialSidRef.current = null
        pendingSidRef.current = false
        popInFlightRef.current = false
        setSidError(`Session "${urlSlot}" not found`)
      }
    }, 5000)
    return () => clearTimeout(timer)
  }, [connected])
  // Sync activeSlot → ?sid= in URL (persistent deep-link)
  // Skip entirely when embedded — URL belongs to the host app
  const basePath = popout ? '/popout/chat' : embedMode === 'chat' || embedMode === 'sessions' ? '/embed/chat' : '/chat'
  const searchParamsRef = useRef(searchParams)
  searchParamsRef.current = searchParams
  useEffect(() => {
    if (embedded && !embedMode) return
    // noUrlSync (artifact companion chat panel): the host page owns the URL
    // entirely (e.g. /artifacts/:slug) and passes embedMode="chat" only for its
    // single-session chrome (no sessions sidebar). Never write ?sid= or
    // navigate to basePath — an in-place navigate would swap the host route out
    // from under the panel. The sid-READ paths are gated for the same flag
    // above (initialSidRef + the post-mount POP effect); do not assume a
    // noUrlSync host route is sid-free.
    if (noUrlSync) return
    // In sessions embed mode, the URL is `/embed/sessions` regardless of
    // activeSlot. Navigation away from sessions is driven by the explicit
    // onSelectSlot callback in ChatSidebar — never auto-navigate from here,
    // since activeSlot may change due to background state (initial load,
    // localStorage hydration, WS updates) which would unwantedly bounce
    // the user back into chat view.
    if (embedMode === 'sessions') return
    const sp = searchParamsRef.current
    // Back/Forward (POP) activation in flight: the browser already set the URL to
    // the target session and activeSlot is catching up via the switchSlot the
    // ?sid→activeSlot effect above just dispatched. Writing the URL here would run
    // with a STALE activeSlot (the slot we're leaving) and push a spurious history
    // entry for it — corrupting multi-step Back/Forward. Bail until activeSlot
    // matches the URL, then fall through for replace-only slug normalization (a POP
    // must never produce a push).
    if (popInFlightRef.current) {
      if (!activeSlot || activeSlot !== sp.get('sid')) return
      popInFlightRef.current = false
    }
    if (!activeSlot) {
      if (sp.has('sid') && !initialSidRef.current && !pendingSidRef.current) {
        navigate(basePath, { replace: true })
      }
      return
    }
    pendingSidRef.current = false
    const current = sp.get('sid')
    const slot = filteredSlots.find(s => s.key === activeSlot)
    const slug = slot?.title && slot.title !== slot.key ? toSlug(slot.title) : ''
    const expectedPath = `${basePath}${slug ? '/' + slug : ''}`
    if (current === activeSlot && location.pathname === expectedPath) return
    const next = new URLSearchParams(sp)
    next.set('sid', activeSlot)
    next.delete('slot')
    next.delete('prefill')
    next.delete('autoSend')
    next.delete('newSession')
    next.delete('msg')
    // Push a new history entry on a real session switch (already viewing a
    // different session) so native Back/Forward retraces sessions; replace for
    // the initial activation (no prior sid) and same-session path normalization.
    const isSessionSwitch = !!current && current !== activeSlot
    navigate(`${basePath}${slug ? '/' + slug : ''}?${next}`, { replace: !isSessionSwitch })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, filteredSlots, navigate, basePath, location.pathname, embedded, noUrlSync])
  // Re-fetch slot messages on mount (handles nav away + back).
  // Skip when newSession=1 — createSlot in send() will set the active slot;
  // dispatching switchSlot here would race and overwrite it.
  useEffect(() => { if (activeSlot && !newSessionRef.current && filteredSlotsRef.current.find(s => s.key === activeSlot)) dispatch(switchSlot(activeSlot)) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // Clear activeSlot when it belongs to a different mode (page switch)
  useEffect(() => {
    if (activeSlot && slots.length > 0 && !filteredSlots.find(s => s.key === activeSlot)) {
      dispatch(setActiveSlot(null))
    }
  }, [activeSlot, slots.length, filteredSlots, dispatch])
  // Auto-select slot after refresh — restore from localStorage or pick first
  // If no slots exist at all, auto-create one so the user lands in a ready chat
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  const autoCreatedRef = useRef(false)
  useEffect(() => {
    if (activeSlot) return
    // Don't auto-select/auto-create while the challenge-redirect token effect
    // is still creating + slack-linking its session; otherwise we'd switch to
    // a different slot and orphan the linked one (breaking Slack mirroring).
    if (tokenConsumingRef.current) return
    if (searchParams.get('slot') || searchParams.get('sid') || initialSidRef.current) return
    if (filteredSlots.length > 0) {
      const saved = localStorage.getItem(slotStorageKey)
      const target = saved && filteredSlots.find(s => s.key === saved) ? saved : filteredSlots[0].key
      dispatch(switchSlot(target))
    } else if (connected && slotsLoaded && !autoCreatedRef.current) {
      // Connected, slots fetched, and truly empty — auto-create one
      autoCreatedRef.current = true
      dispatch(createSlot({ agent: defaultAgent || undefined, mode }))
    }
  }, [activeSlot, filteredSlots, searchParams, dispatch, slotStorageKey, connected, slotsLoaded, defaultAgent, mode])

  // Slot switch: the virtualizer (keyed on sessionId = activeSlot) force-pins
  // to the true bottom itself in a layout effect. Here we just re-arm the
  // local at-bottom ref used by the gating effects below.
  const prevSlotRef = useRef<string | null>(null)
  useEffect(() => {
    if (activeSlot !== prevSlotRef.current) {
      prevSlotRef.current = activeSlot
      isAtBottomRef.current = true
    }
  }, [activeSlot])

  // Auto-scroll during streaming — only when pinned to bottom
  const lastMsg = messages[messages.length - 1]
  const isStreaming = lastMsg?.role === 'streaming'
  // Follow-up options derived from the last assistant message in the current chat.
  // Swapping chats (activeSlot change) → messages change → memo recomputes fresh.
  const { followUpOptions, followUpIsPlan } = useMemo(
    () => deriveFollowUpOptions(messages, isStreaming),
    [messages, isStreaming],
  )
  // Visual-only highlight state; text in the input is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the active chat switches — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  const followUpOptionsKey = followUpOptions.join('\x00')
  useEffect(() => { setFollowUpPicked(new Set()) }, [followUpOptionsKey, activeSlot])
  const { data: dashCfg } = useQuery<{ quick_send?: boolean; session_grid?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000 })
  // Session grid (split view) is an opt-in feature flag (Settings › Chat › Split View). Gates ⌘D, the Columns2 button, and the grid render.
  const splitFeatureEnabled = dashCfg?.session_grid === true
  // Pop-out state for the title-bar control (shared singleton — same channel the menus use).
  const { isPoppedOut: isSlotPoppedOut, open: openActivePopout, focus: focusActivePopout, returnSelfToMain } = useChatPopouts()
  const activePoppedOut = !!activeSlot && isSlotPoppedOut(activeSlot)
  const planTaskId = useMemo(() => {
    for (const m of messages) {
      const match = m.content?.match(/<!-- plan_task_id:(\S+) -->/)
      if (match) return match[1]
    }
    return ''
  }, [messages])

  // Scroll to show Footer when agent starts running (loading indicator appears)
  const prevRunningRef = useRef(false)
  useEffect(() => {
    if (slotRunning && !prevRunningRef.current && isAtBottomRef.current) {
      setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    }
    prevRunningRef.current = slotRunning
  }, [slotRunning, scrollBottom])

  // Reconcile the active slot's running state from WS slot updates. The reducer
  // guards against a stale snapshot overwriting an unconfirmed local turn.
  useEffect(() => {
    if (!activeSlot) return
    const s = slots.find(s => s.key === activeSlot)
    if (!s) return
    dispatch(syncSlotRunningFromServer({ slot: s.key, running: s.running, stopping: s.stopping ?? false }))
  }, [slots, activeSlot, dispatch])

  const handleResumeSession = useCallback(async (key: string, title: string) => {
    try {
      await dispatch(resumeFromHistory({ key, title })).unwrap()
      if (activeSlot && activeSlot !== key) {
        delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]; prevSlot.current = null; saveDrafts()
        dispatch(deleteSlot(activeSlot)).unwrap().catch(() => {})
      }
    } catch { /* resume failed — keep current slot */ }
  }, [activeSlot, dispatch, saveDrafts])
  // Raw send — sends pre-built text directly to the server
  const modeRef = useRef(mode)
  modeRef.current = mode
  const planActionMutationRef = useRef(planActionMutation)
  planActionMutationRef.current = planActionMutation

  const send = useCallback(async (optionText?: string, targetSlot?: string) => {
    // Defense-in-depth: ChatInput already gates Send/Optimize buttons and
    // the keyboard Enter shortcut on `connected`, but a future caller (a
    // programmatic dispatch from a hotkey, a follow-up option click, an
    // intent handler) could call send() while offline. Bail before we
    // clear the draft via setInput('') below — losing the user's typed
    // message with no recovery path is the offline-UX regression we're
    // guarding against. Cheap belt-and-braces.
    if (!connected) return
    const raw = (optionText || inputRef.current).trim()
 // Capture + clear the widget-origin tag: attribute this
    // turn to a widget only if the composer still carries the exact text a
    // widget action pre-filled. Cleared on every send so it can't go stale.
    const widgetOrigin = !!widgetPrefillRef.current && raw.includes(widgetPrefillRef.current)
    widgetPrefillRef.current = null
    if (!raw && !pendingFilesRef.current.length) return

    // Sending while STREAMING dictation is live ends the dictation. The panel
    // advertises "Enter to send", so this path is reachable by design — and
    // without it, streaming STT keeps running past the send: `onPartial`
    // re-derives the composer value from `frozenInputRef`, which was snapshotted
    // BEFORE the send cleared it, so the next partial repopulates the composer
    // with text the user already sent. Disarm FIRST so any partial/final already
    // in flight is dropped, then stop capture (stop() is async — up to 5s for
    // the backend close).
    //
    // STREAMING ONLY, deliberately. In batch mode the transcription arrives
    // exactly once, from `MediaRecorder.onstop` AFTER capture ends, and it
    // arrives through `onText` — which honours `sttDisarmedRef`. Disarming here
    // would throw away the entire recording, which is the opposite of the bug
    // being fixed. Batch therefore keeps its pre-existing behaviour untouched:
    // capture continues, and the transcript lands when the user stops.
    if (voiceRef.current.recording && sttStreamingRef.current) {
      sttDisarmedRef.current = true
      frozenInputRef.current = null
      voiceRef.current.toggle()
    }

    // The session actually on screen at send time. Read from the ref (fresh
    // every render), not the closure `activeSlot` (stale until send() is
    // re-memoized). Under lag a reducer-driven activeSlot change can move the
    // active slot before ChatPage re-renders, so the closure would route into
    // the slot the user just left. Used for slash routing, the composer draft
    // clear, and (below) the send target.
    const uiSlot = activeSlotRef.current

    // Slash command interception (e.g. /side): runs before knowledge so a
    // bare prefix like /side returns immediately without touching input parse.
    const slashResult = await interceptSlashCommand(raw, uiSlot, dispatch)
    if (slashResult.intercepted) {
      if (!optionText) { setInput(''); setPasteBlocks([]) }
      return
    }

    // Knowledge fetch: intercept @knowledge prefix, show picker instead of sending
    const kq = extractKnowledgeQuery(raw)
    if (kq && !optionText) {
      knowledgeFetchRef.current.searchKnowledge(kq)
      setInput('')
      return
    }

    // Snapshot the staged attachments BEFORE the composer is cleared below, so a
    // failed send can put them back (prepareSendPayload's `filePaths` drops
    // images, which would silently lose them on restore).
    const sentFiles = pendingFilesRef.current.slice()
    const { txt, displayTxt, filePaths } = prepareSendPayload(raw, pendingFilesRef.current)
    // Expand paste tokens for the LLM; UI-facing displayTxt keeps the tokens
    // intact so the user bubble can render them as clickable chips.
    const activePastes = pasteBlocksRef.current
    let llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
    const browsing = browseModeRef.current
    // Prepend knowledge context if pending
    let knowledgeBlock: import('./chat/useKnowledgeFetch').KnowledgeBlock | null = null
    if (knowledgeFetchRef.current.pendingKnowledge) {
      knowledgeBlock = knowledgeFetchRef.current.pendingKnowledge
      llmTxt = expandKnowledgeBlock(knowledgeBlock) + '\n' + llmTxt
    }
    knowledgeFetchRef.current.clearPending()
    const bubblePastes = pruneBlocksUtil(displayTxt, activePastes)
    if (bubblePastes.length) saveStoredPaste(llmTxt, displayTxt, bubblePastes, filePaths)

    setPrefillHint(false)
    if (!optionText) {
      setInput(''); setPendingFiles([]); setPasteBlocks([]); if (uiSlot) { delete drafts.current[uiSlot]; delete fileDrafts.current[uiSlot]; delete pasteDrafts.current[uiSlot]; saveDrafts() }
      // The challenge-handoff prompt is seeded into PREFILL_STORAGE_KEY and the
      // slot-restore effect re-applies it on slot changes. Once that prompt is
      // sent, clear the seed so a later slot-restore can't re-fill the (now
      // empty) composer with the already-sent text.
      try { sessionStorage.removeItem(PREFILL_STORAGE_KEY) } catch { /* sessionStorage unavailable */ }
    }
    // Target the slot the user is actually looking at (uiSlot, from the ref),
    // not the stale closure `activeSlot`. See the uiSlot note above.
    let slot = targetSlot ?? uiSlot
    // Only a normal (non-targeted) send consumes the one-shot "new session"
    // intent. A targeted send — e.g. submitting document comments to the
    // document's origin slot — must leave it intact for the user's next send.
    let forceNew = false
    if (!targetSlot) {
      forceNew = newSessionRef.current
      newSessionRef.current = false
    }
    if (!slot || forceNew) {
      sendingRef.current = true;
      // The composer was cleared above, so a create failure here would destroy
      // the user's text: `.unwrap()` rejects, send() unwinds, and nothing is
      // ever sent — no error bubble, no draft to recover, and sendingRef stuck
      // true (which suppresses the welcome state). Restore the composer, its
      // paste blocks and attachments, surface the failure, and bail.
      let created: { key: string } | null = null
      try {
        created = await dispatch(createSlot({ agent: pendingAgentRef.current || defaultAgent || undefined, model: pendingModelRef.current || undefined, mode: modeRef.current })).unwrap()
      } catch (e: unknown) {
        sendingRef.current = false
        // Recover the payload WITHOUT clobbering anything newer. Two traps make a
        // plain assignment lossy here:
        //  - The composer is only cleared above when `!optionText`, and the
        //    reachable forceNew path IS the optionText path (Projects / Dev Fleet /
        //    Prompts navigate to ?autoSend=1&newSession=1), so the composer still
        //    holds the user's own draft — overwriting it would destroy exactly the
        //    kind of text this guard exists to protect.
        //  - The create is awaited, so meanwhile the user may have typed, attached
        //    files, or switched sessions.
        // So MERGE into whatever the target slot holds now, and only touch live
        // composer state while that slot is still the one on screen.
        // Restore in place ONLY when the composer still belongs to the slot that
        // issued the send. A no-slot send (auto-send that fires before the slot list
        // resolves) must NOT fall back to whatever session auto-selection has since
        // activated: that would splice a new-session payload into an unrelated
        // session and send it there on retry. Those cases get a notification.
        const sameSlot = activeSlotRef.current === uiSlot
        const onScreen = sameSlot
        // Un-consume the one-shot new-session intent while the user is still on the
        // slot that issued the send — re-arming after they switched away would make
        // THAT session's next message spawn an unintended new session. Also re-arm
        // whenever there was no origin slot: the queued retry below MUST still create
        // its own session, and `sameSlot` is false there as soon as auto-selection
        // activates one mid-await, which would otherwise send the payload into an
        // unrelated existing session.
        // `|| !uiSlot` on the VALUE too, not just the condition: a slotless send also
        // reaches the create branch via `!slot` with `forceNew === false` (the
        // challenge-token flow, whose own createSlot failed), and arming `false` there
        // would let the queued retry deliver the payload as a user turn in whatever
        // unrelated session auto-selection activates. A send that had no origin slot
        // must always create its own session on retry.
        if (sameSlot || !uiSlot) newSessionRef.current = forceNew || !uiSlot
        const keepFiles = onScreen ? pendingFilesRef.current : (uiSlot ? fileDrafts.current[uiSlot] ?? [] : [])
        const restoredFiles = [...new Set([...keepFiles, ...sentFiles])]
        const keepPastes = onScreen ? pasteBlocksRef.current : (uiSlot ? pasteDrafts.current[uiSlot] ?? [] : [])
        const keptPasteIds = new Set(keepPastes.map(b => b.id))
        // Collapsed pastes resolve by `seq`, not id, and a paste made while the
        // composer was empty restarts at #1 — so a naive id-merge can leave two
        // blocks sharing #1, with both markers resolving to one of them and
        // silently swapping the user's content on retry. Re-sequence the carried
        // blocks past the kept ones and rewrite their markers in the payload text.
        const { text: payload, blocks: carriedPastes } = remapCarriedBlocks(
          raw,
          activePastes.filter(x => !keptPasteIds.has(x.id)),
          new Set(keepPastes.map(b => b.seq)),
        )
        const restoredPastes = [...keepPastes, ...carriedPastes]
        const keepText = onScreen ? inputRef.current : (uiSlot ? drafts.current[uiSlot] ?? '' : '')
        // Don't append a payload the composer already holds: a synchronously
        // rejected create can land before React flushes the clear, and the user may
        // have typed AROUND the payload during a slow one (superset case). The match
        // must be whitespace-delimited, not a bare substring — payload "test" inside
        // a newer draft "latest" is a different message, and treating it as already
        // restored would drop it.
        // Dedupe ONLY on exact equality. A whitespace-delimited occurrence is not
        // proof the payload was already restored — a draft like "please run tests
        // first" contains the distinct payload "run tests" — and treating it as
        // restored drops the message. Equality still covers the case this guard
        // exists for (a synchronously rejected create landing before React flushes
        // the clear), and errs toward a visible duplicate rather than silent loss.
        const alreadyRestored = keepText.trim() === payload.trim()
        const restoredText = !keepText.trim()
          ? payload
          : alreadyRestored
            ? keepText
            : `${keepText.replace(/\s+$/, '')}\n\n${payload}`
        if (onScreen && uiSlot) {
          setInput(restoredText); setPasteBlocks(restoredPastes); setPendingFiles(restoredFiles)
          // clearPending() above already consumed the knowledge selection, so a
          // retry would otherwise go out WITHOUT the context the user picked. Slot-
          // gated: selection is per-slot, so re-injecting while the user views another
          // session would smear it there. MERGE rather than skip-or-replace — `inject`
          // replaces, so skipping when a newer selection exists would drop the failed
          // turn's context, and replacing would drop what the user picked since. Newer
          // items win on an id collision.
          if (knowledgeBlock) {
            const newer = knowledgeFetchRef.current.pendingKnowledge?.items ?? []
            const newerIds = new Set(newer.map(i => i.id))
            knowledgeFetchRef.current.inject([...knowledgeBlock.items.filter(i => !newerIds.has(i.id)), ...newer])
          }
          dispatch(appendMessage({ role: 'error', content: `Could not start a new session: ${createFailReason(e)}. Your message was restored — send it again to retry.`, cls: '' }))
        }
        // Announce the failure wherever the in-chat bubble could not. Two shapes:
        //  - No origin slot at all: nothing durable can hold the text (a draft under
        //    the session auto-selection just activated would splice this payload into
        //    an unrelated conversation, and a composer restore lives in state the
        //    next slot switch wipes). So the notification CARRIES the message —
        //    expanded pastes and attachment paths included.
        //  - Origin slot exists but the user moved on: the draft is parked there, so
        //    point at it. An error bubble would land in the wrong session.
        if (!uiSlot) {
          // No session to restore into or persist to (a draft under the session
          // auto-selection just activated would splice this into an unrelated
          // conversation, and a notification body reaches the OS notification centre
          // — `useNativeNotification` publishes the latest unacked body, and any entry
          // can be re-marked unread, so `acked` is no barrier). Hand the payload back
          // to the mechanism that produced it instead: re-arming `autoSendRef` makes
          // the auto-send effect resend it. Text only — paste blocks and attachments
          // cannot exist on this path (no composer renders without a slot).
          //
          // If a slot is ALREADY active, the effect's deps
          // (`[send, connected, autoSendTick]`) will not change again on their own, so
          // bump the tick to drive the retry now — and stay silent, because that
          // retry reports its own outcome (it runs with a slot, so a second failure
          // produces the error bubble or the moved-on notification below). Telling the
          // user to retype while a retry is in flight invites a duplicate turn.
          // Otherwise nothing can drive it until a real `connected`/slot change, so
          // report it and be honest that the queue is tab-local.
          const retryNow = !!activeSlotRef.current
          autoSendRef.current = payload
          if (retryNow) {
            setAutoSendTick(t => t + 1)
          } else {
            dispatch(addNotification({
              ts: uniqueNotificationTs(),
              kind: 'agent',
              priority: 'critical',
              title: i18nT('pages.chatPage.could_not_start_a_new_session'),
              body: `${createFailReason(e)}. Your message is queued and will be sent when a session is ready — but it is held in this tab only, so if you navigate away or reload you will need to retype it.`,
            }))
          }
        } else if (!onScreen) {
          // The knowledge selection is NOT restored here: `inject` writes to the slot
          // the user is now viewing, so restoring it off-screen would attach the failed
          // turn's context to an unrelated session. Re-selecting is a two-click library
          // action (unlike typed text, which is unrecoverable), so this reports the gap
          // instead of routing knowledge per-slot — but it must not be silent.
          const lostContext = knowledgeBlock
            ? ' Its knowledge context was not kept — re-pick it before you resend.'
            : ''
          dispatch(addNotification({
            ts: uniqueNotificationTs(),
            kind: 'agent',
            priority: 'critical',
            title: i18nT('pages.chatPage.could_not_start_a_new_session'),
            body: `${createFailReason(e)}. Your message is saved as a draft in the session you sent it from.${lostContext}`,
            slot: uiSlot,
          }))
        }
        if (uiSlot) {
          setDraft(drafts.current, uiSlot, restoredText)
          setPasteDraft(pasteDrafts.current, uiSlot, restoredPastes)
          setFileDraft(fileDrafts.current, uiSlot, restoredFiles)
          saveDrafts()
        }
        return
      }
      const result = created
      slot = result.key;
      if (pendingProjectRef.current) {
        await api.chatSlotProject(result.key, pendingProjectRef.current).catch(e => {
          // eslint-disable-next-line no-console -- surface project-assign failures for debugging
          console.error('chatSlotProject failed', e)
        })
      }
    }
    setPendingAgent(''); setPendingModel(''); setPendingProject('')
    // Build meta for persistence (knowledge, files, pastes)
    const meta: Record<string, unknown> = {}
    if (filePaths.length) meta.files = filePaths
    if (bubblePastes.length) meta.pastes = bubblePastes
    if (knowledgeBlock) meta.knowledge = { items: knowledgeBlock.items.length, tokens: knowledgeBlock.totalTokens, titles: knowledgeBlock.items.map(i => i.title), content: knowledgeBlock.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }
    if (widgetOrigin) meta.origin = 'widget'
    const metaPayload = Object.keys(meta).length ? meta : undefined
    // Skip optimistic user bubble when the slot is busy (shared rule:
    // chatSlice.selectComposerBusy) — the backend sends a "queued" role
    // message instead, avoiding a duplicate.
    const _busy = selectComposerBusy(store.getState(), slot ?? null)
    if (!_busy || forceNew) {
      dispatch(appendMessage({ role: 'user', content: displayTxt, cls: '', ts: new Date().toISOString(), meta: metaPayload }))
    }
    window.dispatchEvent(new Event('voice-stop'))
    sendingRef.current = false
    isAtBottomRef.current = true
    setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    if (slot) dispatch(startLocalTurn(slot))
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 10_000)
    try {
      const r = await api.sendChat(llmTxt, slot ?? undefined, colorThemeRef.current, controller.signal, metaPayload, browsing)
      clearTimeout(timeout)
      const body = await r.json().catch(() => ({}))
      if (!body.queued && !body.ok) {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage({ role: 'error', content: body.error || i18nT('pages.chatPage.send_failed'), cls: '' }))
      }
    } catch (e: unknown) {
      clearTimeout(timeout)
      if (e instanceof DOMException && e.name === 'AbortError') {
        // Timeout — message was received, WS will deliver response
      } else {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage({ role: 'error', content: i18nT('pages.chatPage.connection_error'), cls: '' }))
        // Restore draft so the user doesn't lose their message.
        // Also restore the paste blocks backing any tokens in `txt`, otherwise
        // the restored text shows a dead `[ Paste #N · M lines ]` literal.
        // Persist for `slot` unconditionally (recoverable on disk), but only
        // touch the live input/blocks when `slot` is the one on screen. Compare
        // against activeSlotRef.current, NOT the closure's `activeSlot`: a
        // new-session/forceNew send creates a fresh slot and switches the UI to
        // it, so the closure value is stale — using it would leave the user's
        // just-typed message empty on the very session they're now viewing.
        // The ref reflects what's actually on screen, so it restores the text
        // visibly for a new-session failure while still not splicing a targeted
        // send's text into an unrelated slot the user is looking at.
        if (slot) {
          setDraft(drafts.current, slot, txt)
          setPasteDraft(pasteDrafts.current, slot, activePastes)
          saveDrafts()
          if (slot === activeSlotRef.current) { setInput(txt); setPasteBlocks(activePastes) }
        }
      }
    }
    // `send` is deliberately kept stable: it reads volatile values (agent,
    // model, project, mode, colorTheme, activeSlot) through refs so it does not
    // re-create on every keystroke/theme/agent change (it is passed to children
    // and consumed by the auto-send effect). setPending*/saveDrafts/scrollBottom
    // are stable, and defaultAgent is only a creation-time fallback — pulling
    // them into the dep array would defeat that stability without changing
    // outcomes.
    // send() no longer reads the closure `activeSlot` for its target. It reads
    // uiSlot = activeSlotRef.current, so it routes to the on-screen slot even
    // between the reducer flip and this callback's re-memoization.
    // activeSlot is left in deps as a harmless no-op: dropping it churns the
    // array for no behavior change (the ref is always current regardless).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, dispatch, connected])

  // Submit inline document comments to the session the file was opened from,
  // not the currently-active one. If the user switched sessions while the
  // panel was open, switch back to the origin session so the prompt + reply
  // land where the document belongs. switchSlot.pending sets activeSlot
  // synchronously, but send()'s closure activeSlot is stale until re-render,
  // so the origin slot is passed to send() explicitly.
  const submitComments = useCallback((message: string) => {
    const target = tabsCtl.activeTab?.slot ?? null
    if (target && target !== activeSlot) dispatch(switchSlot(target))
    send(message, target ?? undefined)
  }, [tabsCtl.activeTab, activeSlot, dispatch, send])

  // Auto-send when navigated with ?autoSend=1 or ?token= with prompt
  useEffect(() => { if (connected && autoSendRef.current) { const txt = autoSendRef.current; autoSendRef.current = null; send(txt) } }, [send, connected, autoSendTick])  

  // Widget interactivity: when a mcwidget iframe fires an action, PRE-FILL the
 // composer instead of auto-submitting. Auto-send was a
  // trust-boundary bypass: LLM-emitted <script> inside the sandboxed widget
  // iframe can call parent.postMessage directly, bypassing the in-iframe
  // isTrusted click guard, and the parent cannot distinguish that from a
  // genuine click. So a widget action must never become a user-role turn
  // without an explicit human gesture — the user reviews the pre-filled text
  // and presses Enter. We also record the pre-filled text so the resulting
  // send is tagged meta.origin='widget' for forensics (item 4).
  useEffect(() => {
    const handler = (e: Event) => {
      const text = (e as CustomEvent).detail?.text
      if (typeof text !== 'string' || !text) return
      widgetPrefillRef.current = text
      setInput(prev => (prev.trim() ? `${prev.trimEnd()}\n${text}` : text))
      setPrefillHint(true)
      // Touch: reveal the pre-filled composer without focusing (focus pops the
      // soft keyboard); desktop: focus, which scrolls it into view anyway.
      requestAnimationFrame(() => {
        const ta = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')
        if (!ta) return
        if (isTouchDevice()) { if (typeof ta.scrollIntoView === 'function') ta.scrollIntoView({ block: 'nearest' }) }
        else ta.focus()
      })
    }
    window.addEventListener('mc-widget-send', handler)
    return () => window.removeEventListener('mc-widget-send', handler)
  }, [])

  const approve = useCallback(async (action: string) => { if (activeSlot) await api.approveChatSlot(activeSlot, action) }, [activeSlot])
  const toApiDecision = (action: string): 'approve' | 'reject' =>
    action === 'approved' || action === 'trust' ? 'approve' : 'reject'
  const dismissApproval = useCallback((aid: string, decision?: string) => {
    dispatch(resolveByApprovalId({ id: aid, decision }))
    const n = store.getState().notifications.items.find(x => x.approval_id === aid)
    if (n) dispatch(removeNotificationByTs(n.ts))
  }, [dispatch])
  const switchAgent = useCallback(async (agentName: string) => {
    if (!activeSlot) {
      setPendingAgent(agentName)
      const mc = installedAgents.find(a => a.name === agentName)
      const templateName = provider.resolveAgentTemplate(mc || { name: agentName })
      queryClient.fetchQuery({ queryKey: ['resolved-model', templateName, provider.id], queryFn: () => provider.resolveModel(templateName) })
        .then(m => setPendingModel(m)).catch(() => setPendingModel(''))
      return
    }
    await api.chatSlotAgent(activeSlot, agentName)
    setAgentDropdown(false)
    // queryClient, setAgentDropdown, and the setPending* setters are all stable
    // (react-query client / useState setters / useCallback([])), so listing them
    // satisfies the linter without re-creating this callback.
  }, [activeSlot, installedAgents, provider, queryClient, setAgentDropdown, setPendingAgent, setPendingModel])
  const switchModel = useCallback(async (modelName: string) => {
    // 'auto' is stored VERBATIM, not collapsed to ''. Both resolve to the same
    // provider behaviour server-side, but '' is also the "never chosen" state,
    // and every reader of an empty model re-resolves it to the agent template's
    // model (the `resolvedModel` / `_initResolvedModel` queries below, and the
    // backend's slot.model backfill). Writing '' therefore made an explicit Auto
    // pick snap straight back to e.g. claude-opus-5 — Auto was unselectable.
    // kiro-cli advertises `auto` as a real model id (and its default_model), and
    // the ChatPane + Alt+Shift model-cycle paths already send it verbatim.
    if (!activeSlot) { setPendingModel(modelName); return }
    await api.chatSlotModel(activeSlot, modelName)
    // Keep the dropdown open after selecting — the user may switch models again
    // or drill into the reasoning-effort panel. Dismiss is via outside-click/Escape.
    // setPendingModel is a stable useState setter.
  }, [activeSlot, setPendingModel])
  const setProject = useCallback(async (path: string) => {
    if (!activeSlot) { setPendingProject(path); return }
    try {
      await api.chatSlotProject(activeSlot, path)
    } catch (e) {
      // eslint-disable-next-line no-console -- surface setProject failures for debugging
      console.error('setProject failed', e)
    }
    // setPendingProject is a stable ref-backed setter.
  }, [activeSlot, setPendingProject])

  const currentSlot = slots.find(s => s.key === activeSlot)
  // Refs so the "run in terminal" listener (registered once) always sees the
  // live panel controller + this chat's working directory.
  const tabsCtlRef = useRef(tabsCtl); tabsCtlRef.current = tabsCtl
  const currentProjectRef = useRef<string | undefined>(undefined)
  currentProjectRef.current = currentSlot?.project || undefined

  // ── Follow-up card actions (suggest_followup MCP tool) ───────────────────
  // Both routes PRE-FILL a composer and stop; neither sends. `setPendingInput`
  // is consumed by the effect above, which drops the text into the composer and
  // flags the prefill hint — the same path the Projects page and command
  // palette use, so there is one prefill mechanism, not a parallel one.
  //
  // Live per-slot card timestamps, read inside async actions without making them
  // depend on (and re-create on) every card change.
  const followupTsRef = useRef<Record<string, { items: FollowupItem[]; ts: number }>>({})
  followupTsRef.current = followupTsBySlot
  const followupAddToSession = useCallback((item: FollowupItem) => {
    if (!activeSlot) return
    // APPEND when the composer already holds unsent text: the pending-input path
    // replaces the draft and persists it, so a plain set would silently destroy
    // whatever the user was mid-way through typing (GPT review round 13).
    // `inputRef` is the live composer value; a blank line separates the two
    // because a handoff prompt is multi-line prose, not a word to concatenate.
    const draft = inputRef.current ?? ''
    dispatch(setPendingInput(draft.trim() ? `${draft.replace(/\s+$/, '')}\n\n${item.prompt}` : item.prompt))
    // Clear by the RENDERED card's ts, as the worktree action does: a newer card
    // for this slot can land between render and click, and an unqualified clear
    // would delete suggestions the user never saw.
    dispatch(clearFollowupCard({ slot: activeSlot, ts: followupTsRef.current[activeSlot]?.ts }))
  }, [dispatch, activeSlot])

  // Fallback branch name when the agent did not supply one: slugify the title
  // under FOLLOWUP_BRANCH_RE's grammar (the server re-validates, so a slug that
  // degenerates to empty is replaced rather than sent and rejected).
  const followupBranchFor = useCallback((item: FollowupItem) => {
    if (item.branch) return item.branch
    const slug = item.title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40)
    return `followup/${slug || 'suggestion'}`
  }, [])

  const followupStartInWorktree = useCallback(async (item: FollowupItem) => {
    const repo = currentSlot?.project
    if (!repo) throw new Error('This session has no project directory to branch from.')
    const originSlot = activeSlot
    // Capture the card's ts up front so completion clears only THIS card. A
    // newer card can arrive for the same slot while the request is in flight;
    // without the guard the older action's completion would clobber it.
    const originTs = originSlot ? followupTsRef.current[originSlot]?.ts : undefined
    // Create the worktree FIRST: if git refuses (branch exists, not a repo),
    // we must not have already spawned an empty session the user has to clean
    // up. The card surfaces the thrown message inline.
    const res = await api.createWorktree(repo, followupBranchFor(item))
    const path = res?.path
    if (!path) throw new Error(res?.error || 'Worktree creation returned no path')
    let slotKey = ''
    try {
      // `activate: false` on purpose: the slot must be SCOPED to the worktree
      // before the user can type into it. Activating first (the default) leaves a
      // window where the composer is live but `chatSlotProject` is still pending,
      // so a turn sent in that window would run in the default directory — agent
      // tools writing to the wrong checkout. It also means a scoping failure can
      // render its error on the still-mounted card instead of unmounting it
      // (GPT review, PR #461 round 9).
      const slot = await dispatch(createSlot({ mode, project: path, activate: false })).unwrap()
      slotKey = slot?.key || ''
    } catch {
      // The worktree exists but the session does not. Say so, and name the path:
      // the create endpoint is idempotent for its own destination, so pressing
      // the button again reuses this worktree instead of 409-ing on it.
      throw new Error(
        `Worktree created at ${path}, but its session could not be opened and scoped. ` +
        'Press the button again to retry — the existing worktree will be reused.',
      )
    }
    // A fulfilled thunk with no key would skip every guard below (scoping,
    // activation, focus verification) and prefill whatever session is on screen
    // — the exact fail-open the docs promise not to do. Fail closed instead.
    if (!slotKey) {
      throw new Error(
        `Worktree created at ${path}, but no session was returned. ` +
        'Press the button again to retry — the existing worktree will be reused.',
      )
    }
    // Scoping is NOT done here: `createSlot({ activate: false })` awaits the
    // project assignment before it publishes the slot, and deletes the session if
    // that fails, so the slot is never reachable in an unscoped state. A failure
    // therefore rejects the thunk and is reported by the catch above.
    // createSlot's fulfilled reducer deliberately does NOT activate its result
    // if the user switched sessions while the create was in flight. The
    // prefill below writes to the *active* composer, so without this the
    // prompt would land in whatever unrelated session is on screen and the new
    // worktree session would open empty. The user asked for this worktree by
    // clicking; take them to it — and if that fails, surface the error and
    // keep the card rather than prefilling the wrong conversation.
    // Read the store directly, NOT activeSlotRef: the ref is refreshed by a
    // render, and `unwrap()` resolves as soon as the reducer ran — so a stale
    // ref would report a failure (and skip the prefill) on a switch that in
    // fact succeeded. store.getState() sees the committed value immediately.
    // Hand the prompt over through PREFILL_STORAGE_KEY *before* the switch — the
    // same channel the ?sid / popout paths use. `setPendingInput` alone loses the
    // race: its consuming effect is declared BEFORE the per-slot draft-restore
    // effect, so when the switch and the prefill land in one React commit the
    // restore runs last and overwrites the composer with the incoming slot's
    // (empty) draft, and the prompt vanishes. Seeding the prefill makes the
    // restore itself apply the prompt, so there is nothing left to race.
    writePrefill(slotKey, item.prompt)
    if (store.getState().chat.activeSlot !== slotKey) {
      try {
        await dispatch(switchSlot(slotKey)).unwrap()
      } catch {
        throw new Error(
          `Worktree ready at ${path}, but its session could not be opened. ` +
          'Switch to it in the sidebar, or press the button again.',
        )
      }
    }
    if (store.getState().chat.activeSlot !== slotKey) {
      throw new Error(
        `Worktree ready at ${path}, but its session is not in focus. ` +
        'Switch to it in the sidebar, or press the button again.',
      )
    }
    dispatch(setPendingInput(item.prompt))
    if (originSlot) dispatch(clearFollowupCard({ slot: originSlot, ts: originTs }))
  }, [currentSlot?.project, followupBranchFor, dispatch, mode, activeSlot])

  // Feed the Web Preview tab from chat, by signal type (previewFeedDecision).
  // Neither path ever navigates the iframe: both hand the URL to the panel as a
  // "Load preview" card (setSessionPreviewPending) — the GET fires only on the
  // user's explicit Load click, so agent output can never drive the scripted
  // iframe to an arbitrary host without consent.
  //   • marker (`kirocrew:preview`, explicit agent intent) → also OPEN the tab,
  //     once per distinct URL. The applied URL is PERSISTED per slot so a route
  //     remount doesn't reopen a card the user dismissed; an in-memory ref
  //     backstops a failed localStorage write.
  //   • heuristic (a localhost URL merely mentioned in prose) → offer the card
  //     WITHOUT opening the tab, and only when no target is set yet.
  // Reuses the shared tabsCtlRef so the effect stays mount-stable as the strip churns.
  const appliedPreviewMemRef = useRef<Record<string, string>>({})
  useEffect(() => {
    const slot = activeSlot
    if (!slot) return
    let existing = ''
    try {
      existing = localStorage.getItem(`mc-webpreview-url:${slot}`)
        || localStorage.getItem(`mc-webpreview-pending:${slot}`) || ''
    } catch { /* ignore */ }
    const feed = previewFeedDecision(detectPreviewUrl(messages), !!existing)
    if (!feed) return
    const norm = normalizeUrl(feed.url)
    if (!norm) return
    if (feed.open) {
      // Marker → surface the Load-preview card + open the tab, deduped via a
      // PERSISTED applied key (survives remounts) plus an in-memory ref
      // (survives a failed localStorage write) so it never re-opens.
      let applied = ''
      try { applied = localStorage.getItem(`mc-webpreview-applied:${slot}`) || '' } catch { /* ignore */ }
      if (applied === norm || appliedPreviewMemRef.current[slot] === norm) return
      appliedPreviewMemRef.current[slot] = norm
      try { localStorage.setItem(`mc-webpreview-applied:${slot}`, norm) } catch { /* ignore */ }
      // Loopback-only (enforced inside setSessionPreviewPending): a rejected
      // (non-loopback) marker feeds nothing — and must not open the tab either.
      if (!setSessionPreviewPending(slot, norm)) return
      dispatch(openActivityPanel())
      tabsCtlRef.current.openView('browser')
    } else {
      setSessionPreviewPending(slot, norm)      // heuristic offer: card only, no open, no load
    }
  }, [messages, activeSlot, dispatch])
  // Auto-open the Browser panel when the agent starts browsing — the live
  // Playwright mirror streams as `kirocrew-browser-frame` events. Open/focus the
  // tab only at the START of a stream (new session_key, or after a >90s gap),
  // NOT on every frame, so it can't steal focus from a tab the user switched to
  // mid-browse. This replaces the old floating BrowserLiveView window.
  const browseFrameOpenedRef = useRef<{ key: string | null; ts: number }>({ key: null, ts: 0 })
  useEffect(() => {
    const onFrame = (e: Event) => {
      const key = (e as CustomEvent<{ session_key?: string }>).detail?.session_key ?? null
      const now = Date.now()
      // Only auto-open the Browser tab when the browsing session IS the one on
      // screen (the active slot). A background session's frames must not open —
      // or, with the panel's own session gate, display in — another session's
      // panel; that would misattribute the "Let the agent act" grant.
      if (!key || key !== activeSlotRef.current) return
      const prev = browseFrameOpenedRef.current
      if (prev.key !== key || now - prev.ts > 90_000) {
        dispatch(openActivityPanel())
        tabsCtlRef.current.openView('browser')
        // A freshly-mounted panel starts with browseOn=false; replay the current
        // browse-mode so the live mirror shows the right interaction state.
        window.dispatchEvent(new CustomEvent(BROWSE_MODE_EVENT, { detail: { on: browseModeRef.current } }))
      }
      browseFrameOpenedRef.current = { key, ts: now }
    }
    window.addEventListener('kirocrew-browser-frame', onFrame)
    return () => window.removeEventListener('kirocrew-browser-frame', onFrame)
  }, [dispatch])
  // "Run in terminal" (from chat code blocks): open a FRESH terminal tab in
  // this chat and run the command in it, starting in the chat's working dir.
  // The result is echoed back so the code-block button can show sent/failed.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      const code: string = detail.code
      const reqId: string = detail.reqId
      if (typeof code !== 'string' || !code) return
      dispatch(openActivityPanel())
      const sessionId = tabsCtlRef.current.openTerminal({ cwd: currentProjectRef.current })
      let settled = false
      const emit = (ok: boolean) => {
        if (settled) return
        settled = true
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId, ok } }))
      }
      if (!sessionId) { emit(false); return }
      const unsub = onTerminalReady(sessionId, () => { emit(sendToTerminalSession(sessionId, code)) })
      // Give the PTY time to connect; if it never does, report failure.
      setTimeout(() => { unsub(); emit(false) }, 6000)
    }
    window.addEventListener('mc:run-in-terminal', handler)
    return () => window.removeEventListener('mc:run-in-terminal', handler)
  }, [dispatch])
  // Cold-tab hydration: after a reload (or when restoring a slot's strip from
  // the persisted panel-tabs store), file tabs come back as lightweight
  // references with their heavy content stripped (content === undefined). Read
  // it back declaratively with useQueries — one ['file-read', path] query per
  // cold file tab (same key/shape as handleFileOpen so the cache dedupes).
  // Once a tab's content is patched in it drops out of coldFileTabs and its
  // query unsubscribes. Diff tabs are transient (not persisted — a restored
  // diff can't reconstruct the original turn snapshot); artifact tabs
  // self-hydrate via ArtifactPanel's own ['artifact', slug] query.
  const coldFileTabs = useMemo(
    () => tabsCtl.tabs.filter(t => t.kind === 'file' && t.path && t.content === undefined),
    [tabsCtl.tabs],
  )
  const coldFileResults = useQueries({
    queries: coldFileTabs.map(t => ({
      queryKey: ['file-read', t.path!],
      queryFn: async () => {
        const res = await fetch(fileReadUrl(t.path!))
        const text = res.ok
          ? await res.text()
          : res.status === 404 ? i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
          : i18nT('pages.chatPage.unable_to_read_file')
        return { text, ok: res.ok }
      },
      staleTime: 10_000,
    })),
  })
  // Mirror settled reads into the tab strip. useQueries owns the fetch
  // lifecycle (error/retry/dedupe); this effect only writes results back, and
  // the content===undefined guard keeps it idempotent (a hydrated tab leaves
  // coldFileTabs, so it isn't re-patched).
  useEffect(() => {
    coldFileResults.forEach((r, i) => {
      const t = coldFileTabs[i]
      if (!t || t.content !== undefined) return
      if (r.data) tabsCtl.patchTab(t.id, { content: r.data.text })
      else if (r.isError) tabsCtl.patchTab(t.id, { content: i18nT('pages.chatPage.error_reading_file') })
    })
  }, [coldFileResults, coldFileTabs, tabsCtl])
  // Session mode of the active slot. In the unified chat view the page-level
  // `mode` prop is always '' — the slot's own mode is the source of truth for
  // header identity (Autopilot icon + tooltip).
  const effectiveMode = currentSlot?.mode || mode
  const title = currentSlot?.title && currentSlot.title !== currentSlot.key ? currentSlot.title : activeSlot || ''
  const displayMode = approvalMode === 'yolo' ? 'yolo' : currentSlot?.trust ? 'trust' : currentSlot?.trust_reads ? 'trust_reads' : 'normal'
  // Resolve model for existing slots that don't have one stored
  const _slotMc = installedAgents.find(a => a.name === currentSlot?.agent)
  const _slotTemplateName = (currentSlot && !currentSlot.model && installedAgents.length > 0) ? provider.resolveAgentTemplate(_slotMc || { name: currentSlot.agent || 'default' }) : ''
  const { data: _slotResolvedModel } = useQuery({
    queryKey: ['resolved-model', _slotTemplateName, provider.id],
    queryFn: () => provider.resolveModel(_slotTemplateName),
    enabled: !!_slotTemplateName,
  })
  // Derived, not mirrored into state via an effect: the effect form cost an extra
  // render pass every time the query settled, for a value that is a pure function
  // of the query result.
  const resolvedModel = _slotResolvedModel || ''
  // The configured default effort for new sessions. A slot that has never
  // touched the effort control carries '' (no override) but still RUNS at this
  // default — the backend applies `slot.reasoning_effort or agent.reasoning_effort`
  // — so the composer must show the inherited value rather than a bare
  // "Default", which read as "the model decides" and hid the real setting.
  const { data: _defaultEffort } = useQuery({
    queryKey: ['default-effort', provider.id],
    queryFn: () => provider.resolveDefaultEffort(),
    enabled: provider.capabilities.reasoningEffort,
  })
  const defaultEffort = _defaultEffort || ''
  // Effort actually in force for the active slot: per-slot override, else the
  // configured default. Display only — the slot's raw value still drives the
  // picker so "no override" stays distinguishable from an explicit pick.
  const effectiveEffort = currentSlot?.reasoning_effort || defaultEffort
  // Branch label for the active project chip. The user can check out a
  // different branch outside the dashboard at any time, so this refetches on a
  // slow interval and on window focus rather than being read once. A failure
  // (no git, path gone, not a repo) leaves the chip showing the folder name
  // alone, which is the pre-existing behaviour.
  const _slotProject = currentSlot?.project || ''
  const { data: projectGit, isError: projectGitError } = useQuery({
    queryKey: ['project-git', _slotProject],
    queryFn: () => api.projectGit(_slotProject),
    enabled: !!_slotProject,
    staleTime: 15_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
  // React Query keeps the last successful data after a failed refetch, so a
  // project that was deleted or revoked would keep showing its old branch
  // indefinitely. Treat an errored query as "no branch" and fall back to the
  // folder name, which is the same degradation as a non-repo project.
  const projectBranch = projectGitError
    ? ''
    : projectGit?.branch || (projectGit?.detached ? projectGit.head || '' : '')
  const [sidebarPinned, setSidebarPinned] = useState(() => localStorage.getItem('mc-sidebar-pinned') !== 'false')
  const isMobile = useIsMobile()
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const v = parseInt(localStorage.getItem('mc-sidebar-width') || '', 10)
    return !isNaN(v) && v >= SIDEBAR_MIN && v <= SIDEBAR_MAX ? v : 260
  })
  const [sidebarDragging, setSidebarDragging] = useState(false)
  const [editingTitle, setEditingTitle] = useState(false)
  // Native session grid "split mode": an in-place tiling of the chat surface (NOT an
  // overlay). The flag is EPHEMERAL per mount — nav/refresh lands on single chat —
  // but the LAYOUT persists per anchor slot (splitLayoutStore). So a split is
  // preserved across navigation, and a member session opened on its own shows single
  // chat plus an "in split" badge that re-enters it (β model). `splitAnchor` is the
  // slot whose split we're showing (the one ⌘D'd from, or the badge's target).
  // enterSplit opens Split View for `anchor`: SessionGridView restores anchor's saved
  // layout if one exists, else seeds [anchor | placeholder]. Closing back down to a
  // single session dissolves the layout and collapses to native chat (onCollapse).
  const enterSplit = useCallback((anchor: string | null) => { setSplitAnchor(anchor); setSplitMode(true) }, [])
  // Anchor of the persisted split the active session belongs to (>= 2 live sessions),
  // or null — drives the "in split" badge in single chat. Validated against live
  // slots so a stale layout (a member was deleted) never shows a dead badge.
  const splitAnchorForActive = useMemo(() => {
    if (!splitFeatureEnabled || splitMode || !activeSlot) return null
    const anchor = anchorForSlot(activeSlot)
    if (!anchor) return null
    const liveKeys = new Set(slots.map((s) => s.key))
    return sessionSlots(loadLayout(anchor)).filter((k) => liveKeys.has(k)).length >= 2 ? anchor : null
  }, [splitFeatureEnabled, splitMode, activeSlot, slots])
  // True when the active session IS the anchor of its live persisted split (the slot
  // ⌘D was originally pressed from). The anchor's natural view IS its split, so we
  // auto-open it (no badge, no extra click); non-anchor members stay single chat + badge.
  const activeIsSplitAnchor = splitAnchorForActive !== null && splitAnchorForActive === activeSlot
  // Auto-enter split when you land on its anchor. Gated on splitMode being off (so we
  // don't fight an in-progress exit) and on a resolved activeSlot + real >=2-member live
  // layout (so a fresh refresh never seeds an orphan pane — the old localStorage race).
  // Members never auto-enter; closing a split to 1 dissolves the layout so there's no loop.
  useEffect(() => {
    if (embedMode || splitMode || !activeIsSplitAnchor) return
    enterSplit(splitAnchorForActive)
  }, [embedMode, splitMode, activeIsSplitAnchor, splitAnchorForActive, enterSplit])
  // ⌘D / Ctrl+D enters split mode from single chat (splitting the current session).
  // Inside split mode the grid (SessionGridView) owns ⌘D = split the focused pane.
  useEffect(() => {
    if (embedMode) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'd') {
        if (!splitFeatureEnabled || splitMode || !activeSlot) return
        e.preventDefault()
        enterSplit(activeSlot)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [embedMode, splitMode, enterSplit, splitFeatureEnabled, activeSlot])
  const [generatingTitleSlots, setGeneratingTitleSlots] = useState<Set<string>>(new Set())
  const [titleDraft, setTitleDraft] = useState('')
  const lastTextIdx = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') return i
    }
    return -1
  }, [messages])
  const [regenerating, setRegenerating] = useState(false)
  useEffect(() => { setRegenerating(false) }, [activeSlot])
  // Clear typing dots as soon as streaming starts
  useEffect(() => {
    if (regenerating && isStreaming) setRegenerating(false)
  }, [regenerating, isStreaming])
  // Safety timeout
  useEffect(() => {
    if (!regenerating) return
    const t = setTimeout(() => { setRegenerating(false) }, 30_000)
    return () => clearTimeout(t)
  }, [regenerating])
  const handleRegenerate = useCallback(() => {
    if (!activeSlot || regenerating || slotRunning) return
    const uIdx = messages.slice(0, lastTextIdx).map(mm => mm.role).lastIndexOf('user')
    if (uIdx < 0) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(uIdx + 1))
    setRegenerating(true)
    api.regenerateSlot(activeSlot).catch((e: unknown) => {
      // eslint-disable-next-line no-console -- surface regenerate failures for debugging
      console.warn('regenerate failed', e)
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, regenerating, slotRunning, messages, lastTextIdx, dispatch])

  const [flyingQuote, setFlyingQuote] = useState<{ text: string; from: DOMRect } | null>(null)
  const inputAreaRef = useRef<HTMLDivElement>(null)

  const handleQuote = useCallback((text: string, rect: DOMRect) => {
    const quoted = text.split('\n').map(line => `> ${line}`).join('\n')
    setInput(prev => {
      // Append new quote after existing content (supports multiple quotes)
      if (!prev.trim()) return `${quoted}\n\n`
      return `${prev.trimEnd()}\n\n${quoted}\n\n`
    })
    // Trigger flying animation
    setFlyingQuote({ text, from: rect })
    // Touch: reveal the pre-filled composer without focusing (focus pops the
    // soft keyboard); desktop: focus, which scrolls it into view anyway.
    requestAnimationFrame(() => {
      const ta = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')
      if (!ta) return
      if (isTouchDevice()) { if (typeof ta.scrollIntoView === 'function') ta.scrollIntoView({ block: 'nearest' }) }
      else ta.focus()
    })
  }, [])

  // "Ask" (Select-to-Ask): open the isolated /side conversation seeded with the
  // selection, WITHOUT touching the main chat context (unlike handleQuote, which
  // injects into the main composer). Mirrors the /side slash command's
  // openActivityToTab('side') bridge, then hands the selection to SideChat via a
  // `side-seed` CustomEvent (same event-bridge pattern as openActivityToTab /
  // reveal-slot — no new prop-drilling, no backend change). No transit
  // animation: the popup routes the selection straight to the Side panel
  // (matches Codex's "Ask in side chat" behavior).
  const handleAsk = useCallback((text: string) => {
    dispatch(openActivityToTab('side'))
    // The Side panel (and its `side-seed` listener) mounts asynchronously once
    // the panel opens. Poll a few frames for the Side input as a mount signal,
    // then dispatch the seed. Fall back to dispatching after a cap so the
    // feature still works even if the input never resolves.
    const trySeed = (attempt = 0) => {
      const mounted = document.querySelector('textarea[aria-label="Ask a side question"]')
      if (mounted || attempt >= 20) {
        window.dispatchEvent(new CustomEvent('side-seed', { detail: { text } }))
      } else {
        requestAnimationFrame(() => trySeed(attempt + 1))
      }
    }
    requestAnimationFrame(() => trySeed())
  }, [dispatch])

  const handleEditResend = useCallback((index: number, ts: string, newContent: string) => {
    if (!activeSlot || slotRunning) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(index))
    dispatch(appendMessage({ role: 'user', content: newContent, cls: '', ts: new Date().toISOString() }))
    setRegenerating(true)
    // Use /rewind (fork-and-swap) — discards the orphan kiro-cli session so
    // truncated forward turns can't resurface on resume. Mirrors kiro-cli's
    // native /rewind slash command, but swaps the session under the same
    // slot identity so the UI stays in place (no new tab, no title change).
    rewindWithRollback(activeSlot, ts, newContent, () => {
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, slotRunning, messages, dispatch])

  const searchCtxValue = useMemo(() => ({
    term: search.term,
    caseSensitive: search.caseSensitive,
    currentMessageIdx: search.currentMessageIdx,
    currentOccurrenceIdx: search.currentOccurrenceIdx,
  }), [search.term, search.caseSensitive, search.currentMessageIdx, search.currentOccurrenceIdx])

  const renderUserContentCb = useCallback(
    (c: string, mt: Record<string, unknown> | undefined) => renderUserContent(c, mt, handleFileOpen),
    [handleFileOpen]
  )

  const cancelTitleRef = useRef(false)
  const composingRef = useRef(false)
  useEffect(() => {
    const togglePin = () => {
      // Always-available collapse. Only guard is no-sessions (the sidebar is
      // force-open then anyway, so there is nothing to collapse).
      if (filteredSlotsRef.current.length === 0) return
      setSidebarPinned(p => {
        const next = !p
        safeSetItem('mc-sidebar-pinned', String(next))
        return next
      })
    }
    window.addEventListener('toggle-pin-chat-sidebar', togglePin)
    return () => window.removeEventListener('toggle-pin-chat-sidebar', togglePin)
  }, [])

  const lastRole = messages[messages.length - 1]?.role ?? ''
  // Precompute: index of last finalized assistant message (tools after this are "trailing")
  // The activity panel has exactly two modes, and the question that picks one
  // is NOT "how wide is the window" — it is "how much width is left for the
  // chat". Subtract the shell's nav rail and the session sidebar (both of which
  // the user can hide) from the viewport: if what remains still seats the panel
  // at its minimum PLUS a usable chat pane, the panel sits BESIDE the chat.
  // Otherwise it FILLS the chat column, with the sidebar and rail untouched.
  //
  // Consequences worth stating:
  //  - Hiding the rail (162px) or the sidebar (~260px) can promote fill -> beside
  //    at a viewport width that could not seat both a moment earlier.
  //  - Mobile needs no special case: rail 0 + sidebar 0 (its drawer is fixed,
  //    not a flex sibling) always lands under the threshold. isMobile is still
  //    forced to fill so a 700px phone-class viewport cannot go beside.
  //  - The measurement is loop-free ON PURPOSE. It reads the rail TRACK and the
  //    sidebar's own state, never the chat container's painted width — that
  //    shrinks when the panel opens, which would oscillate beside <-> fill.
  const railWidth = useRailWidth()
  const [winW, setWinW] = useState(() => window.innerWidth)
  useEffect(() => {
    const onResize = () => setWinW(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const toggleAct = useCallback(() => {
    // Opening with no tabs shows the empty-state launcher grid (no seeded
    // default view) -- the user picks what to open.
    dispatch(toggleActivity())
  }, [dispatch])
  // Header-launched toggle: the top-bar Activity button (App.tsx) dispatches
  // this event so the panel-close coordination above stays in ChatPage.
  useEffect(() => {
    const h = () => toggleAct()
    window.addEventListener('toggle-activity-panel', h)
    return () => window.removeEventListener('toggle-activity-panel', h)
  }, [toggleAct])
  // Bridge explicit view requests (e.g. the /side slash command dispatches
  // openActivityToTab('side')) into the tab model.
  const activityTab = useAppSelector(s => s.chat.activityTab)
  // Skip the mount invocation: this bridge only reacts to a GENUINE activityTab
  // change (e.g. the /side slash command via openActivityToTab). Firing on mount
  // would re-open the activityTab view (Files by default) on top of the
  // now-persisted strip every time ChatPage remounts after a route change.
  const activityTabBridged = useRef(false)
  useEffect(() => {
    if (!activityTabBridged.current) { activityTabBridged.current = true; return }
    if (activityOpen) tabsCtl.openView(activityTab === ('nav' as string) ? 'files' : activityTab)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activityTab])
  // Stable row callbacks. These used to be inline lambdas in the row renderer, so
  // every render handed AssistantMessage a fresh function identity and its memo()
  // could never bail out — the boundary was broken at the call site, not by the
  // renderer. Both read live state from a ref / the store rather than closing over
  // it, so neither needs a dependency that churns while a turn streams.
  const handleSpeak = useCallback((content: string) => {
    if (store.getState().chat.voicePlaying) {
      window.dispatchEvent(new Event('voice-stop'))
      dispatch(setVoiceAudio(null))
      return
    }
    dispatch(setVoiceAudio(null))
    api.voiceSynthesize(activeSlotRef.current || '', content).catch(() => {})
  }, [dispatch])

  const handleApplyPlan = useCallback(async (steps: PlanStepInput[]) => {
    try {
      const r = await api.planFromChat(steps, planTaskId)
      if (r.ok) { navigate('/projects?applied=' + (r.task_id || planTaskId)); return true }
    } catch { /* API error */ }
    alert(i18nT('pages.chatPage.failed_to_apply_plan'))
    return false
  }, [planTaskId, navigate])

  // Grouping depends ONLY on `messages`; `slotRunning` decides one boolean on the
  // trailing turn. Bundling both in one memo re-ran the whole O(N) grouping pass on
  // every turn start/stop just to flip that flag, and the new identity cascaded into
  // messageToDisplayIdx / visibleIndexMap / the virtualizer. Split: group once, then
  // apply the flag in O(1).
  const groupedTurns = useMemo(() => groupDisplayItems(messages), [messages])

  const displayItems = useMemo<DisplayItem[]>(
    () => applyRunningState(groupedTurns, slotRunning),
    [groupedTurns, slotRunning],
  )

  // Keep the ref in sync so handleRangeChanged / updatePinnedPrompt
  // read the latest displayItems. useEffect rather than render-body
  // mutation keeps us in line with React's rules of render (no side
  // effects) — the one-tick lag is irrelevant because callbacks fire
  // after commit, by which point the ref has caught up.
  useEffect(() => { displayItemsRef.current = displayItems }, [displayItems])

  // Pinned prompt: keep the enablement ref in sync (updatePinnedPrompt is declared
  // above chatConfig and reads it through a ref), and recompute after the list
  // changes — a new turn shifts geometry with no scroll event of its own.
  useEffect(() => {
    pinEnabledRef.current = chatConfig.pinLastPrompt
    if (!chatConfig.pinLastPrompt) setPinned(null)
  }, [chatConfig.pinLastPrompt])
  useEffect(() => { updatePinnedPrompt() }, [displayItems, updatePinnedPrompt])
  // Expanded state PERSISTS as the pinned prompt is replaced by the next one
  // while scrolling — the user asked for a sticky "keep it open" behaviour, so we
  // do NOT collapse on `pinned.idx` change. It still resets on slot switch below
  // (a different session should start collapsed).

  // Virtualized display — only mounts items in the viewport window. The
  // virtualizer shares `scrollerRef` with useScrollManager so the legacy
  // scroll APIs (scrollToDisplayIndex, scrollToBottom) operate on the
  // same DOM element. Its own follow-output handles streaming auto-pin
  // and append-pin, so the legacy useStreamingScroll/useFollowOutput
  // calls below are no-ops in this configuration but are kept invoked
  // for hook-call stability.
  // Per-message identity used to derive BOTH the inner bubble key (renderMessage,
  // ~line 2848) AND the virtualizer/HeightCache key (virtualKey, below). Keeping
  // them on the SAME identity means the #253 steer-bubble stability fix protects
  // the virtualizer + HeightCache layer too, not just the bubble:
  //   1. Prefer meta.clientTs — the steer_push echo overwrites `ts` (client→
  //      server) mid-stream; keying on `ts` alone would flip the key, orphan the
  //      cached height, revert the row to the estimate, and lurch the viewport.
  //   2. Fall back to `ts` for ordinary messages.
  //   3. For ts-less messages (e.g. an error appended on the send-failure path)
  //      DON'T fall back to the array index: truncateAfterIndex / regenerate
  //      would shift the key of every following row → mass remount + a large
  //      scroll swing. Mint a per-message-instance id instead. Object identity
  //      is stable across renders under Immer's structural sharing, and survives
  //      truncation of *later* rows, so the key is stable for the message's life.
  //      (A durable id stamped in the reducer at append would also survive a full
  //      refetch/replace — see .scroll-handoff-c.md.)
  const msgIdSeq = useRef(0)
  const msgIds = useRef(new WeakMap<ChatMessage, string>())
  const stableMsgKey = useCallback((m: ChatMessage): string => {
    const explicit = (m.meta?.clientTs as string | undefined) || m.ts
    if (explicit) return explicit
    let id = msgIds.current.get(m)
    if (!id) { id = `mid-${msgIdSeq.current++}`; msgIds.current.set(m, id) }
    return id
  }, [])
  const virtualKey = useCallback(
    (it: DisplayItem, i: number) => virtualKeyFor(it, i, stableMsgKey),
    [stableMsgKey],
  )

  // (Sticky widget detection removed — widgets now unmount with the
  // window like any other item. See useVirtualChat call below for the
  // memory-vs-flicker trade-off rationale.)

  const virt = useVirtualChat<DisplayItem>({
    items: displayItems,
    getKey: virtualKey,
    sessionId: activeSlot ?? '__no_slot__',
    estimatedHeight: 100,
    // Overscan tradeoff (experimental):
    //   smaller (3)   → least memory, frequent widget remounts on small scrolls
    //   medium  (12)  → screenful of buffer, ~290MB baseline / 450MB while scrolling
    //   larger  (25)  → fewer remounts but inflated RAM from warm iframe pool
    // Currently testing 6 — middle ground between memory and remount frequency.
    overscan: 6,
    // No isSticky: widget messages unmount along with everything else
    // when they leave the viewport window. Trade-off: scrolling back to
    // an old widget causes its iframe to reload (1-2 frames of flicker).
    // Memory benefit: only widgets in the active window are kept alive,
    // ~290MB baseline instead of 500MB+ with all-widgets-sticky.
    externalScrollerRef: scrollerRef,
    // The currently-streaming message is always the LAST message and
    // therefore always ends up in the LAST displayItems entry — whether
    // that entry is itself the streaming `single`, or a `turn`/`group`
    // that the streaming message got folded into (turns only close when a
    // new user/nudge message opens the next one, by which point the prior
    // streaming message has already finished). Passing its index lets the
    // virtualizer track that one row's growth every RO tick instead of
    // debouncing it into a stale-then-jump spacer (see the `streamingIndex`
    // option's doc and useVirtualChat.spacerLurch.test.tsx).
    streamingIndex: isStreaming && displayItems.length > 0 ? displayItems.length - 1 : undefined,
  })

  // Single scroll controller wiring: expose the virtualizer's follow API to
  // the early effects/handlers (declared above) via refs, and derive the
  // at-bottom state for the jump-to-bottom pill. The virtualizer owns slot
  // entry, streaming follow, and append-pin; ChatPage only triggers explicit
  // jumps (send, jump-to-latest pill) through these.
  const isAtBottom = virt.isAtBottom
  // Mirror the virtualizer's follow API into the refs the early effects/handlers
  // (declared above) read. Done in a layout effect rather than the render body
  // so a concurrent render React throws away can't write stale callbacks into
  // the refs. Layout effects run before passive effects, so the gating effect
  // that reads isAtBottomRef.current still sees this commit's value.
  useLayoutEffect(() => {
    isAtBottomRef.current = isAtBottom
    vScrollToBottomRef.current = virt.scrollToBottom
    mountIndexRef.current = virt.mountIndex
    scrollToIndexSmoothRef.current = virt.scrollToIndexSmooth
  })

  // Legacy aliases so the JSX below keeps reading the same names.
  const visibleDisplayItems = virt.virtualItems
  // No "load more" pagination indicator with virtualization — the
  // windowing engine swaps mounted/placeholder automatically.

  // Reset scroll-navigation state on slot switch.
  useEffect(() => {
    setPinned(null)
    setPinExpanded(false)
  }, [activeSlot])

  const allQueuedMessages = useMemo(() => messages.filter(m => m.role === 'queued'), [messages])
  // Only user-typed queued messages get the interactive (edit/cancel) card
  // stack. System injections are excluded (isNonInteractiveQueued): sub-agent
  // deliveries collapse into one progress line, and synthetic turn-recovery
  // continuations (tool refusal / stalled turn / stalled tool / interrupted /
  // empty response) are machine-facing orchestration — they drain
  // automatically and must never render as an editable/cancellable "user" card
  // (editing or cancelling one corrupts the recovery). They surface as a
  // compact RecoveryCard in the transcript once dequeued instead.
  const queuedMessages = useMemo(
    () => allQueuedMessages.filter(m => !isNonInteractiveQueued(m)),
    [allQueuedMessages],
  )
  // Count sub-agent deliveries directly (not by subtraction): recovery
  // injections are also excluded from queuedMessages, but they are NOT
  // sub-agent results and must not inflate the delivery progress line.
  const systemDeliveryCount = useMemo(
    () => allQueuedMessages.filter(m => isSystemDelivery(m)).length,
    [allQueuedMessages],
  )

  // Mid-turn steer: inject the composer content into the RUNNING turn instead
  // of queueing for the next one. Mirrors send()'s payload prep so pending
  // files ride along — images become `![image](path)` markdown and other
  // files `[attached_file N]` tokens (previously steer sent only the raw
  // text, silently dropping attachments). kiro-cli's `_session/steer` is a
  // text-only channel, so unlike a queued send the image travels as its
  // absolute path for the agent to open with a tool, not as an inline
  // content block. Paste tokens are expanded for the LLM the same way
  // send() does. The POST goes through steerMutation (above); fire-and-forget
  // — the backend falls back to the queue if steer is unavailable, and echoes
  // the text inline via the 'steer_push' WS event. Composer, pending files,
  // paste blocks, and the per-slot drafts are all cleared HERE (not in
  // ChatInput) so text and attachments clear atomically.
  const steer = useCallback(() => {
    if (!activeSlot) return
    const raw = inputRef.current.trim()
    const files = pendingFilesRef.current
    if (!raw && !files.length) return
    const { txt } = prepareSendPayload(raw, files)
    const activePastes = pasteBlocksRef.current
    const llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
    // Optimistically show the steered text immediately. Since steer became the
    // default mid-turn action (split send button), pressing Enter while a turn
    // is running routes here — which previously had NO optimistic bubble, so
    // the message only appeared once the backend echoed it via the 'steer_push'
    // WS event, making it look like nothing happened until the response resumed.
    // Tagged meta.optimistic so the echo reconciles this bubble in place
    // (appendSlotMessage) instead of rendering a duplicate.
    dispatch(appendMessage({ role: 'user', content: llmTxt, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { steer: true, optimistic: true } }))
    steerMutation.mutate(llmTxt)
    setInput(''); setPendingFiles([]); setPasteBlocks([])
    delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]
    saveDrafts()
  }, [activeSlot, steerMutation, saveDrafts, dispatch])

  const handleCancelQueued = useCallback((queueId: string) => {
    if (!activeSlot) return
    const msg = messagesRef.current.find(m => m.role === 'queued' && (m.meta?.queueId as string) === queueId)
    if (msg?.content) setInput(msg.content)
    // Optimistically remove the card; WS event is a no-op if already gone
    dispatch(cancelQueuedMessage({ slot: activeSlot, queue_id: queueId }))
    api.cancelQueuedMessage(activeSlot, queueId).catch(() => {})
  }, [activeSlot, dispatch])

  const handleInterruptQueued = useCallback((queueId: string) => {
    if (!activeSlot) return
    api.interruptSlot(activeSlot, queueId).catch(() => {})
  }, [activeSlot])

  const handleEditQueued = useCallback((queueId: string, content: string) => {
    if (!activeSlot) return
    const trimmed = content.trim()
    if (!trimmed) return
    // Optimistically update the card; WS event reconciles other clients
    dispatch(editQueuedMessage({ slot: activeSlot, queue_id: queueId, content: trimmed }))
    api.editQueuedMessage(activeSlot, queueId, trimmed).catch(() => {})
  }, [activeSlot, dispatch])


  // Search: map message index → displayItems index for scroll-to-match
  const messageToDisplayIdx = useMemo(() => {
    const map = new Map<number, number>()
    displayItems.forEach((item, di) => {
      if (item.kind === 'turn') {
        for (const ti of item.items) {
          if (ti.kind === 'single') map.set(ti.idx, di)
          else if (ti.kind === 'group') ti.msgs.forEach((_, mi) => map.set(ti.startIdx + mi, di))
        }
      } else if (item.kind === 'single') map.set(item.idx, di)
      else if (item.kind === 'group') item.msgs.forEach((_, mi) => map.set(item.startIdx + mi, di))
    })
    return map
  }, [displayItems])

  const chatNav = useChatNavigation(messages, messageToDisplayIdx)

  // Track the timestamp of the previous search-nav step so we can tell "user is
  // holding Enter through many matches" apart from "user landed on one match".
  // Rapid consecutive steps snap instantly (behavior:'auto') — a smooth glide
  // would be interrupted and restarted on every keypress, producing the stutter
  // of half-finished eased scrolls. A lone step (or the final one after a pause)
  // glides smoothly and centers. navToDisplayIndex still forces 'auto' for FAR
  // jumps regardless; this only governs NEAR jumps, which is where the queued-
  // animation jank lived.
  const lastSearchStepAtRef = useRef(0)
  // Set when the user clicks a row in the results panel (vs. Enter/Arrow
  // stepping). A click is a direct jump that's usually FAR and to an unmeasured
  // virtualized row — a smooth scroll animates to the *estimated* offset and
  // then visibly corrects once the row mounts. Snapping instantly collapses
  // that into one jump.
  const searchClickJumpRef = useRef(false)
  // Cancel handle for the re-click converge loop (below) so repeated re-clicks
  // of the same result don't stack concurrent loops + window listeners.
  const reclickScrollCancelRef = useRef<(() => void) | null>(null)
  // Read the display-index map via a ref so the scroll effect below does NOT
  // re-fire when the map is rebuilt (every new message / stream chunk rebuilds
  // it). Otherwise an open search pane would yank the chat back to the current
  // match each time the agent emits output. The effect should scroll only on
  // deliberate search navigation (currentIdx / currentMessageIdx change).
  const messageToDisplayIdxRef = useRef(messageToDisplayIdx)
  messageToDisplayIdxRef.current = messageToDisplayIdx
  const jumpToSearchResult = useCallback((i: number) => {
    // Re-clicking the already-selected result won't change currentIdx, so the
    // nav effect won't fire — scroll back to it imperatively so a click always
    // returns to the match even after the user has scrolled away from it.
    if (i === search.currentIdx) {
      const m = search.matches[i]
      const di = m ? messageToDisplayIdxRef.current.get(m.msgIdx) : undefined
      if (di !== undefined) {
        requestAnimationFrame(() => {
          navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
          // currentOcc is unchanged so the message's occurrence-scroll effect
          // won't re-run; converge-center the already-rendered active mark.
          reclickScrollCancelRef.current?.()
          reclickScrollCancelRef.current = scrollCurrentMatchIntoView()
        })
      }
      return
    }
    searchClickJumpRef.current = true
    search.goTo(i)
  }, [search, navToDisplayIndex])
  useEffect(() => {
    if (search.currentMessageIdx < 0) return
    const di = messageToDisplayIdxRef.current.get(search.currentMessageIdx)
    if (di === undefined) return
    const now = performance.now()
    const behavior = searchClickJumpRef.current
      ? 'auto'
      : pickSearchScrollBehavior(now, lastSearchStepAtRef.current)
    searchClickJumpRef.current = false
    lastSearchStepAtRef.current = now
    navToDisplayIndex(di, { behavior, align: 'center' })
  }, [search.currentMessageIdx, search.currentIdx, navToDisplayIndex])

  // "Show in chat" button on the approval bar dispatches openActivityToTool,
  // which sets `focusToolCallId`. Pulling a virtualised pill back into the DOM
  // requires Virtuoso's own scrollToIndex — direct DOM scrollIntoView fails
  // because the element doesn't exist. ToolCallLine's own effect then takes
  // over once it mounts: refines the scroll position and clears the focus.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (!focusToolCallId) return
    const msgIdx = messages.findIndex(m =>
      m.role === 'tool' && m.meta?.tool_call_id === focusToolCallId
    )
    if (msgIdx < 0) return
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
  }, [focusToolCallId, messages, messageToDisplayIdx, navToDisplayIndex])

  // Deep-link: scroll to ?msg= timestamp on cold load.
  // The scroll-to-bottom effect above is suppressed while initialMsgRef is set.
  // Safety net: clear initialMsgRef after 5s to restore scroll-to-bottom if deep-link fails.
  useEffect(() => {
    if (!initialMsgRef.current) return
    const timer = setTimeout(() => { initialMsgRef.current = null }, 5000)
    return () => clearTimeout(timer)
  }, [])
  useEffect(() => {
    const targetTs = initialMsgRef.current
    if (!targetTs || messages.length === 0) return
    const msgIdx = messages.findIndex(m => m.ts === targetTs)
    if (msgIdx < 0) return
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    initialMsgRef.current = null
    setTimeout(() => {
      navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
      setHighlightTs(targetTs)
      setTimeout(() => setHighlightTs(null), 3000)
    }, 500)
  }, [messages, messageToDisplayIdx]) // eslint-disable-line react-hooks/exhaustive-deps

  // Precomputed O(n) map from message index → visible (user/assistant) index,
  // used by the fork button. Replaces per-row O(i) filter that made the
  // renderer O(n²) overall.
  const visibleIndexMap = useMemo(() => {
    const map = new Map<number, number>()
    let count = 0
    for (let idx = 0; idx < messages.length; idx++) {
      const r = messages[idx].role
      if (r === 'user' || r === 'assistant') {
        map.set(idx, count)
        count++
      }
    }
    return map
  }, [messages])

  const activeSlotTitle = filteredSlots.find(s => s.key === activeSlot)?.title

  // Session documents (in-session artifacts) for the active slot. Used only to
  // badge file-change rows that are tracked docs/artifacts (e.g. a generated
  // PR body) rather than source-file edits. Shares the ['session-artifacts',
  // slot] query key with the Artifacts tab so it's a single deduped fetch; the
  // memoized Set keeps AssistantMessage's memo stable across renders.
  const { data: sessionDocs } = useQuery({
    queryKey: ['session-artifacts', activeSlot],
    queryFn: () => api.artifactSessionDocs(activeSlot || undefined),
    enabled: !!activeSlot,
    staleTime: 15_000,
  })
  const artifactPaths = useMemo(
    () => new Set((sessionDocs?.docs || []).map(d => d.path)),
    [sessionDocs],
  )

  const renderMessage = useCallback((i: number, m: ChatMessage) => {
    // Prefer the optimistic client ts (stashed by the steer-echo reconcile in
    // chatSlice.appendSlotMessage) over the server ts for the React key. A
    // steered bubble is appended optimistically with a client ts, then the
    // steer_push echo overwrites ts with the server ts — keying on ts alone
    // would change the key mid-stream, remounting the bubble and replaying its
    // one-shot entrance animation. clientTs keeps the key (and DOM) stable.
    const keyTs = (m.meta?.clientTs as string | undefined) || m.ts
    const key = keyTs ? `${m.role}-${keyTs}` : `${m.role}-${i}`
    if (m.role === 'thinking') return m.content ? <ThinkingBlock key={key} content={m.content} /> : null
    if (m.role === 'tool') {
      // Skip ✅/🚫 completion messages — completion shown via CircleCheckBig icon
      if (!m.content.startsWith('🔧')) return null
      // A workflow_run launch renders as a persistent, clickable inline card
      // (live status + open-panel affordance) instead of the generic tool pill.
      const wfRunId = extractWorkflowRunId(m)
      if (wfRunId) return <WorkflowRunCard key={key} runId={wfRunId} message={m} />
      // Likewise a spawn_run launch: the transient chip above the composer
      // drops when the wave ends and only covers the viewed slot, so without
      // this the only record of a spawn is a pill folded into "Worked through
      // N steps".
      const spawnLaunch = extractSpawnRunLaunch(m)
      if (spawnLaunch) return <SubagentRunCard key={key} launch={spawnLaunch} slot={activeSlot || ''} />
      // Animate tools in the trailing group (after last assistant/streaming text)
      const isInTrailingGroup = slotState === 'tool_running' && i > lastTextIdx
      return <ToolCallLine key={key} message={m} running={isInTrailingGroup} onFileOpen={handleFileOpen} />
    }
    if (m.role === 'file') {
      try {
        const f = JSON.parse(m.content)
        return <FileCard key={key} file={f} />
      } catch { /* fall through to default */ }
    }
    if (m.role === 'queued') return null
    // Auto-nudge turns are machine-facing instruction blobs — collapse them to
    // a compact chip instead of rendering the whole payload as a chat bubble.
    // The Loop button is offered only when this row's own loop is the one still
    // bound to the slot, so a historical card never opens a successor loop's
    // controls.
    if (m.role === 'nudge') {
      const ownLoop = nudgeMatchesLoop(m, autoNudgeLoop?.id)
      return <NudgeCard key={key} message={m} onOpenLoop={ownLoop ? () => setAutoNudgeOpen(true) : undefined} />
    }
    if (m.kind === 'stop_event' || m.meta?.kind === 'stop_event') return <StopEventCard key={m.meta?.id as string ?? key} message={m} />
    // A synthetic turn-recovery continuation (tool refusal / stalled turn /
    // stalled tool) is machine-facing instruction text. It stays in the
    // transcript for auditability, but as a one-line card that names the event
    // and the deny pattern rather than a full-width bubble of prompt prose.
    if (m.role === 'inject') {
      const recovery = parseRecoveryMessage(m.content)
      if (recovery) return <RecoveryCard key={key} parsed={recovery} />
    }
    if (m.role === 'error') return <div key={key} className="bg-danger-subtle text-danger text-[13px] px-3 py-2 rounded-md border border-danger/15 self-center animate-scale-in">{m.content}</div>
    if (m.role === 'notice') return <div key={key} className="bg-card text-muted text-[13px] px-3 py-2 rounded-md border border-border self-center animate-scale-in">{m.content}</div>
    if (m.role === 'permission') return null
    if (m.role === 'mcp_oauth') {
      const banner = renderMcpOAuthMessage(m)
      return banner ? <div key={key}>{banner}</div> : null
    }
    // An injected workflow completion event renders as a compact status card
    // (with the full result folded away) instead of a wall of raw JSON.
    if (isWorkflowCompletionMessage(m)) return <WorkflowCompletionCard key={key} message={m} onFileOpen={handleFileOpen} />
    const isUser = m.role === 'user'
    const isStreaming = m.role === 'streaming'
    const isInject = m.role === 'inject'
    // Pass a stable handleFork (useCallback) + primitive index so memo()
    // on AssistantMessage can short-circuit when only unrelated state changes
    // (raised in review). visibleIndexMap is O(1) per row.
    const canFork = !isStreaming && !isInject && !slotHasMore
    const forkIndex = canFork ? visibleIndexMap.get(i) : undefined
    const msgTime = m.ts ? new Date(m.ts).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''
    return (
      <MessageSearchScope key={key} messageIdx={i}>
      <div className={`group flex flex-col min-w-0 ${isUser ? 'items-end' : ''} ${m.ts && m.ts === highlightTs ? 'animate-msg-highlight rounded-lg' : ''}`}>
        <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden ${isUser ? 'items-end' : ''}`}>
          {isUser ? (
            <UserMessage
              content={m.content}
              meta={m.meta}
              timestamp={chatConfig.showTimestamps ? msgTime : undefined}
              renderContent={renderUserContentCb}
              canEdit={!slotRunning && !regenerating && !!activeSlot}
              messageIndex={i}
              messageTs={m.ts || ''}
              onEditResend={handleEditResend}
              slotKey={activeSlot || undefined}
              slotTitle={activeSlotTitle}
              mode={mode}
            />
          ) : isInject ? (
            (() => {
              const cronLabel = (m.meta?.cronLabel as string) || ''
              // Strip wrapper tags — LLM needs them for context but user sees clean content
              const cleanContent = cronLabel
                ? m.content.replace(/^\[Cron notification from ".*"\]\n/, '').replace(/\n\[End of cron notification\]$/, '')
                : m.content
              return <>
                {cronLabel && <span className="text-muted text-[11px] font-medium px-1 mb-0.5"><Clock className="lucide-inline" /> {cronLabel}</span>}
                <div className="msg-content px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap rounded-lg bg-warning-subtle text-fg border border-warning/30 rounded-bl-[4px] overflow-hidden min-w-0" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}><MessageErrorBoundary rawContent={cleanContent}><MarkdownRenderer content={cleanContent} /></MessageErrorBoundary></div>
                {chatConfig.showTimestamps && msgTime && <span className="text-muted text-[12px] font-mono px-1">{msgTime}</span>}
              </>
            })()
          ) : (
            <div className="flex flex-col gap-0">
              <AssistantMessage content={m.content} isStreaming={isStreaming} isRegenerating={regenerating && i === lastTextIdx} onFileOpen={handleFileOpen} onArtifactOpen={handleArtifactOpen} onQuote={handleQuote} onAsk={handleAsk} slotRunning={slotRunning} planTaskId={planTaskId} timestamp={chatConfig.showTimestamps ? msgTime : undefined} messageTs={m.ts} slotKey={activeSlot || undefined} slotTitle={activeSlotTitle} mode={mode} fileChanges={(m.meta as Record<string, unknown> | undefined)?.file_changes as FileChangeEntry[] | undefined} turnStats={chatConfig.showTurnStats ? (m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined : undefined} onOpenDiff={handleOpenDiff} fileChipStyle={chatConfig.fileChipStyle} artifactPaths={artifactPaths} showFooter={(() => {
                // Show footer on the last assistant message of each completed turn
                if (isStreaming) return false
                // Find next message after this one that's assistant, user, or streaming
                for (let j = i + 1; j < messages.length; j++) {
                  if (messages[j].role === 'user') return true // end of turn — show footer
                  if (messages[j].role === 'assistant' || messages[j].role === 'streaming') return false // not last assistant in turn
                }
                // End of messages — show footer only if agent is done
                return !slotRunning
              })()} onSpeak={handleSpeak} onRegenerate={i === lastTextIdx && !slotRunning && !regenerating && activeSlot ? handleRegenerate : undefined} variants={m.variants} variantIdx={m.variant_idx} onSwitchVariant={i === lastTextIdx && m.variants && m.variants.length > 1 && activeSlot ? (idx: number) => { api.switchVariant(activeSlot, idx).catch((e: unknown) => {
                // eslint-disable-next-line no-console -- surface switch-variant failures for debugging
                console.warn('switch-variant failed', e)
              }) } : undefined} onFork={handleFork} onPlanFromHere={handlePlanFromHere} forkIndex={forkIndex} onApplyPlan={handleApplyPlan} />
            </div>
          )}
        </div>
      </div>
      </MessageSearchScope>
    )
    // dispatch/navigate are stable; handleOpenDiff/handlePlanFromHere are
    // memoized callbacks; planTaskId is read when rendering the plan footer /
    // apply-plan handler, so it belongs here for correctness. approve/send/
    // dismissApproval are NOT referenced in this renderer (user/approval rows go
    // through renderUserContentCb), so they are omitted to keep it stable.
  }, [messages, visibleIndexMap, slotRunning, slotState, lastTextIdx, handleFileOpen, handleArtifactOpen, handleFork, handleQuote, handleAsk, chatConfig, activeSlot, regenerating, handleRegenerate, handleEditResend, slotHasMore, renderUserContentCb, highlightTs, activeSlotTitle, mode, dispatch, handleOpenDiff, handlePlanFromHere, navigate, planTaskId, artifactPaths, autoNudgeLoop])

  const [mobileSessions, setMobileSessions] = useState(false)
  // Close mobile sessions panel when a session is selected
  useEffect(() => { if (isMobile) setMobileSessions(false) }, [activeSlot]) // eslint-disable-line react-hooks/exhaustive-deps
  // Reset mobile sessions state when leaving mobile viewport
  useEffect(() => { if (!isMobile) setMobileSessions(false) }, [isMobile])
  // Swipe from left edge to open sidebar, swipe left on backdrop to close
  const chatContainerRef = useRef<HTMLDivElement>(null)
  // Measured container height — used to size the sidebar border-box morph
  // (the panel rect it grows from / shrinks into the expand button).
  const [containerH, setContainerH] = useState(0)
  useEffect(() => {
    const el = chatContainerRef.current
    if (!el) return
    const measure = () => setContainerH(el.clientHeight)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  // Full-height activity bar slot in the App shell grid (desktop dashboard
  // only): the Activity panel portals into it so it spans the window
  // top-to-bottom. The header row ends at the slot's left edge,
  // so the top-bar right cluster (capsule, terminal, bell, gear) shifts left
  // when the panel opens. Null on mobile / embed frames -> inline fallback.
  //
  // Seed the portal slot SYNCHRONOUSLY so the very first render after a
  // ChatPage remount (e.g. switching back to /chat) already targets the
  // full-height actbar grid column. An effect-only seed leaves activitySlot
  // null for render 1, which falls back to the inline panel (rendered below
  // the header) and then flashes: below-header -> disappear -> portal opens.
  // The App shell (and its #activity-bar-slot) lives outside the router, so on
  // route-nav back it's already in the DOM. The effect below stays as the
  // fallback for cold load / mobile->desktop crossings where it isn't yet.
  const [activitySlot, setActivitySlot] = useState<HTMLElement | null>(
    () => (isMobile || embedMode) ? null : document.getElementById('activity-bar-slot'),
  )
  useEffect(() => {
    if (isMobile || embedMode) { setActivitySlot(null); return }
    const el = document.getElementById('activity-bar-slot')
    if (el) { setActivitySlot(el); return }
    // Slot not in the DOM yet. On a mobile -> desktop crossing, this
    // component's media-query subscription can flush (and run this effect)
    // before the App shell re-renders the slot div -- a one-shot lookup here
    // would miss it forever and strand the panel on the inline fallback
    // (rendering below the header instead of in the full-height column).
    // Watch the DOM until the slot appears, then latch it and stop.
    setActivitySlot(null)
    const mo = new MutationObserver(() => {
      const found = document.getElementById('activity-bar-slot')
      if (found) { setActivitySlot(found); mo.disconnect() }
    })
    mo.observe(document.body, { childList: true, subtree: true })
    return () => mo.disconnect()
  }, [isMobile, embedMode])
  const openSidebar = useCallback(() => setMobileSessions(true), [])
  const closeSidebar = useCallback(() => setMobileSessions(false), [])
  useSwipeEdge(chatContainerRef, { enabled: isMobile && !mobileSessions, edge: 'left', edgeZone: 0.35, onSwipe: openSidebar })
  useSwipeEdge(chatContainerRef, { enabled: isMobile && mobileSessions, edge: 'right', threshold: 50, edgeZone: 9999, onSwipe: closeSidebar })
  // Web Preview "focus" (expand) mode — broadcast by the Web Preview tab's
  // expand toggle. When on, hide the session list (below) and maximize the side
  // panel (passed to SidePanel), so the preview gets max room and chat shrinks
  // to its minimum. App collapses the left nav off the same event.
  const [previewFocused, setPreviewFocused] = useState(false)
  useEffect(() => {
    const onFocus = (e: Event) => setPreviewFocused(!!(e as CustomEvent<{ focused?: boolean }>).detail?.focused)
    window.addEventListener(PREVIEW_FOCUS_EVENT, onFocus)
    return () => window.removeEventListener(PREVIEW_FOCUS_EVENT, onFocus)
  }, [])
  const sidebarOpen = !previewFocused && (isMobile ? mobileSessions : (sidebarPinned || filteredSlots.length === 0))
  useEffect(() => {
    if (filteredSlots.length === 0 && !sidebarPinned) {
      setSidebarPinned(true)
      safeSetItem('mc-sidebar-pinned', 'true')
    }
  }, [filteredSlots.length, sidebarPinned])

  // Horizontal space (px) the detail panel must keep clear so it never grows
  // past its flex row and collapses the chat pane: the open sidebar's width
  // plus a usable chat-pane minimum. On mobile the panel is full-screen (no
  // shared row), so no reserve applies.
  const CHAT_PANE_MIN = CHAT_PANE_MIN_W
  const panelReserve = isMobile ? undefined : (sidebarOpen ? sidebarWidth : 0) + CHAT_PANE_MIN

  // FILL vs BESIDE for the activity panel, decided from the width left for the
  // CHAT once the shell's hideable chrome is subtracted — the nav rail track and
  // the session sidebar (a shrink-0 flex sibling of exactly sidebarWidth; on
  // mobile its drawer is fixed-position and consumes no row width). Undefined =
  // beside. A px width = fill the chat column, squeezing the chat pane to zero
  // while the rail and sidebar stay exactly where they are.
  //
  // The panel's render PATH is unchanged either way, so crossing the threshold
  // never remounts it (no terminal re-attach, no Virtuoso churn) — only its
  // width changes. See sidePanelFillWidth for why this is loop-free.
  const panelFillWidth = sidePanelFillWidth({
    winW,
    railW: railWidth,
    sidebarW: !isMobile && sidebarOpen ? sidebarWidth : 0,
    isMobile,
  })

  return (
    <TagPopoverProvider>
    <div ref={chatContainerRef} className="flex flex-1 min-h-0 h-full overflow-hidden relative">
      <AnimatePresence>
        {isMobile && mobileSessions && (
          <motion.div
            key="sessions-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-sm"
            onClick={() => setMobileSessions(false)}
          />
        )}
      </AnimatePresence>
      {/* Sidebar collapse/expand control. Two stacked pieces, both in the
          stable container (not inside the morphing panel):
          1. Morph border-box: a crisp bordered box (no content, so border &
             radius stay sharp) that grows from the button's rect to the
             panel's rect on open and shrinks back on collapse — the panel's
             border becoming the button's box.
          2. Icon button: fixed at the button's spot, flips icon + action.
          Desktop, non-embed, with sessions only. */}
      {!isMobile && embedMode !== 'chat' && embedMode !== 'sessions' && filteredSlots.length > 0 && (() => {
        const RADIUS = 12 // same as the panel's rounded-xl — constant through the morph
        const PANEL = { top: 0, left: 0, width: sidebarWidth, height: Math.max(0, containerH - 8), borderRadius: RADIUS, opacity: 1 }
        const BTN = { top: 12, left: 8, width: 34, height: 34, borderRadius: RADIUS, opacity: 1 }
        const MORPH = { duration: 0.32, ease: [0.32, 0.72, 0, 1] as const }
        return (
          <>
            <AnimatePresence initial={false}>
              {!sidebarOpen && (
                <motion.div
                  key="sidebar-morph-box"
                  aria-hidden="true"
                  className="absolute z-[60] bg-bg-elevated border border-border shadow-md pointer-events-none"
                  initial={PANEL}
                  animate={BTN}
                  exit={{ ...PANEL, opacity: 0 }}
                  transition={MORPH}
                />
              )}
            </AnimatePresence>
            <button
              type="button"
              onClick={() => window.dispatchEvent(new CustomEvent('toggle-pin-chat-sidebar'))}
              className="absolute top-[12px] left-2 z-[61] w-[34px] h-[34px] rounded-xl flex items-center justify-center cursor-pointer text-muted hover:text-text transition-colors bg-transparent border-none"
              title={sidebarOpen ? i18nT('pages.chatPage.hide_sessions') : i18nT('pages.chatPage.show_sessions')}
              aria-label={sidebarOpen ? i18nT('pages.chatPage.hide_sessions_sidebar') : i18nT('pages.chatPage.show_sessions_sidebar')}
            >
              {sidebarOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
            </button>
          </>
        )
      })()}
      {embedMode === 'chat' ? null : embedMode === 'sessions' ? (
        <div className="flex-1 min-w-0 h-full overflow-hidden [&_.sidebar-inner]:!w-full [&_.sidebar-inner]:!border-0 [&_.sidebar-inner]:!rounded-none [&_.sidebar-inner]:!shrink [&_.sidebar-inner]:!bg-bg [&_.sidebar-resize-handle]:!hidden">
          <ChatSidebar
            slots={filteredSlots}
            activeSlot={null}
            unreadSlots={surfaceUnreadSlots}
            history={history}
            historyHasMore={historyHasMore}
            defaultAgent={defaultAgent}
            installedAgents={installedAgents}
            mode={mode}
            onWidthChange={setSidebarWidth}
            onDragChange={setSidebarDragging}
            onSelectSlot={(key) => navigate(`/embed/chat/${key}`)}
          />
        </div>
      ) : (
      <OverlayDrawer open={sidebarOpen} width={isMobile ? window.innerWidth : sidebarWidth} dragging={sidebarDragging} morph={!isMobile} className={isMobile ? 'mobile-sessions-overlay fixed top-[42px] bottom-0 left-0 z-50 bg-bg-elevated !py-0 rounded-r-xl shadow-lg max-w-[calc(100vw-2.5rem)] [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0' : ''}>
        <ChatSidebar
          slots={filteredSlots}
          activeSlot={activeSlot}
          unreadSlots={surfaceUnreadSlots}
          history={history}
          historyHasMore={historyHasMore}
          defaultAgent={defaultAgent}
          installedAgents={installedAgents}
          mode={mode}
          onWidthChange={setSidebarWidth}
          onDragChange={setSidebarDragging}
          collapsible={!isMobile}
          splitEnabled={splitFeatureEnabled}
          splitActive={splitMode}
          onOpenSplit={() => enterSplit(activeSlot)}
          onSelectSlot={() => setSplitMode(false)}
        />
      </OverlayDrawer>
      )}

      {/* Per-slot tag picker — a single connected popover, opened from any session
          menu (sidebar row or header) via the ChatPage-scoped TagPopover context. */}
      <SlotTagPopover />

      {/* Chat pane */}
      {embedMode !== 'sessions' && (
      <div className={`relative flex flex-col bg-bg min-w-0 min-h-0 h-full overflow-hidden ${(activityOpen && !activitySlot) || search.isOpen ? 'flex-[1_1_60%]' : 'flex-1'}`} style={{ transition: 'flex 0.2s', ...(!sidebarOpen && !isMobile ? { marginLeft: '-0.5rem' } : {}), '--mc-content-width': CONTENT_WIDTH[chatConfig.contentWidth].messages, '--mc-input-width': CONTENT_WIDTH[chatConfig.contentWidth].input } as React.CSSProperties}>
        {snipFrame && (
          <SnipOverlay
            frame={snipFrame}
            onComplete={f => { uploadFiles([f], snipSlotRef.current); setSnipFrame(null) }}
            onCancel={() => setSnipFrame(null)}
            onError={setUploadError}
          />
        )}
        {uploadError && (
          <div className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--danger) 45%, transparent)' }}>
            <span className="text-sm text-text flex-1">{uploadError}</span>
            <button onClick={() => setUploadError('')} aria-label={i18nT('pages.chatPage.dismiss_upload_error')} className="text-muted hover:text-text text-lg leading-none">&times;</button>
          </div>
        )}
        {sidError && (
          <div className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
            <span className="text-sm text-text flex-1">{sidError}</span>
            <button onClick={() => setSidError('')} aria-label={i18nT('pages.chatPage.dismiss_error')} className="text-muted hover:text-text text-lg leading-none">&times;</button>
          </div>
        )}
        {isMobile && !sidebarOpen && !(activeSlot && (messages.length > 0 || slotRunning)) && (
          <div className="fixed top-[42px] left-2 z-10">
            <button className="p-2 rounded-lg text-muted hover:text-text bg-bg-elevated border border-border shadow-sm cursor-pointer" onClick={() => setMobileSessions(true)} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
              {effectiveMode === 'orchestrator' ? <MessageSquareDot size={18} /> : <MessageSquare size={18} />}
            </button>
          </div>
        )}
        {splitMode && splitFeatureEnabled ? (
          <SessionGridView
            seedSlot={splitAnchor ?? activeSlot}
            onClose={() => setSplitMode(false)}
            onCollapse={(slot) => { dispatch(switchSlot(slot)); setSplitMode(false) }}
          />
        ) : !activeSlot ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-4 px-8">
            <EmptyState icon={<MessageSquare className="lucide-inline" />} title={i18nT('pages.chatPage.what_can_i_do_for_you')} subtitle={i18nT('pages.chatPage.start_a_new_chat_to_begin')} />
            <Btn primary onClick={() => dispatch(createSlot({ agent: pendingAgent || defaultAgent || undefined, model: pendingModel || undefined, mode }))}>{i18nT('pages.chatPage.start_a_new_chat')}</Btn>
          </div>
        ) : (
          <SearchHighlightContext.Provider value={searchCtxValue}>
          <div className="relative flex flex-col flex-1 min-h-0">
            {/* Claude-style title row — absolute overlay, solid top fading to transparent.
                Inset on the right by the 6px scrollbar width (see ::-webkit-scrollbar
                in index.css) so the overlay never paints over the scroller's scrollbar
                track — otherwise the thumb is hidden/un-grabbable when scrolled to top. */}
            <div className="absolute top-0 left-0 right-1.5 z-10 pointer-events-none" style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}>
              <div className={`relative pr-5 pt-1 pb-2 flex items-center gap-2 bg-bg pointer-events-none ${!sidebarOpen && !isMobile ? 'pl-14' : 'pl-5'}`}>
                {embedMode !== 'chat' && isMobile && (
                  <button className="p-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none pointer-events-auto" onClick={() => setMobileSessions(p => !p)} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
                    {effectiveMode === 'orchestrator' ? <MessageSquareDot size={16} /> : <MessageSquare size={16} />}
                  </button>
                )}
                <div className="group/header flex items-stretch gap-0.5 pointer-events-auto">
                <div className="rounded-l-md rounded-r-[2px] px-1.5 py-0.5 group-hover/header:bg-bg-hover transition-colors">
                <ChatHeaderMenu
                  activeSlot={activeSlot}
                  agent={currentSlot?.agent}
                  onReveal={activeSlot ? () => { if (!sidebarPinned) setSidebarPinned(true); window.dispatchEvent(new CustomEvent('reveal-slot', { detail: activeSlot })) } : undefined}
                  onRename={activeSlot ? () => { setEditingTitle(true); setTitleDraft(title) } : undefined}
                  mode={effectiveMode}
                />
                </div>
              {editingTitle ? (
                <div className="flex w-fit items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md bg-bg-hover">
                  {currentSlot?.memory_mode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
                  {currentSlot?.memory_mode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
                  <Input className="session-header-title text-sm font-semibold text-muted font-body bg-transparent border-0 rounded-none p-0 m-0 flex-none outline-none max-w-[50vw] focus:!shadow-none" size={Math.min(Math.max(titleDraft.length + 2, 6), 80)} autoFocus value={titleDraft} onChange={e => setTitleDraft(e.target.value)} onBlur={() => { if (!cancelTitleRef.current && titleDraft.trim() && activeSlot && titleDraft !== title) { dispatch(sseSlotTitle({ key: activeSlot, title: titleDraft.trim() })); api.renameSlot(activeSlot, titleDraft.trim()).catch(() => {}) } cancelTitleRef.current = false; setEditingTitle(false) }} onCompositionStart={() => { composingRef.current = true }} onCompositionEnd={() => { composingRef.current = true; setTimeout(() => { composingRef.current = false }, 50) }} onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing && !composingRef.current) (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') { cancelTitleRef.current = true; setEditingTitle(false) } }} />
                </div>
              ) : (
                <div className="cursor-text flex items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md group-hover/header:bg-bg-hover transition-colors">
                  <Clickable className="flex items-center gap-1" onClick={() => { if (activeSlot && generatingTitleSlots.has(activeSlot)) return; setEditingTitle(true); setTitleDraft(title) }}>
                    {currentSlot?.memory_mode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
                    {currentSlot?.memory_mode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
                    <TypewriterText text={title} className="session-header-title text-sm font-semibold text-muted font-body truncate max-w-[50vw]" />
                    <Pen size={13} className="shrink-0 text-muted opacity-0 group-hover/header:opacity-60 transition-opacity" />
                  </Clickable>
                  {activeSlot && (generatingTitleSlots.has(activeSlot) ? <Loader size={16} className="shrink-0 text-accent animate-spin" /> : <Btn aria-label={i18nT('pages.chatPage.regenerate_title_with_llm')} className="shrink-0 text-muted opacity-0 group-hover/header:opacity-40 hover:!opacity-100 hover:text-accent transition-all cursor-pointer bg-transparent border-none p-0" title={i18nT('pages.chatPage.regenerate_title_with_llm')} onClick={e => { e.stopPropagation(); if (!activeSlot || generatingTitleSlots.has(activeSlot)) return; const slot = activeSlot; setGeneratingTitleSlots(prev => new Set(prev).add(slot)); api.generateTitle(slot).then(r => { /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */ if (r.title) dispatch(sseSlotTitle({ key: slot, title: r.title })) }).catch(e => {
                    // eslint-disable-next-line no-console -- surface title-generation failures for debugging
                    console.warn('Failed to generate title:', e)
                  }).finally(() => setGeneratingTitleSlots(prev => { const next = new Set(prev); next.delete(slot); return next })) }}><Sparkles size={16} /></Btn>)}
                </div>
              )}
                </div>
              {effectiveMode === 'orchestrator' && <span className="pointer-events-auto"><InfoTip text={i18nT('pages.chatPage.autopilot_plans_before_executing_each_stage_need')} /></span>}
              <InboundLinkChip slotKey={activeSlot} />
              {/* Trailing controls grouped under a single ml-auto so multiple
                  right-aligned items don't each absorb free space (two ml-auto
                  siblings split the gap, parking the split icon mid-header). */}
              <div className="ml-auto flex items-center gap-1.5 pr-1.5 pointer-events-none">
              {/* Pop-out control, promoted to the title bar (menu items remain for
                  sidebar parity). Mirrors the split-view pattern to its left: a
                  dimmed icon to act, an accent chip when the state is active.
                  Inside the popout window itself the same spot carries Return. */}
              {popout ? (
                <Clickable className="flex items-center gap-1 text-muted hover:text-text transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded hover:bg-bg-hover" onClick={returnSelfToMain} title={i18nT('pages.chatPage.return_this_session_to_the_main_window')} aria-label={i18nT('pages.chatPage.return_to_main_window')}>
                  <Undo2 size={13} /> {i18nT('pages.chatPage.return')}
                </Clickable>
              ) : !embedMode && activeSlot && (activePoppedOut ? (
                <Clickable className="flex items-center gap-1 text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded" onClick={() => focusActivePopout(activeSlot)} title={i18nT('pages.chatPage.this_session_is_open_in_its_own_window_focus_it')} aria-label={i18nT('pages.chatPage.focus_popped_out_window')}>
                  <ExternalLink size={13} /> {i18nT('pages.chatPage.popped_out')}
                </Clickable>
              ) : (
                <Clickable className="flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 text-muted hover:text-text pointer-events-auto" onClick={() => openActivePopout(activeSlot, currentSlot?.title)} title={i18nT('pages.chatPage.pop_out_to_window')} aria-label={i18nT('pages.chatPage.pop_out_session_to_its_own_window')}>
                  <ExternalLink size={15} />
                </Clickable>
              ))}
              {/* Activity panel open toggle — relocated here from the top bar
                  (item 2.4) so opening the panel no longer narrows the now
                  full-width header. Shown only while the panel is closed; the
                  panel's own header carries the close button. Never disabled:
                  below the mobile breakpoint the panel opens full width, at or
                  above it opens beside the chat. There is no width at which
                  the button does nothing. */}
              {!embedMode && !popout && !activityOpen && (
                <Clickable
                  className="flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 pointer-events-auto text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                  onClick={toggleAct}
                  title={i18nT('pages.chatPage.open_activity_panel')}
                  aria-label={i18nT('pages.chatPage.open_activity_panel')}
                >
                  <PanelRight size={15} />
                </Clickable>
              )}
              {!embedMode && splitFeatureEnabled && (splitAnchorForActive && !activeIsSplitAnchor ? (
                <Clickable className="flex items-center gap-1 text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded" onClick={() => enterSplit(splitAnchorForActive)} title={i18nT('pages.chatPage.this_session_is_open_in_a_split_return_to_it')} aria-label={i18nT('pages.chatPage.return_to_split_view')}>
                <Columns2 size={13} /> {i18nT('pages.chatPage.in_split')}
              </Clickable>
              ) : (
                <Clickable className="opacity-40 hover:opacity-100 transition-opacity cursor-pointer pointer-events-auto" onClick={() => enterSplit(activeSlot)} title={i18nT('pages.chatPage.split_view_d')} aria-label={i18nT('pages.chatPage.enter_split_view')}>
                <Columns2 size={14} />
              </Clickable>
              ))}
              </div>
              {/* Header fade — softens content passing up into the opaque title
                  row, so it hangs off that row's bottom edge. Absolutely
                  positioned rather than in flow: as an in-flow sibling its 24px
                  consumed layout and pushed the pinned card that far off the
                  header. Out of flow it overlays the transcript instead, and the
                  pinned card (painted later, and positioned) sits above it. */}
              <div aria-hidden className="absolute top-full inset-x-0 h-6 bg-gradient-to-b from-bg to-transparent" />
              </div>
              {/* Fold sentinel — zero-height, always mounted. Its top edge is the
                  line the pinned prompt sticks to (see updatePinnedPrompt). */}
              <div ref={pinFoldRef} aria-hidden className="h-0" />
              {pinned && (
                <PinnedPrompt
                  text={pinned.text}
                  fullText={pinned.full}
                  pushUp={pinned.push}
                  bannerH={pinned.bannerH}
                  expanded={pinExpanded}
                  onToggleExpanded={() => setPinExpanded(p => !p)}
                  onJump={() => scrollToPinnedPrompt(pinned.idx)}
                  cardRef={pinCardRef}
                />
              )}
            </div>
            {slotLoading && (
              <div className="absolute inset-0 flex items-center justify-center z-20 pointer-events-none">
                <Loader size={20} className="animate-spin text-muted" />
              </div>
            )}
            {isWelcomeState ? (
              <motion.div
                key="welcome-hero"
                layout
                className="flex-1 flex flex-col items-center justify-center gap-6 px-8 min-h-0 overflow-y-auto"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
              >
                <WelcomeView
                  mode={currentSlot?.mode || mode}
                  setInput={setInput}
                  memoryMode={currentSlot?.memory_mode ?? 'persistent'}
                  cleanMode={currentSlot?.clean_mode}
                  onSwitchMode={async (newMode) => {
                    if (!activeSlot) return
                    // Create-first-then-delete: deleting the active slot first
                    // would make deleteSlot jump focus to a sibling. Creating
                    // first keeps the new slot active, so the delete skips the
                    // sibling navigation. Carry agent/project/folder/color so
                    // the recreated slot keeps its identity and placement.
                    const old = currentSlot
                    const opts = {
                      agent: old?.agent || defaultAgent || undefined,
                      model: old?.model || undefined,
                      mode,
                      memory_mode: newMode,
                      folder_id: old?.folder_id ?? null,
                      color_index: old?.color_index ?? null,
                      project: old?.project ?? null,
                    }
                    try { await dispatch(createSlot(opts)).unwrap() } catch { return }
                    try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch { /* new slot already active */ }
                  }}
                  onToggleClean={async (clean) => {
                    if (!activeSlot) return
                    const old = currentSlot
                    const opts = {
                      agent: old?.agent || defaultAgent || undefined,
                      model: old?.model || undefined,
                      mode,
                      clean_mode: clean,
                      folder_id: old?.folder_id ?? null,
                      color_index: old?.color_index ?? null,
                      project: old?.project ?? null,
                    }
                    try { await dispatch(createSlot(opts)).unwrap() } catch { return }
                    try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch { /* new slot already active */ }
                  }}
                />
              </motion.div>
            ) : (
            <div
              ref={scrollerRef}
              // stable theming hook 'chat-container' — see website/docs/theming-contract.md
              className="chat-container"
              style={{
                flex: 1,
                paddingBottom: 8,
                overflowY: 'auto',
                // Reserve a stable scrollbar gutter so the 6px scrollbar always
                // occupies the same right-edge column the title overlay is inset
                // from (see the right-1.5 inset above) — keeps the thumb visible
                // and grabbable at the top instead of hidden behind the header.
                scrollbarGutter: 'stable',
                // Native scroll anchoring: when items above the viewport
                // resize (e.g. widget iframes loading async), the browser
                // adjusts scrollTop to keep the user's content stable.
                // This is more precise than item-level anchoring because
                // it works at the DOM-element granularity.
                overflowAnchor: 'auto',
                // Keep wheel/touch momentum inside the message list. Without
                // this, a delta that arrives at the top or bottom edge chains
                // to the nearest scrollable ancestor — the document, which
                // `body{overflow-y:auto}` leaves scrollable — and drags the
                // whole app shell by however many pixels of slack exist
                // (a browser-extension node parked past the shell is enough).
                overscrollBehavior: 'contain',
              } as React.CSSProperties}
              aria-label={i18nT('pages.chatPage.chat_messages')}
              aria-live="polite"
              onScroll={onScrollPin}
            >
              {/* Header spacer */}
              <div className="h-16" />
              {/* Top sentinel: drives upward window expansion via virtualizer's IO. */}
              <div ref={virt.topSentinelRef} aria-hidden style={{ height: 1 }} />
              {/* Top spacer — reserves the height of all items above the mounted
                  window so the scrollbar stays accurate while only the window
                  renders real DOM (keeps fast scroll cheap — O(window) nodes).
                  overflow-anchor:none so the browser anchors on real content,
                  not on this spacer (which resizes as the window moves). */}
              <div aria-hidden style={{ height: virt.offsetBefore, overflowAnchor: 'none' }} />
              {/* Message items — only the mounted window renders; everything
                  else is represented by the top/bottom spacers. */}
              {visibleDisplayItems.map((vi) => {
                if (!vi.mounted) return null
                const item = vi.data
                const displayIdx = vi.index
                if (item.kind === 'turn') {
                  const renderTurnItem = (it: TurnItem, _j: number) => {
                    // Skip hidden tool messages (✅/🚫 completions) to avoid empty py-1 wrappers
                    if (it.kind === 'single' && it.msg.role === 'tool' && !it.msg.content.startsWith('🔧')) return null
                    return <div key={it.kind === 'single' ? (it.msg.ts || it.idx) : `g-${it.startIdx}`} className={`px-5 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                      {it.kind === 'group' ? (() => {
                        const unresolvedPerms = it.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                        // Skip group entirely if it only contains unresolved permissions (handled by ApprovalBar)
                        if (it.msgs.every(m => m.role === 'permission')) return null
                        return (
                        <CollapsibleToolGroup
                          count={it.msgs.filter(m => m.role !== 'permission').length}
                          hasPermission={false}
                          isRunning={false}
                          permissionMeta={unresolvedPerms.at(-1)?.meta as Record<string, unknown> | undefined}
                          pendingPermCount={unresolvedPerms.length}
                          onApprove={(() => {
                            const aid = unresolvedPerms.at(-1)?.meta?.approval_id as string | undefined
                            if (!aid) return approve
                            return async (action: string) => { await api.resolveApproval(aid, toApiDecision(action)); dismissApproval(aid) }
                          })()}
                          onViewActivity={toggleAct}
                          activityOpen={activityOpen}
                        >{it.msgs.map((m, j) => <div key={m.ts || j}>{renderMessage(it.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
                      })() : renderMessage(it.idx, it.msg)}
                    </div>
                  }
                  return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx}><TurnBlock turn={item} renderItem={renderTurnItem} collapseAll={chatConfig.collapseAllSteps} appToolCallIds={appToolCallIds} /></div>
                }
                return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx} className={`px-5 mx-auto w-full py-1`} style={{
                  maxWidth: 'var(--mc-content-width, 900px)',
                  // The pinned banner is styled as this row's own bubble and sits
                  // at the exact position and width the bubble had when it crossed
                  // the fold, so leaving both visible is what betrays them as two
                  // containers. Hide the real one (visibility, NOT display — the
                  // virtualizer must keep measuring its height or the transcript
                  // would reflow under the reader) and the bubble appears to simply
                  // stop travelling and stick.
                  visibility: pinned?.idx === displayIdx ? 'hidden' : undefined,
                }}>{item.kind === 'group' ? (() => {
                const unresolvedGroupPerms = item.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                if (item.msgs.every(m => m.role === 'permission')) return null
                return (
                <CollapsibleToolGroup
                  count={item.msgs.filter(m => m.role !== 'permission').length}
                  hasPermission={false}
                  isRunning={slotRunning && displayIdx === displayItems.length - 1}
                  permissionMeta={unresolvedGroupPerms.at(-1)?.meta as Record<string, unknown> | undefined}
                  pendingPermCount={unresolvedGroupPerms.length}
                  onApprove={(() => {
                    const aid = unresolvedGroupPerms.at(-1)?.meta?.approval_id as string | undefined
                    if (!aid) return approve
                    return async (action: string) => {
                      await api.resolveApproval(aid, toApiDecision(action))
                      dismissApproval(aid)
                    }
                  })()}
                  onViewActivity={toggleAct}
                  activityOpen={activityOpen}
                >{item.msgs.map((m, j) => <div key={m.ts || j}>{renderMessage(item.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
              })() : renderMessage(item.idx, item.msg)}</div>
              })}
              {/* Bottom spacer — reserves the height of all items below the
                  mounted window. overflow-anchor:none (see top spacer). */}
              <div aria-hidden style={{ height: virt.offsetAfter, overflowAnchor: 'none' }} />
              {/* Bottom sentinel: drives downward window expansion when in jump mode. */}
              <div ref={virt.bottomSentinelRef} aria-hidden style={{ height: 1 }} />
              {/* Footer */}
              <ChatFooter running={slotRunning} stopping={slotStopping} state={slotState} lastRole={lastRole} regenerating={regenerating} stopState={currentSlot?.stop_state} />
              <div style={{height: '2vh'}} />
            </div>
            )}
            <div className="h-6 bg-gradient-to-t from-bg to-transparent pointer-events-none -mt-6 relative z-[1]" />
            <div className="relative">
              {!isAtBottom && messages.length > 0 && (
                <div className="absolute -top-10 inset-x-0 z-10 pointer-events-none flex justify-center">
                  <button
                    className="w-8 h-8 rounded-full flex items-center justify-center cursor-pointer pointer-events-auto transition-all duration-200 bg-bg-elevated border border-border-strong text-text hover:bg-bg-hover hover:border-accent hover:scale-[1.06] active:scale-95 active:duration-75 shadow-md"
                    onClick={() => { isAtBottomRef.current = true; scrollBottom(true) }}
                    aria-label={i18nT('pages.chatPage.scroll_to_bottom')}
                  ><ArrowDown size={14} strokeWidth={2.5} /></button>
                </div>
              )}
              {/* Not gated on activityOpen (unlike the two bars below): the
                  activity sidebar has no TODO view, so hiding it there would
                  lose the information rather than de-duplicate it. */}
              <TaskProgressBar slot={activeSlot} />
              {!activityOpen && <SubagentProgressBar slot={activeSlot} />}
              {!activityOpen && <WorkflowProgressBar slot={activeSlot} />}
              <SubagentDeliveryProgress count={systemDeliveryCount} />
              <QueueStack messages={queuedMessages} onCancel={handleCancelQueued} onInterrupt={handleInterruptQueued} onEdit={handleEditQueued} fuseBelow={followUpOptions.length === 0 && !knowledgeFetch.pendingKnowledge} />
              {flyingQuote && <FlyingQuote text={flyingQuote.text} from={flyingQuote.from} targetRef={inputAreaRef} onComplete={() => setFlyingQuote(null)} />}
              <div ref={inputAreaRef} className="relative z-10">
              {showHistorySuggestions && (
                <div className="absolute left-0 right-0 bottom-full mb-1 mx-auto w-full max-w-[760px] border border-border rounded-lg bg-card overflow-hidden animate-scale-in z-50 shadow-lg flex flex-col max-h-[min(300px,40vh)]">
                  <div className="px-3.5 py-2.5 border-b border-border shrink-0">
                    <span className="text-[12px] font-semibold text-muted tracking-[.02em]">{i18nT('pages.chatPage.continue_a_previous_chat')}</span>
                  </div>
                  <div className="overflow-y-auto flex-1 min-h-0" role="listbox" aria-label={i18nT('pages.chatPage.previous_chats')}>
                    {historySuggestions.map((s) => (
                      <div
                        key={s.key}
                        role="option"
                        tabIndex={0}
                        aria-selected={false}
                        className="w-full text-left px-3.5 py-2.5 flex items-center gap-3 cursor-pointer transition-all border-b border-border last:border-0 hover:bg-bg-hover"
                        onMouseDown={(e) => { e.preventDefault(); handleResumeSession(s.key, s.title || s.key) }}
                        onKeyDown={(e) => { if (e.key === 'Enter') handleResumeSession(s.key, s.title || s.key) }}
                      >
                        <div className="flex-1 min-w-0">
                          <div className="font-mono text-[13px] text-text truncate">{s.title || s.key}</div>
                          {s.created && <div className="text-[11px] text-muted font-mono mt-0.5">{new Date(s.created).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })}</div>}
                        </div>
                        <Undo2 size={14} className="text-accent shrink-0" />
                      </div>
                    ))}
                  </div>
                  <div className="px-3.5 py-2 border-t border-border flex justify-end shrink-0">
                    <span className="text-[11px] text-muted-strong">{i18nT('pages.chatPage.esc_to_dismiss')}</span>
                  </div>
                </div>
              )}
              {knowledgeFetch.results.length > 0 || knowledgeFetch.loading ? (
                <KnowledgePicker
                  results={knowledgeFetch.results}
                  query={knowledgeFetch.query}
                  loading={knowledgeFetch.loading}
                  onInject={(selected) => {
                    knowledgeFetch.inject(selected)
                  }}
                  onSkip={() => knowledgeFetch.clearResults()}
                />
              ) : null}
              {pendingQuestion && (
                <div className="px-5 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                  <PendingQuestionCard
                    slotKey={activeSlot}
                    onFallbackSend={(text) => {
                      // A 404 means the blocked wait is gone and the card has
                      // already cleared. Keep the user's answer in the composer
                      // for an explicit retry instead of auto-sending: even with
                      // a live WS, /api/chat can resolve with an HTTP error (for
                      // example Kiro becoming unavailable), which would otherwise
                      // leave the answer only in a non-persisted optimistic bubble.
                      setInput((prev) => (prev.trim() ? `${prev}\n${text}` : text))
                    }}
                    onDirectSend={(text) => {
                      // No-ask_id card: the card IS the interaction, so answer
                      // and send in one click (#755).
                      //
                      // Offline, send() bails at its own !connected guard and
                      // the card clears regardless — which would DROP the
                      // answer. Fall back to the composer so it survives, the
                      // same recovery the 404 path uses.
                      if (!connected) {
                        setInput((prev) => (prev.trim() ? `${prev}\n${text}` : text))
                        return
                      }
                      void send(text, activeSlot || undefined)
                    }}
                  />
                </div>
              )}
              {pendingFollowup && activeSlot && (
                <div className="px-5 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                  <FollowUpCard
                    items={pendingFollowup.items}
                    projectDir={currentSlot?.project || undefined}
                    onAddToSession={followupAddToSession}
                    onStartInWorktree={followupStartInWorktree}
                    onSkip={(index) => dispatch(dismissFollowupItem({ slot: activeSlot, index, ts: pendingFollowup.ts }))}
                  />
                </div>
              )}
              <ChatInput
              aboveComposer={
                /* In-flow tip inside the composer's own width wrapper: shares
                   the composer's exact box geometry (Raymond 2026-07-21: tip
                   width must always match the input box) while still pushing
                   chat content up like QueueStack (team decision: never cover
                   thinking/output; queue and question card keep priority via
                   tipSuppressed). */
                <AnimatePresence>
                  {activeTip && (
                    <div className="pb-1.5">
                      <TipCard tip={activeTip} onDismiss={dismissTip} />
                    </div>
                  )}
                </AnimatePresence>
              }
              value={input}
              onChange={setInput}
              onSend={() => send()}
              canSteer={slotRunning}
              onSteer={steer}
              onFollowUpSend={(text?: string) => send(text)}
              disabled={
                /* Streaming, compaction, and stopping all
                   keep the input interactive: api_chat queues on slot.running and
                   stop preserves the queue, so typing + Enter queues a
                   follow-up during the stop window instead of being silently blocked. */
                false
              }
              autoFocusKey={activeSlot}
              prefillHint={prefillHint}
              onDismissHint={() => setPrefillHint(false)}
              onScreenshot={handleCapture}
              onUploadFiles={uploadFiles}
              uploading={uploading}
              pendingFiles={pendingFiles}
              resizedInfo={resizedInfo}
              onRemoveFile={p => setPendingFiles(prev => prev.filter(x => x !== p))}
              onFileSelect={path => setPendingFiles(prev => prev.includes(path) ? prev : [...prev, path])}
              onFileOpen={handleFileOpen}
              project={currentSlot?.project || ''}
              projectBranch={projectBranch}
              projectDetached={!projectGitError && !!projectGit?.detached}
              isMac={isMac}
              onDrop={handleDrop}
              dragOver={dragOver}
              onDragOver={e => { e.preventDefault(); e.stopPropagation(); setDragOver(true) }}
              onDragLeave={e => { if (e.currentTarget === e.target) setDragOver(false) }}
              voiceRecording={voice.recording}
              voiceTranscribing={voice.transcribing}
              voiceError={voice.error}
              voiceLevel={voice.level}
              voiceDeviceLabel={voice.deviceLabel}
              onClearVoiceError={voice.clearError}
              voiceDictationPanel={sttDictationPanel}
              voiceSampleRef={voice.sampleRef}
              voicePartial={voice.partial}
              onVoiceToggle={voiceInputSupported ? toggleVoice : undefined}
              onVoicePrewarm={voiceInputSupported ? voice.prewarm : undefined}
              agentName={currentSlot?.agent || 'default'}
              agentSource={installedAgents.find(a => a.name === (currentSlot?.agent || 'default'))?.source}
              modelName={currentSlot?.model || resolvedModel || 'auto'}
              onAgentClick={provider.capabilities.agentTemplates ? (rect) => { setAgentBtnRect(rect); setAgentDropdown(!agentDropdown) } : undefined}
              onModelClick={(rect) => { setModelBtnRect(rect); setModelDropdown(!modelDropdown) }}
              onProjectClick={(rect) => {
                setProjectBtnRect(rect)
                setProjectPickerOpen(o => !o)
              }}
              contextPct={contextPct}
              contextUsedTokens={contextTokens?.used}
              contextWindowTokens={contextTokens?.window || provider.getContextWindow(currentSlot?.model || resolvedModel || 'auto')}
              showContextPct={chatConfig.showContextPct}
              isRunning={composerBusy}
              onStop={() => {
                const slot = activeSlot
                if (!slot) return
                const isEscalation = isEscalationState(currentSlot?.stop_state)
                // Per-slot view over the map, satisfying SoftStopRef so the
                // arming window is measured against THIS slot's soft press.
                const map = softStopAtMapRef.current
                const slotRef = {
                  get current() { return map.get(slot) ?? 0 },
                  set current(v: number) { map.set(slot, v) },
                }
                const action = handleStopPress(
                  isEscalation,
                  Date.now(),
                  slotRef,
                  () => dispatch(requestStop({ slotId: slot, force: false })),
                  () => dispatch(requestStop({ slotId: slot, force: true })),
                )
                // 'ignore' = accidental rapid double-tap during the arming window
                if (action !== 'ignore') dispatch(clearPendingPermissions())
              }}
              isQueued={slotStopping}
              stopState={currentSlot?.stop_state}
              approvalMode={displayMode}
              providerId={provider.id}
              reasoningEffort={effectiveEffort}
              onReasoningEffortClick={provider.capabilities.reasoningEffort && modelSupportsEffort(currentSlot?.model || resolvedModel) ? (rect) => { setReasoningEffortBtnRect(rect); setReasoningEffortDropdown(!reasoningEffortDropdown) } : undefined}
              onAutoNudgeClick={setAutoNudgeOpen}
              autoNudgeLoop={autoNudgeLoop}
              autoNudgeOpen={autoNudgeOpen}
              onAutoNudgeChange={setAutoNudgeLoop}
              browseMode={browseMode}
              onBrowseToggle={toggleBrowseMode}
              onOptimizeResult={handleOptimizeResult}
              memoryMode={currentSlot?.memory_mode ?? 'persistent'}
              cleanMode={currentSlot?.clean_mode}
              sentMessages={sentMessages}
              sendOnEnter={isMobile ? 'ctrl-enter' : chatConfig.sendOnEnter}
              followUpOptions={followUpOptions}
              followUpPicked={followUpPicked}
              quickSend={dashCfg?.quick_send}
              followUpLayout={chatConfig.followUpLayout}
              onFollowUpSelect={(o: string, e: React.MouseEvent) => {
                // Plan options (e.g. Stage-N-APPROVE) dispatch directly — no input fill.
                if (followUpIsPlan && effectiveMode === 'orchestrator' && activeSlot) {
                  if (planActionMutationRef.current.isPending) return
                  planActionMutationRef.current.mutate({ slot: activeSlot, action: o })
                  return
                }
                // One-click: enabled + no shift + not busy + not already in multi-select
                if (tryQuickSend(o, dashCfg?.quick_send, e.shiftKey, slotRunning, followUpPicked.size, send)) return
                // Regular options: toggle. Click unpicked → append + mark; click
                // picked → try to remove text + unmark (if the user edited the
                // text so it no longer matches, leave text alone — the chip
                // still un-highlights for consistency).
                if (followUpPicked.has(o)) {
                  setInput(prev => {
                    // Order matters: try leading ", o" first so "opt, opt" + remove
                    // last "opt" doesn't match "opt, " and splice the wrong one.
                    const leading = ', ' + o
                    let idx = prev.indexOf(leading)
                    if (idx >= 0) return prev.slice(0, idx) + prev.slice(idx + leading.length)
                    const trailing = o + ', '
                    idx = prev.indexOf(trailing)
                    if (idx >= 0) return prev.slice(0, idx) + prev.slice(idx + trailing.length)
                    if (prev === o) return ''
                    return prev  // user edited — leave text, still unmark below
                  })
                  setFollowUpPicked(prev => { const next = new Set(prev); next.delete(o); return next })
                } else {
                  setInput(prev => prev.trim() ? prev.trimEnd() + ', ' + o : o)
                  setFollowUpPicked(prev => new Set(prev).add(o))
                }
              }}
              pasteBlocks={pasteBlocks}
              onPasteBlocksChange={setPasteBlocks}
              knowledgeChip={knowledgeFetch.pendingKnowledge ? <div className="flex items-start gap-1"><KnowledgeBubbleChip knowledge={{ items: knowledgeFetch.pendingKnowledge.items.length, tokens: knowledgeFetch.pendingKnowledge.totalTokens, titles: knowledgeFetch.pendingKnowledge.items.map(i => i.title), content: knowledgeFetch.pendingKnowledge.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }} /><button type="button" onClick={() => knowledgeFetch.clearPending()} className="shrink-0 mt-0.5 p-0.5 text-muted hover:text-danger bg-transparent border-none cursor-pointer rounded hover:bg-danger/10 transition-colors" aria-label={i18nT('pages.chatPage.remove_knowledge_context')} title={i18nT('pages.chatPage.remove_knowledge_context')}>&times;</button></div> : undefined}
              connected={connected}
            />
            </div>
            <VoiceDisabledModal
              open={voiceSetupOpen}
              onClose={() => setVoiceSetupOpen(false)}
              onOpenSettings={() => {
                setVoiceSetupOpen(false)
                navigate(embedded ? '/embed/settings' : '/settings?tab=voice')
              }}
            />
            {/* Agent dropdown portal — triggered from input bar */}
            {agentDropdown && agentBtnRect && createPortal(
              // The keydown handler routes arrow/Enter navigation to the inner
              // role="listbox"; the dialog is a focus container (tabIndex={-1}),
              // not an interactive widget itself, so this delegation is intentional.
              // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
              <div ref={agentDropdownRef} role="dialog" aria-label={i18nT('pages.chatPage.agent_selector')} tabIndex={-1} onKeyDown={onAgentListKeyDown} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[260px] max-w-[340px] flex flex-col p-1 gap-0.5 animate-slide-up" style={(() => { const left = Math.max(8, Math.min(agentBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - agentBtnRect.top + 4, left } })()}>
                <div className="px-1.5 pt-1.5 pb-1">
                  <Input ref={agentInputRef} type="text" aria-label={i18nT('pages.chatPage.filter_agents')} placeholder={i18nT('pages.chatPage.type_to_filter')} value={agentFilter} onChange={e => setAgentFilter(e.target.value)} className="w-full px-2 py-1 text-[13px] font-mono" />
                </div>
                <div role="listbox" aria-label={i18nT('pages.chatPage.agent_list')} className="overflow-y-auto max-h-[280px]">
                <AgentDropdownList agents={filteredAgents} activeAgent={currentSlot?.agent || 'default'} defaultAgent={defaultAgent} onSelect={(name) => { switchAgent(name); setAgentDropdown(false) }} filter={agentFilter} />
                </div>
              </div>,
              document.body
            )}
            {/* Model dropdown portal — triggered from input bar */}
            {modelDropdown && modelBtnRect && createPortal(
              <ModelEffortDropdown
                anchorRect={modelBtnRect}
                dropdownRef={modelDropdownRef}
                inputRef={modelInputRef}
                onListKeyDown={onModelListKeyDown}
                models={filteredModels}
                activeModel={currentSlot?.model || resolvedModel || 'auto'}
                onSelectModel={name => switchModel(name)}
                filter={modelFilter}
                setFilter={setModelFilter}
                onClose={() => setModelDropdown(false)}
                hasEffort={!!(activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(currentSlot?.model || resolvedModel))}
                slot={activeSlot}
                currentEffort={currentSlot?.reasoning_effort || ''}
                defaultEffort={defaultEffort}
                onSetDefault={() => {
                  setModelDropdown(false)
                  navigate(`/settings?tab=chat&highlight=${SETTINGS_DEFAULT_MODEL_ID}`)
                }}
              />,
              document.body
            )}
            {/* Project picker — triggered from input bar */}
            <ProjectPicker
              open={projectPickerOpen}
              onOpenChange={setProjectPickerOpen}
              anchorRect={projectBtnRect}
              onSelect={path => { setProject(path); setProjectPickerOpen(false) }}
            />
            {/* Reasoning effort dropdown portal */}
            {reasoningEffortDropdown && reasoningEffortBtnRect && activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(currentSlot?.model || resolvedModel) && createPortal(
              <div ref={reasoningEffortDropdownRef} className="fixed z-[9999] animate-slide-up" style={(() => { const left = Math.max(8, Math.min(reasoningEffortBtnRect.left, window.innerWidth - 220)); return { bottom: window.innerHeight - reasoningEffortBtnRect.top + 4, left: isMobile ? 8 : left, ...(isMobile ? { right: 8, maxWidth: 'calc(100vw - 16px)' } : {}) } })()}>
                <ReasoningEffortDropdown slot={activeSlot} currentEffort={currentSlot?.reasoning_effort || ''} defaultEffort={defaultEffort} onClose={() => setReasoningEffortDropdown(false)} />
              </div>,
              document.body
            )}
            </div>
          </div>
          </SearchHighlightContext.Provider>
        )}
      </div>
      )}
      {search.isOpen && (
          <DetailPanel
            key="search-panel"
            title={<SearchBar docked term={search.term} setTerm={search.setTerm} matches={search.matches} currentIdx={search.currentIdx} next={search.next} prev={search.prev} close={search.close} caseSensitive={search.caseSensitive} toggleCaseSensitive={search.toggleCaseSensitive} focusNonce={search.focusNonce} goTo={search.goTo} />}
            onClose={search.close}
            initialWidth={400}
            minWidth={320}
            reserveWidth={panelReserve}
            storageKey="mc-search-width"
            noPadding
          >
            {search.matches.length > 0 ? (
              <SearchResultsList
                matches={search.matches}
                currentIdx={search.currentIdx}
                messages={messages}
                term={search.term}
                caseSensitive={search.caseSensitive}
                onJump={jumpToSearchResult}
              />
            ) : (
              <div className="px-4 py-3 text-[13px] text-muted">{search.term ? i18nT('pages.chatPage.no_results') : i18nT('pages.chatPage.type_to_search_this_conversation')}</div>
            )}
          </DetailPanel>
        )}
      <AnimatePresence initial={false}>
        {/* Inline side panel — mobile / embed frames where there's no actbar
            grid column. Desktop uses the actbar portal below. */}
        {activityOpen && !search.isOpen && !activitySlot && (
          <motion.div
            key="side-panel-inline"
            initial={{ width: 0 }}
            animate={{ width: 'auto' }}
            exit={{ width: 0 }}
            transition={{ duration: 0.4, ease: [0.32, 0.72, 0, 1] }}
            className="h-full overflow-hidden flex justify-end shrink-0"
          >
            <SidePanel
              tabsCtl={tabsCtl}
              slot={activeSlot || ''}
              files={touchedFiles.files} onFileOpen={handleFileOpen} onFileRemove={touchedFiles.removeFile} onFilesClear={touchedFiles.clearBySource}
              onArtifactOpen={handleArtifactOpen}
              projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
              sources={sourceLinks} selectedSourceUrl={selectedSourceUrl} onSelectSource={setSelectedSourceUrl}
              issues={issueLinks} selectedIssueUrl={selectedIssueUrl} onSelectIssue={setSelectedIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
              onSubmitComments={submitComments} onFileSave={handleFileSave} onClose={toggleAct}
              inlinePreviewPath={inlinePreviewPath} onInlinePreviewChange={setInlinePreviewPath}
              expanded={previewFocused}
              fillWidth={panelFillWidth}
            />
          </motion.div>
        )}
      </AnimatePresence>
      {/* Full-height tabbed side panel: portaled into the App shell's
          'actbar' grid column so it spans the window top-to-bottom; the header
          row ends at its left edge, shifting the top-bar buttons left.
          The motion wrapper animates the column width 0 -> auto: the actbar
          grid column tracks it frame-by-frame, so the chat pane slides left in
          sync while the panel (right-anchored via justify-end) slides out from
          the window edge — both sides move together instead of snapping. */}
      {activitySlot && createPortal(
        <AnimatePresence initial={false}>
          {activityOpen && !search.isOpen && (
            <motion.div
              key="side-panel"
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: 'auto', opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
              className="h-full overflow-visible flex justify-end"
            >
              <SidePanel
                tabsCtl={tabsCtl}
                slot={activeSlot || ''}
                files={touchedFiles.files} onFileOpen={handleFileOpen} onFileRemove={touchedFiles.removeFile} onFilesClear={touchedFiles.clearBySource}
                onArtifactOpen={handleArtifactOpen}
                projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
                sources={sourceLinks} selectedSourceUrl={selectedSourceUrl} onSelectSource={setSelectedSourceUrl}
              issues={issueLinks} selectedIssueUrl={selectedIssueUrl} onSelectIssue={setSelectedIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
                onSubmitComments={submitComments} onFileSave={handleFileSave} onClose={toggleAct}
                inlinePreviewPath={inlinePreviewPath} onInlinePreviewChange={setInlinePreviewPath}
                expanded={previewFocused}
                fillWidth={panelFillWidth}
              />
            </motion.div>
          )}
        </AnimatePresence>,
        activitySlot
      )}
    </div>
    </TagPopoverProvider>
  )
}

