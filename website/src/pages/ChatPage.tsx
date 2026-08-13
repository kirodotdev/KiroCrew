import { useState, useRef, useCallback, useEffect, useLayoutEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query'
import { useModelsDegraded } from '../providers/modelListHealth'
import { useIsMobile } from '../hooks/useIsMobile'
import { useImeGuard } from '../hooks/useImeGuard'
import { useRailWidth } from '../hooks/useRailWidth'
import { SETTINGS_DEFAULT_MODEL_ID } from '../hooks/useSettingHighlight'
import { isTouchDevice } from '../utils/isTouchDevice'
import { isBrowseCommand } from '../utils/browseCommand'
// Re-exported so the symbol `ChatPage` exported before this extraction stays
// importable from here; the implementation lives in `utils/browseCommand` so a
// pure test need not pull ChatPage's module graph.
export { isBrowseCommand }
import { useSwipeEdge } from '../hooks/useSwipeEdge'
import type { ResizeInfo } from '../utils/resizeImage'
import { useAppSelector, useAppDispatch, store } from '../store'
import { useConnected } from '../hooks/useConnected'
import { useChatPopouts } from '../hooks/useChatPopouts'
import {
  switchSlot, createSlot, deleteSlot, fetchHistory, loadOlderMessages, isSupersededPagingRejection,
  appendMessage, appendSlotMessage, resumeFromHistory, forkSlot,
  setSlotRunning, startLocalTurn, syncSlotRunningFromServer, setPendingInput, setAgentSwitchNotice, resolveByApprovalId, clearPendingPermissions, cancelQueuedMessage, editQueuedMessage,
  selectComposerBusy,
  selectContinuable,
  selectTurnInterrupted,
  setVoiceAudio,
  toggleActivity, openActivityPanel, openActivityToTab,
  selectSubagent,
  setActiveSlot, truncateAfterIndex, replaceMessages,
  requestStop, pendingQuestionFor, captureStatelessCard, clearFollowupCard, dismissFollowupItem, clearFolderSuggestion,
  retireStatelessQuestion, capturePendingAskId, confirmOptimisticSend,
  requestSlotReveal,
  mcpAppKey,
} from '../store/chatSlice'
import { confirmedDelivered } from '../utils/sendDelivery'
import { addNotification, removeNotificationByTs } from '../store/notificationsSlice'
import { onTerminalReady, sendToTerminalSession } from '../utils/terminalRegistry'
import { addTab as addDockTerminal } from '../hooks/useBottomTerminal'
import { interceptSlashCommand, isInterceptedSlashCommand } from './chat/ChatInput'
import { sseSlotTitle, triggerRefresh } from '../store/dashboardSlice'
import { api } from '../api/client'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
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
import { useTheme } from '../hooks/useTheme'
import CollapsibleToolGroup from './chat/CollapsibleToolGroup'
import ThinkingBlock from './chat/ThinkingBlock'
import { RowDisclosureProvider } from './chat/rowDisclosure'
import type { DisplayItem, TurnItem } from './chat/types'
import McpToolsPanel from './chat/McpToolsPanel'
import { deriveLoadedMcpTools } from '../lib/mcpLoadedTools'
import type { McpServer } from '../types'
import { useScrollManager } from './chat/useScrollManager'
import { shouldPaginateOlder } from './chat/pagination'
import EarlierMessagesBar from './chat/EarlierMessagesBar'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { parseFiles, prepareSendPayload, resolveFileSegment, buildFileLabels, buildRelMap, findUnreferencedAttachments, parseDirTokens, serializeDirTokens, parseDirs, resolveDirSegment, spliceDirTokens } from '../utils/fileTokens'
import { classifyDrop } from '../utils/dropClassify'
import { makeRelative } from '../components/FilePickerMenu'
import { type PasteBlock, expandAll as expandPasteTokens, findTokenRanges, pruneBlocks as pruneBlocksUtil, remapCarriedBlocks, saveStoredPaste, recollapsePastes } from '../utils/pasteTokens'
import { extractPromptFromToken, extractSlackContextFromToken } from '../utils/tokenPrompt'
/** Delay (ms) before scrolling to bottom after a state update, giving React time to commit. */
const SCROLL_AFTER_RENDER_MS = 100
// No arbitrary cap on pinned-jump page loads: the loop terminates when the
// target message is found OR history is exhausted (!slotHasMore / null result).
// The `cancelled` flag in the useEffect cleanup and the loadOlderMessages null
// sentinel prevent infinite loops.  A ref tracks loads for diagnostics only.
// Canonical home is utils/navIntent (shared with the popout nav-intent
// applier); re-exported here for this page's historical importers.
export { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import { PREFILL_STORAGE_KEY, writePrefill } from '../utils/navIntent'
import { consumeChatHandoff, subscribeChatHandoff } from '../utils/errorReport'
import WelcomeView from '../components/WelcomeView'
import { usePanelTabs, openPanelView, clearInlineDraft, getInlineDraft, claimAppAutoOpen, useAnyLiveAppTab } from '../hooks/usePanelTabs'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useAgents } from '../hooks/useAgents'
import AgentDropdownList, { DefaultAgentRow, ManageAgentsFooter } from '../components/AgentDropdownList'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import ProjectPicker from '../components/ProjectPicker'
import InboundLinkChip from '../components/InboundLinkChip'
import SessionActionsMenu from '../components/SessionActionsMenu'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent,
} from '../components/ui/dropdown-menu'
import ModelEffortDropdown from '../components/ModelEffortDropdown'

import ChatInput from '../components/ChatInput'
import ErrorNotice from '../components/ErrorNotice'
import SessionGridView from '../components/SessionGridView'
import { anchorForSlot, loadLayout, sessionSlots } from '../hooks/splitLayoutStore'
import { modelSupportsEffort } from '../lib/effort'
import { isEmbeddedPane } from '../lib/embedded'
import { isSystemNoticeKind } from '../lib/systemNotice'
import { displayModel, pinIsWithheld } from '../lib/model'
import FollowUpCard from '../components/FollowUpCard'
import FolderSuggestionCard from './chat/FolderSuggestionCard'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import PendingQuestionCard from '../components/PendingQuestionCard'
import SessionPulseSurveyCard from '../components/SessionPulseSurveyCard'
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
import { useVoiceInput, voiceInputSupported, type TranscriptOrigin } from '../hooks/useVoiceInput'
import { usePushToTalk } from '../hooks/usePushToTalk'
import VoiceDisabledModal from '../components/VoiceDisabledModal'
import { ChatFooter, AssistantMessage, UserMessage, PinnedPrompt } from './chat'
import type { TurnStats } from './chat/AssistantMessage'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { JiraHostsCtx } from '../lib/jiraHosts'
import MessageErrorBoundary from '../components/MessageErrorBoundary'
import TypewriterText from '../components/TypewriterText'
import { useChatNavigation } from '../hooks/useChatNavigation'
import { useChatPins } from '../hooks/useChatPins'
import SubagentProgressBar from './chat/SubagentProgressBar'
import TaskProgressBar from './chat/TaskProgressBar'
import SidePanel, { CHAT_PANE_MIN_W, sidePanelFillWidth } from './chat/SidePanel'
import { useSidePanelDock } from '../hooks/useSidePanelDock'
import { groupDisplayItems, applyRunningState } from './chat/groupDisplayItems'
import { setSessionPreviewPending, normalizeUrl, PREVIEW_EXPAND_EVENT, PREVIEW_SNIP_EVENT } from '../components/WebPreviewPanel'
import { detectPreviewUrl, previewFeedDecision } from '../utils/detectPreviewUrl'
import { fileLandingSlot } from '../utils/uploadRouting'
import ChatSidebar, { SIDEBAR_MIN, SIDEBAR_MAX } from './ChatSidebar'
import { toSlug } from '../utils/shareUrl'
import { DRAFT_SAVE_DEBOUNCE_MS, loadDrafts, mergeIntoDraft, saveDrafts as persistDrafts, setDraft } from '../utils/chatDrafts'
import { loadFileDrafts, saveFileDrafts as persistFileDrafts, setFileDraft } from '../utils/chatFileDrafts'
import { loadPasteDrafts, savePasteDrafts as persistPasteDrafts, setPasteDraft } from '../utils/chatPasteDrafts'
import { loadSessionRefDrafts, saveSessionRefDrafts as persistSessionRefDrafts, setSessionRefDraft } from '../utils/chatSessionRefDrafts'
import { addSessionRef, removeSessionRef, mergeSessionRefs, appendSessionRefLinks, type SessionRef } from '../utils/sessionRefs'
import { findPinnedPromptIdx, findNextPromptIdx, computePinPush, promptPreview, promptImages, promptBody, pinHandoffY, pinPushTravel, jumpAnchorIdx, DEFAULT_PINNED_CARD_H } from '../utils/pinnedPrompt'
import {
  adoptSourceSelections,
  commitRevealedSource,
  commitSourceSelection,
  isSourceSelectionKey,
  loadSeenPullRequestLinks,
  loadSourceSelections,
  partitionSourceLinks,
  parseSourceLinkUrl,
  persistSeenPullRequestLinks,
  PullRequestLinkIndex,
  recordNewPullRequestLinks,
  type RevealedSources,
  loadRevealedSources,
  type SourceLinkKind,
  sourceSelection,
  withSourceSelection,
} from '../utils/pullRequestLinks'
import { deriveFollowUpOptions } from '../app-sdk/protocol'
import OverlayDrawer from '../components/OverlayDrawer'
import { loadChatConfig, CONTENT_WIDTH, type ChatConfig } from './chat/ChatSettings'
import SessionFlyout, { TOGGLE_RECT } from './chat/SessionFlyout'
import { focusComposerAfter, revealComposer } from './chat/composerFocus'
import { useHoverIntent } from '../hooks/useHoverIntent'
import { useKnowledgeFetch, extractKnowledgeQuery, expandKnowledgeBlock } from './chat/useKnowledgeFetch'
import { KnowledgePicker } from './chat/KnowledgePicker'
import { BookOpen, EyeOff, Loader, Pen, ChevronDown, ChevronRight, Plug, ArrowDown, MessageSquare, MessageSquareDot, Sparkles, VenetianMask, Clock, Undo2, Columns2, ExternalLink, Paperclip, Folder, X } from 'lucide-react'
import { PanelLeftSolid, PanelLeftLight, PanelRightSolid } from '../components/icons/panels'

import InfoTip from '../components/InfoTip'
import { FileCard } from '../components/FileCard'
import SlotTagPopover from '../components/SlotTagPopover'
import { TagPopoverProvider } from '../hooks/useTagPopover'

import { AnimatePresence, motion } from 'framer-motion'
import DetailPanel from '../components/DetailPanel'

import type { ChatMessage, Artifact } from '../types'

import ToolCallLine from './chat/ToolCallLine'
import { shouldMountSidePanel, isSidePanelHidden, sidePanelDockMotion } from './chat/sidePanelMount'
import { optsForReplace } from './chat/replaceGuard'
import WorkflowRunCard, { extractWorkflowRunId } from './chat/WorkflowRunCard'
import SubagentRunCard, { extractSpawnRunLaunch } from './chat/SubagentRunCard'
import WorkflowCompletionCard, { isWorkflowCompletionMessage } from './chat/WorkflowCompletionCard'
import SubagentCompletionCard from './chat/SubagentCompletionCard'
import { isSubagentCompletionMessage, type ParsedSubagentCompletion } from './chat/subagentCompletion'
import { renderMcpOAuthMessage } from './chat/McpOAuthBanner'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import TurnBlock from './chat/TurnBlock'
import Clickable from '../components/Clickable'
import StopEventCard from './chat/StopEventCard'
import NudgeCard, { nudgeMatchesLoop } from './chat/NudgeCard'
import RecoveryCard, { resolveInjectCard } from './chat/RecoveryCard'
import { ErrorCard } from './chat/ErrorCard'
import WorkflowProgressBar from './chat/WorkflowProgressBar'
import { tryQuickSend } from '../lib/quickSend'
import { rewindWithRollback } from '../lib/rewindCall'


import { i18nT } from '../i18n/t'
import { parseNudgeMessage, nudgeLabel } from './chat/NudgeCard'
import { parseSubagentCompletionMessage } from './chat/subagentCompletion'
import { headline as subagentHeadline } from './chat/SubagentCompletionCard'
import { fmtDateFields, fmtNumber } from '../i18n/format'
import { fmtMessageTime, fmtMessageTimeFull } from './chat/messageTime'
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
  // MCP server list is fetched lazily when its submenu opens (driven by the
  // Radix Sub's open state).
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

/** Per-message identity key with row-id tie-break. `msgKey` alone is NOT
 *  unique — a coarse OS clock can stamp two rows appended in one tick with the
 *  same `ts` (see isRedeliveredMessage in chatSlice on why row identity is
 *  `meta.mid`, not a ts tuple). `mid` is stamped once per row and survives
 *  every delivery door (HTTP rebuild, WS broadcast, JSONL round trip), so the
 *  suffix is as reload-stable as the key it disambiguates. Rows without a
 *  `mid` (locally-minted streaming/optimistic bubbles) fall back to `msgKey`
 *  alone, which is exactly the uniqueness they had before. */
function msgIdentityKey(m: ChatMessage, msgKey: (m: ChatMessage) => string): string {
  const mid = m.meta?.mid
  return typeof mid === 'string' && mid ? `${msgKey(m)}~${mid}` : msgKey(m)
}

/** Stable key for a single TurnItem — the leading row of a turn OR a top-level
 *  single/group. A `single` and the `turn` it leads resolve to the SAME key so
 *  a mid-stream regroup (single promoted into a grouped turn once it gains
 *  working steps) does NOT change the row's virtual key → no remount / silent
 *  re-measure. `msgKey` supplies the per-message identity (clientTs → ts →
 *  minted id; never the array index — see stableMsgKey). Groups key on their
 *  FIRST MESSAGE's identity, never `startIdx`: a prepend (history backfill)
 *  renumbers every array index but leaves message identities intact, so a
 *  group-led row keeps its key — and with it its cached height, DOM node, and
 *  scroll anchor — across the shift. The index key this replaces was unique by
 *  construction, so group keys go through `msgIdentityKey` to keep that
 *  property across same-tick `ts` ties.
 *
 *  `msgs` is non-empty by construction (both producers emit a group only under
 *  `if (group.length)`), but the type allows `[]` and this is a public export —
 *  degrade to the index rather than throwing inside `msgKey`. */
export function turnLeadKey(it: TurnItem, msgKey: (m: ChatMessage) => string): string {
  if (it.kind === 'single') return `row-${msgKey(it.msg)}`
  const lead = it.msgs[0]
  return lead ? `grp-${msgIdentityKey(lead, msgKey)}` : `grp-idx-${it.startIdx}`
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

/** React key for a message row's INNER bubble (the virtualizer row key is
 *  virtualKeyFor). Prefer the optimistic client ts (stashed by the steer-echo
 *  reconcile, and stamped at birth on streaming/thinking messages) over the
 *  server ts, so a mid-stream ts overwrite never remounts the bubble.
 *
 *  Role-prefixed for cross-role uniqueness, EXCEPT that 'streaming' normalizes
 *  to 'assistant': finalization (`_done` / `_segment`) mutates the SAME logical
 *  message's role from streaming to assistant, and a role-sensitive key
 *  remounted the bubble at end-of-turn — destroying useSmoothStream's drain
 *  state, so the trailing unrevealed text (a standing ~LAG_SECS of it under the
 *  constant-latency controller) snapped into view instead of finishing its
 *  reveal. Exported for tests. */
export function messageRowKey(m: ChatMessage, i: number): string {
  const keyTs = (m.meta?.clientTs as string | undefined) || m.ts
  const role = m.role === 'streaming' ? 'assistant' : m.role
  return keyTs ? `${role}-${keyTs}` : `${role}-${i}`
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
        <BookOpen size={12} className="shrink-0" /> {i18nT('pages.chatPage.knowledge_item', { count: knowledge.items })} · {fmtNumber(knowledge.tokens)} {i18nT('pages.chatPage.tokens')}
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

export function renderUserContent(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, onFolderOpen?: (path: string) => void, linkPreviews?: boolean) {
  // Per-message containment (defense-in-depth): a render crash in a
  // user/inject bubble must degrade to a per-message fallback, not unwind to
  // the root boundary and blank the whole dashboard.
  //
  // Sent-prompt images render small: renderFileSegment passes `compactImages`
  // to MarkdownRenderer, which owns the CompactImagesCtx provider internally.
  // (Done there, not here, so tests that mock MarkdownRenderer don't need the
  // context export.)
  return (
    <MessageErrorBoundary rawContent={content}>
      {renderUserContentInner(content, meta, onFileOpen, onFolderOpen, linkPreviews)}
    </MessageErrorBoundary>
  )
}

function renderUserContentInner(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, onFolderOpen?: (path: string) => void, linkPreviews?: boolean) {
  const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
  const knowledge = meta?.knowledge as { items: number; tokens: number; titles: string[]; content?: { title: string; text: string }[] } | undefined

  // Folder references resolve FIRST, on the whole message: `[attached_dir N]
  // /path` markers (history replay / steer echo) rewrite to `@label/` display
  // tokens, and fresh `@rel/` tokens map to their meta.dirs path. One pass
  // here — before the paste split — so every segment renderer below sees the
  // token form and one shared label->path map. Dir markers never appear
  // inside paste blocks (they serialize from the typed text only), so the
  // rewrite cannot break paste-token ranges recomputed on the result.
  const { display: dirResolved, dirMentionMap } = resolveDirSegment(content, parseDirs(content, meta))
  content = dirResolved

  const knowledgeBadge = knowledge ? (
    <KnowledgeBubbleChip knowledge={knowledge} />
  ) : null

  if (!pastes.length) return <>{knowledgeBadge}{renderFileSegment(content, meta, onFileOpen, 'seg', dirMentionMap, onFolderOpen, linkPreviews)}</>


  // History load re-serves the fully-EXPANDED content (what the LLM saw), so a
  // message whose bubble was a `[ Paste #N ]` chip when sent comes back as the
  // raw paste text with no token in it. If mergePreservedPastes couldn't
  // re-collapse it (no optimistic bubble, side-table entry evicted/missing),
  // handing that raw text — potentially hundreds of KB / tens of thousands of
  // lines — to renderFileSegment → MarkdownRenderer parses and lays it out on
  // the main thread and freezes the tab. Re-collapse deterministically from the
  // blocks that travel with the message so the chip is restored regardless of
  // external state. See recollapsePastes.
  let text = content
  let ranges = findTokenRanges(text, pastes)
  if (!ranges.length) {
    const collapsed = recollapsePastes(content, pastes)
    if (collapsed !== content) {
      text = collapsed
      ranges = findTokenRanges(text, pastes)
    }
  }
  if (!ranges.length) return <>{knowledgeBadge}{renderFileSegment(text, meta, onFileOpen, 'seg', dirMentionMap, onFolderOpen, linkPreviews)}</>

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
      if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, `t${i}`, dirMentionMap, onFolderOpen))
    }
    out.push(<PastedChip key={`p${i}-${r.block.id}`} block={r.block} />)
    lastIdx = trimEnd
  })
  if (lastIdx < text.length) {
    const seg = text.slice(lastIdx)
    if (seg) out.push(renderInlineSegment(seg, meta, onFileOpen, 'tend', dirMentionMap, onFolderOpen))
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

/** Boundary-checked presence of an `@token` in a text segment — the same rule
 *  the split regex uses, so a key is only offered to a segment that can
 *  actually match it. */
function tokenPresent(text: string, token: string): boolean {
  const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(^|\\s)@${esc}(?=\\s|$)`).test(text)
}

/** Inline chip for a folder reference in a sent message. Clicking opens the
 *  directory in the side panel's file tree — the SAME handler assistant-message
 *  directory chips use (handleFolderOpen -> tabsCtl.openFolder), so a folder is
 *  equally actionable whichever side of the conversation names it. Shift-click
 *  reveals in the OS file manager, mirroring MarkdownRenderer's activatePath.
 *  Without a handler (export used outside ChatPage) it degrades to an inert
 *  span with the path in the tooltip. */
function DirChip({ label, fullPath, onOpen }: { label: string; fullPath: string; onOpen?: (path: string) => void }) {
  const body = (
    <>
      <Folder size={11} aria-hidden="true" className="shrink-0 lucide-inline" />@{label}
    </>
  )
  if (!onOpen) {
    return (
      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 mx-0.5 rounded border border-accent/25 bg-accent/10 text-accent text-[12px] font-mono" title={fullPath}>
        {body}
      </span>
    )
  }
  return (
    <Clickable
      className="inline-flex items-center gap-1 px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors"
      title={fullPath}
      aria-label={i18nT('pages.chatPage.open_folder', { path: fullPath })}
      onClick={e => {
        if (e && 'shiftKey' in e && e.shiftKey) { api.revealPath(fullPath); return }
        onOpen(fullPath)
      }}
    >
      {body}
    </Clickable>
  )
}

/** Inline-flow renderer for a text segment adjacent to a paste chip.
 *  Handles @-file tokens as inline chips; other text is rendered as a
 *  whitespace-preserving span (no markdown). */
function renderInlineSegment(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, keyBase: string, dirMap?: Map<string, string>, onFolderOpen?: (path: string) => void) {
  const parsedFiles = parseFiles(content, meta)
  const dirKeys = dirMap ? [...dirMap.keys()].filter(k => tokenPresent(content, k)).slice(0, 20) : []
  if (!parsedFiles.length && !dirKeys.length) {
    return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{content}</span>
  }
  // Inline-flow variant (adjacent to a paste chip): keep everything inline.
  // Non-image attachments referenced in the text render as inline chips; any
  // standalone-token upload in this segment also renders as an inline chip
  // appended to it (this path can't host block cards without breaking the
  // inline flow). Never-referenced attachments are handled once at message
  // level. Pass the ORIGINAL ordered list so token indices line up.
  const { display, mentionMap, cardPaths, labels } = resolveFileSegment(content, parsedFiles)
  if (!mentionMap.size && !cardPaths.length && !dirKeys.length) {
    return <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>{display}</span>
  }

  // Folder tokens join the same split as file mentions. A dir key always ends
  // in `/` and a file key never does, so classification below is unambiguous.
  const keys = [...[...mentionMap.keys()].slice(0, 20), ...dirKeys]
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = tokPattern
    ? display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))
    : [display]
  const chipCls = 'inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors'
  return (
    <span key={keyBase} style={{ whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        const tok = part.match(/^@(.+)$/)?.[1]
        const dirPath = tok && dirMap?.get(tok)
        if (dirPath) {
          return <DirChip key={`${keyBase}-d${i}`} label={tok} fullPath={dirPath} onOpen={onFolderOpen} />
        }
        const fullPath = tok && mentionMap.get(tok)
        if (fullPath) {
          return (
            <Clickable key={`${keyBase}-f${i}`} className={chipCls} title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={i18nT('pages.chatPage.open_file', { path: fullPath })}>@{tok}</Clickable>
          )
        }
        return <span key={`${keyBase}-p${i}`}>{part}</span>
      })}
      {cardPaths.map((p, i) => (
        <Clickable key={`${keyBase}-uc${i}`} className={chipCls} title={p} onClick={() => onFileOpen(p)} aria-label={i18nT('pages.chatPage.open_file', { path: p })}>@{labels.get(p) || p}</Clickable>
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
      aria-label={i18nT('pages.chatPage.open_file', { path: fullPath })}
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
function renderFileSegment(content: string, meta: Record<string, unknown> | undefined, onFileOpen: (path: string) => void, keyBase: string, dirMap?: Map<string, string>, onFolderOpen?: (path: string) => void, linkPreviews?: boolean) {
  const parsedFiles = parseFiles(content, meta)
  const dirKeys = dirMap ? [...dirMap.keys()].filter(k => tokenPresent(content, k)).slice(0, 20) : []

  // No attachments — plain markdown (bold, code, links, etc.).
  // softBreaks: preserve Shift+Enter line breaks as <br> (see MarkdownRenderer).
  // compactImages: this is user-message content, so attached images render small.
  // linkPreviews: mirrors the assistant path — a URL the user pasted unfurls
  // under the same opt-in gate as one the model wrote (issue #2580).
  //
  // A folder token routes the message into the inline chip-split body below,
  // which renders surrounding text as plain whitespace-preserving spans — so
  // markdown in a folder-referencing message shows literally. This is the
  // same trade-off inline file mentions already make, accepted here because
  // the chip must sit inline in the sentence and MarkdownRenderer has no
  // inline-widget seam; a folder-referencing prompt with block markdown is
  // the uncommon combination.
  if (!parsedFiles.length && !dirKeys.length) {
    return <MarkdownRenderer content={content} softBreaks compactImages linkPreviews={linkPreviews} />
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
  // Folder references never card: a folder is a path reference, not an upload,
  // and its token is by construction present in the text.
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

  // No inline @-mentions of either kind: caption (if any) is plain markdown,
  // then the cards.
  if (!mentionMap.size && !dirKeys.length) {
    const caption = display.trim()
    return <>{caption ? <MarkdownRenderer key={`${keyBase}-cap`} content={caption} softBreaks compactImages linkPreviews={linkPreviews} /> : null}{cards}</>
  }

  // Inline-mention path: the caption keeps files inline, so render it as a
  // single inline flow — text runs as whitespace-preserving spans (NOT block
  // MarkdownRenderer, which wraps each run in a <p> and would break the line
  // around the chip) and each @token as an inline chip. Block markdown (bold,
  // lists) inside a caption that also carries an inline mention renders as
  // literal text — a rare combination, same trade-off as renderInlineSegment.
  // Cap tokens to prevent ReDoS from many alternations. Folder tokens join
  // the same split; a dir key always ends in `/` and a file key never does,
  // so classification below is unambiguous.
  const keys = [...[...mentionMap.keys()].slice(0, 20), ...dirKeys]
  const tokPattern = keys.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  const parts = display.split(new RegExp(`(@(?:${tokPattern}))(?=\\s|$)`, 'g'))
  const body = (
    <span key={`${keyBase}-body`} style={{ whiteSpace: 'pre-wrap' }}>
      {parts.map((part, i) => {
        const tok = part.match(/^@(.+)$/)?.[1]
        const dirPath = tok && dirMap?.get(tok)
        if (dirPath) {
          return <DirChip key={`${keyBase}-d${i}`} label={tok} fullPath={dirPath} onOpen={onFolderOpen} />
        }
        const fullPath = tok && mentionMap.get(tok)
        if (fullPath) {
          return (
            <Clickable key={`${keyBase}-f${i}`} className="inline-flex items-center px-1.5 py-0.5 mx-0.5 rounded bg-accent/15 text-accent text-[12px] font-mono cursor-pointer hover:bg-accent/25 transition-colors"
              title={fullPath} onClick={() => onFileOpen(fullPath)} aria-label={i18nT('pages.chatPage.open_file', { path: fullPath })}>@{tok}</Clickable>
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

// Per-action titles for the refused-press notice above the composer. A press
// added later gets its refusal surfaced by adding one entry here and calling
// `showRefusedPress` from its catch — the `as const` map keeps every key
// statically resolvable for the catalog-key gate.
const REFUSED_PRESS_TITLE_KEYS = {
  continue: 'pages.chatPage.could_not_continue',
  regenerate: 'pages.chatPage.could_not_regenerate',
  switch_variant: 'pages.chatPage.could_not_switch_variant',
} as const
type RefusedPressAction = keyof typeof REFUSED_PRESS_TITLE_KEYS

export default function ChatPage({ mode, embedded, embedMode, popout, noUrlSync }: { mode?: string; embedded?: boolean; embedMode?: 'chat' | 'sessions'; popout?: boolean; noUrlSync?: boolean } = {}) {
  const dispatch = useAppDispatch()
  const moveSlotToFolder = useMoveSlotToFolder()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const location = useLocation()
  const queryClient = useQueryClient()
  const provider = useProvider()
  const [searchParams, setSearchParams] = useSearchParams()
  const slots = useAppSelector(s => s.dashboard.slots)
  // Unified chat view: show default, orchestrator and crew slots together.
  // App-owned worker slots (s.app) are excluded by the sidebar itself.
  const filteredSlots = useMemo(
    () => slots.filter(s => {
      const sk = s.surface ?? s.mode ?? ''
      return sk === '' || sk === 'orchestrator' || sk === 'crew'
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
  // Create-in-flight, so the flyout's New button can go inert exactly like the
  // sidebar's does instead of accepting a second click.
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
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
  // MCP Apps in the side panel (dashboard.mcp_app_panel, opt-in). When on, a new
  // render opens the panel to its own `app` tab instead of drawing inline in the
  // bubble — same auto-open path the web-preview marker uses.
  const { data: appPanelCfg, isError: appPanelCfgError } = useQuery<{ mcp_app_panel?: boolean; auto_open_git_panel?: boolean }>({
    queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000,
  })
  const mcpAppPanel = appPanelCfg?.mcp_app_panel === true
  // Opt-in: expand the side panel to the Git tab on sight of a git project
  // (dashboard.auto_open_git_panel). See the git-panel effect for why it is off
  // by default.
  const autoOpenGitPanel = appPanelCfg?.auto_open_git_panel === true
  // Whether that value is KNOWN yet. The git effect consumes a one-shot
  // localStorage marker, so acting while this query is still in flight would
  // burn the marker with the flag reading false and an opted-in user would never
  // get the panel. A FAILED query counts as known and resolves to the documented
  // default (off) — otherwise a config endpoint that is down would withhold the
  // Git tab itself, which the flag does not govern.
  const autoOpenGitPanelKnown = appPanelCfg !== undefined || appPanelCfgError
  // Tool-call ids already routed to a tab, so re-renders of the same app don't
  // yank focus back to the panel on every streaming update.
  useEffect(() => {
    if (!mcpAppPanel || !activeSlot) return
    for (const id of appToolCallIds) {
      // The claim lives at module scope, NOT in a ref: a ref is recreated on every
      // ChatPage mount, so a trip to Settings and back re-opened (and re-focused)
      // a tab the user had deliberately closed.
      if (!claimAppAutoOpen(activeSlot, id)) continue
      dispatch(openActivityPanel())
      tabsCtlRef.current?.openApp(id, i18nT('pages.chatPage.mcp_app_tab_title'), activeSlot)
    }
  }, [mcpAppPanel, activeSlot, appToolCallIds, dispatch])

  const messages = useAppSelector(s => s.chat.messages)
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const kiroCrewVersion = useAppSelector(s => s.dashboard.status?.version) || ''
  const assistantTurnCount = useMemo(
    () =>
      messages.filter(
        m =>
          m.role === 'assistant' &&
          // Skip ALL assistant-role system notices (compaction, session_reload,
          // and any future kind), not just compaction — otherwise a reload
          // notice or stage separator counts as a completed assistant turn and
          // can trip the survey's 3-turn threshold with no real interaction.
          !isSystemNoticeKind(m.kind ?? (m.meta?.kind as string | undefined)),
      ).length,
    [messages],
  )
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
  // Turn disclosure ("N tool calls" / "Worked through N steps"), keyed by the
  // virtualizer's stable row key. This lives HERE rather than in TurnBlock
  // because the transcript is virtualised: a row is unmounted once it leaves
  // the mounted window, which streaming does routinely as it scrolls content
  // past, and row-local state would be destroyed every time. An entry exists
  // only for a turn the user has explicitly toggled; absent means "use the
  // default", so the automatic collapse-on-completion is untouched.
  const [turnDisclosure, setTurnDisclosure] = useState<Record<string, boolean>>({})
  const setTurnDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setTurnDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  // Same problem, same shape, for the per-tool-call pill (ToolCallLine): its
  // expanded panel is also row-local and also dies when the virtualizer
  // recycles the row. Keyed by the pill's own message key.
  const [toolDisclosure, setToolDisclosure] = useState<Record<string, boolean>>({})
  const setToolDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setToolDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  // Row keys are only unique within a slot, so carrying them across a slot
  // switch would apply one session's choices to another's turns.
  useEffect(() => { setTurnDisclosure({}); setToolDisclosure({}) }, [activeSlot])
  // Shared composer-busy rule (chatSlice.selectComposerBusy). Drives the
  // composer's busy/queue affordance so a message sent during a sub-agent run
  // reads as "will queue".
  const composerBusy = useAppSelector(s => selectComposerBusy(s, s.chat.activeSlot))
  const slotStopping = useAppSelector(s => s.chat.slotStopping)
  const slotLoading = useAppSelector(s => s.chat.slotLoading)
  // While a session-switch history fetch is still in flight for the active
  // slot, this equals activeSlot (even during the cached-provisional window
  // where slotLoading is already false). Used to defer the session-pulse
  // survey's baseline capture until the real transcript has settled.
  const slotSwitchTarget = useAppSelector(s => s.chat.slotSwitchTarget)
  const pendingQuestion = useAppSelector(s => pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot))
  const pendingFollowup = useAppSelector(s => (s.chat.activeSlot ? s.chat.followups?.[s.chat.activeSlot] : undefined))
  const folderSuggestion = useAppSelector(s => (s.chat.activeSlot ? s.chat.folderSuggestions?.[s.chat.activeSlot] : undefined))
  const followupTsBySlot = useAppSelector(s => s.chat.followups) ?? EMPTY_FOLLOWUPS
  // The ambient tip yields to functional surfaces that own the above-composer band
  const tipSuppressed = useAppSelector(s =>
    s.chat.messages.some(m => m.role === 'queued') ||
    // Question card only renders for its OWNING slot (see the render-site
    // slot check below) -- suppression must match, or a question pending in
    // another running slot suppresses tips here forever.
    !!pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot) ||
    // The follow-up card occupies the same above-composer band. Cards are
    // slot-keyed, so read only the ACTIVE slot's entry — a card parked in
    // another session must not suppress tips here.
    (!!s.chat.activeSlot && !!s.chat.followups?.[s.chat.activeSlot]) ||
    // The folder-suggestion card takes the same slot inside the composer box the
    // tip does, and it can land on the FIRST turn — exactly when a tip is most
    // likely to be offered. It is actionable and one-shot where the tip is
    // ambient and re-offered, so the tip yields. Slot-keyed like the follow-up
    // card, so a card parked in another session must not suppress tips here.
    (!!s.chat.activeSlot && !!s.chat.folderSuggestions?.[s.chat.activeSlot]) ||
    // Active subagents render the progress bar in the same above-composer
    // zone the floating tip occupies — the tip always yields: never crowd
    // the queue/subagent surfaces.
    Object.values(s.chat.subagents).some(a => a.status === 'running' || a.status === 'tool' || a.status === 'pending') ||
    // Workflow runs render WorkflowProgressBar in the same band — but only
    // runs belonging to THIS slot show a bar here, so filter by ownership or
    // a terminal run parked in another slot would suppress tips everywhere
    // forever.
    Object.values(s.chat.workflowRuns ?? {}).some(r => runBelongsToSlot(r.sessionKey, s.chat.activeSlot) && (r.status === 'running' || r.status === 'finished' || r.status === 'failed' || r.status === 'cancelled'))
  ) || knowledgeFetch.loading || knowledgeFetch.results.length > 0
  // Split View state is declared up here (not at its usage site) because the
  // tip hook below must know about it: in split mode SessionGridView replaces
  // the composer, TipCard never renders, and an unblocked hook would fetch a
  // tip + record it as shown, silently burning the 6h cadence.
  const [splitMode, setSplitMode] = useState(false)
  const [splitAnchor, setSplitAnchor] = useState<string | null>(null)
  // Temporary sessions ("no memory reads or writes") must never show
  // memory-personalized tips.
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
  // read.
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  const slotHasMore = useAppSelector(s => s.chat.slotHasMore)
  const slotOldestIndex = useAppSelector(s => s.chat.slotOldestIndex)
  const cursorIsForActiveSlot = useAppSelector(s => s.chat.slotCursorKey === s.chat.activeSlot)
  const loadingOlder = useAppSelector(s => s.chat.loadingOlder)
  const olderFailed = useAppSelector(s => s.chat.slotOlderError)
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
  // Per-slot session references staged by dragging a session onto this pane.
  // Persisted (sessionStorage) so a slot switch restores the refs belonging to
  // the slot being shown — which is also what stops one slot's staged refs from
  // smearing onto another.
  const sessionRefDrafts = useRef<Record<string, SessionRef[]>>(null!)
  if (sessionRefDrafts.current === null) sessionRefDrafts.current = loadSessionRefDrafts()
  const saveDraftsTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const saveDrafts = useCallback(() => { persistDrafts(drafts.current); persistFileDrafts(fileDrafts.current); persistPasteDrafts(pasteDrafts.current); persistSessionRefDrafts(sessionRefDrafts.current) }, [])
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
  const pendingInput = useAppSelector(s => s.chat.pendingInput)

  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])

  // Project is part of the roster's identity: re-pointing this slot at another
  // project changes which project-scoped agents exist. Derived here rather than
  // from `currentSlot`, which is computed further down the render body.
  const activeSlotProject = slots.find(s => s.key === activeSlot)?.project || undefined
  const { agents: installedAgents, defaultAgent } = useAgents(refreshTrigger, activeSlot ?? undefined, activeSlotProject)
  const [defaultAgentFailed, setDefaultAgentFailed] = useState(false)
  // Promotes an agent to the global default. Set-only: clearing the default lives on
  // the Agent Templates page, where the control is labelled and the outcome is visible.
  // Refresh goes through the store's global trigger rather than local state, because
  // every open picker (this one, each split pane, the Templates page) reads the same
  // setting — a per-hook refresh would leave sibling pickers showing the old default.
  // api.setDefaultAgent is called defensively: component tests mock the api module
  // partially, so the method can be absent under test.
  const toggleDefaultAgent = useCallback((name: string) => {
    setDefaultAgentFailed(false)
    Promise.resolve(api.setDefaultAgent?.(name))
      .then(() => dispatch(triggerRefresh()))
      .catch(() => setDefaultAgentFailed(true))
  }, [dispatch])
  const { open: agentDropdown, setOpen: setAgentDropdown, filter: agentFilter, setFilter: setAgentFilter, dropdownRef: agentDropdownRef, inputRef: agentInputRef, filtered: filteredAgentsByName } = useFilteredDropdown(installedAgents)
  const filteredAgents = filteredAgentsByName
  const availableModels = useAvailableModels()
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

  // pendingModel is the model for the NEXT new slot, and it is deliberately
  // left EMPTY unless the user explicitly picks one (switchModel below).
  //
  // It used to be seeded at mount from the backend resolver. That resolver
  // answers "what would run", which is right for the composer chip but wrong as
  // a session-create value: a session's model is a permanent pin (the runtime
  // reads `slot.model or agent_model`, so a set slot.model wins for every later
  // turn). Seeding it pinned every new chat to whatever the four-tier chain
  // happened to resolve at page load, so an agent left on Auto never
  // re-resolved and later changes to the agent or the global default never
  // reached the session (#2035).
  //
  // Sending nothing is what preserves the chain. `SessionManager.get_or_create`
  // documents that a `None` model "falls back to the global agent.model config
  // -- but only when the named agent does not pin its own model ... and the
  // global is not a sentinel value like 'auto', in which case it stays None to
  // let the backend resolve from the agent's own JSON config". So omitting it
  // honours the crew pin, the template pin, the global default and Auto, in that
  // order, at session-create time.
  //
  // Sending the literal 'auto' would NOT be equivalent: it is truthy, so it
  // short-circuits `slot.model or agent_model` and would override a template or
  // global pin the user did configure.
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
  // Holds the exact text a widget action pre-filled into the composer, so the
  // eventual user-initiated send can be tagged meta.origin='widget' for
 // forensic attribution. Set on widget pre-fill, consumed
  // and cleared in send(). A genuine from-scratch turn never sets this.
  const widgetPrefillRef = useRef<string | null>(null)
  // Token (`${slotKey}:${ts}`) of the most recently consumed composer prefill.
  // Guards the per-slot draft-restore effect against React.StrictMode's mount
  // double-invoke: the first invoke consumes+removes PREFILL_STORAGE_KEY and
  // seeds the composer, so without this the second invoke would find no stored
  // prefill and reset the composer to the (empty) incoming draft — the artifact
  // companion panel mounts ChatPage fresh, so it hits this double-invoke every
  // first open. See the draft-restore effect below.
  const consumedPrefillRef = useRef<string | null>(null)

  // Auto-dismiss prefill hint after 10 seconds
  useEffect(() => {
    if (!prefillHint) return
    const t = setTimeout(() => setPrefillHint(false), 10000)
    return () => clearTimeout(t)
  }, [prefillHint])

  // Drain the error hand-off channel ("Ask the agent" on an error surface).
  // sessionStorage rather than Redux because the root ErrorBoundary's button has
  // to work after a hard reload, when the store it would have dispatched to is
  // gone. Feeding pendingInput keeps a single downstream prefill path.
  //
  // Two triggers: on mount (arriving from another route, or a full reload) and on
  // the subscription (an error surface inside chat hands off with no route
  // change, so nothing remounts).
  useEffect(() => {
    if (embedded) return
    // Wait for a slot before consuming. The channel is SINGLE-USE, and on the
    // hard-nav path (the root ErrorBoundary reloads the page) this effect runs
    // with activeSlot still null: the pending-input consumer then cannot persist
    // the prompt as a draft, and the slot-restore that follows overwrites the
    // composer — losing the prompt for good. The 60s hand-off TTL covers the wait,
    // and this effect re-runs once the slot appears.
    if (!activeSlot) return
    const drain = () => {
      const prompt = consumeChatHandoff()
      if (!prompt) return
      // APPEND when the composer already holds unsent text — same hazard, and
      // same helper, as `followupAddToSession` below: the pending-input consumer
      // replaces the draft AND persists it, so a plain set would silently
      // destroy whatever the user was mid-way through typing. This is reachable
      // precisely because the subscription fires with no route change, while
      // error surfaces INSIDE chat (a failed PR action, a message that failed to
      // render) hand off from under a composer in use.
      // Merge against the text that actually belongs to `activeSlot`. On the
      // hard-nav path the composer may not have adopted this slot's stored draft
      // yet — `composerSlotRef` lags `activeSlot` — and merging against an empty
      // composer would make the pending-input consumer persist the prompt OVER
      // the stored draft. When they agree, the live composer value is the truth.
      const base = composerSlotRef.current === activeSlot
        ? inputRef.current
        : drafts.current[activeSlot] ?? ''
      dispatch(setPendingInput(mergeIntoDraft(base, prompt)))
    }
    drain()
    return subscribeChatHandoff(drain)
  }, [dispatch, embedded, activeSlot])

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
    const storedSessionRefs = loadSessionRefDrafts()
    for (const [k, v] of Object.entries(storedSessionRefs)) { if (!(k in sessionRefDrafts.current)) sessionRefDrafts.current[k] = v }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
    const prevSlotVal = prevSlot.current
    prevSlot.current = activeSlot
    const raw = sessionStorage.getItem(PREFILL_STORAGE_KEY)
    const draftFallback = activeSlot ? drafts.current[activeSlot] ?? '' : ''
    if (raw) {
      try {
        const { slotKey, prompt, ts } = JSON.parse(raw)
        if (Date.now() - (ts ?? 0) > 30_000) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
        else if (slotKey === activeSlot) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); consumedPrefillRef.current = `${slotKey}:${ts}`; setInput(prompt) }
        else { setInput(draftFallback) }
      } catch { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
    } else if (prevSlotVal === activeSlot && !!activeSlot && consumedPrefillRef.current?.startsWith(`${activeSlot}:`)) {
      // StrictMode re-invoked this mount effect for the SAME active slot after
      // the first invoke already consumed+removed the prefill. The composer
      // already holds the staged prompt; a setInput(draftFallback) here would
      // wipe it back to the empty draft. Leave the composer as-is. (A genuine
      // slot switch changes activeSlot, so prevSlotVal !== activeSlot and this
      // branch cannot mask a real draft restore.)
    } else { setInput(draftFallback) }
    // Restore the incoming slot's staged file attachments (copy so the
    // live state array and the stored draft don't share a reference).
    setPendingFiles(activeSlot ? (fileDrafts.current[activeSlot] ?? []).slice() : [])
    // Staged folder references need no restore of their own: the chips derive
    // from `@rel/` tokens in the composer text, and the text draft restored
    // above is per-slot. A folder staged in slot A therefore reappears with
    // slot A's draft and never bleeds into slot B.
    // Restore the incoming slot's collapsed-paste blocks (deep copy so the live
    // state and the stored draft don't share references). Without this the
    // token text rehydrates from the text draft but its backing block is gone,
    // leaving a dead `[ Paste #N · M lines ]` literal in the input.
    setPasteBlocks(activeSlot
      ? (pasteDrafts.current[activeSlot] ?? []).map(b => ({ ...b }))
      : [])
    // Restore the incoming slot's staged session references (copy per record so
    // the live state and the stored draft never share a reference).
    setPendingSessions(activeSlot
      ? (sessionRefDrafts.current[activeSlot] ?? []).map(r => ({ ...r }))
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
    if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
    flushDrafts()
  }, [flushDrafts])
  // Flush pending draft save on tab close / refresh (debounce may not fire)
  useEffect(() => {
    const h = () => {
      if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
      if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
      if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
      if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
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
  // Staged folder chips DERIVE from the composer text: an `@rel/` token is the
  // only form of a folder reference the agent receives, so token presence is
  // the single source of truth. There is no parallel state to leak across
  // slots, clear on send, or sync against hand-edits — inserting the token
  // stages the chip, deleting the token (by any means) unstages it, and the
  // per-slot text draft persists the reference across slot switches for free.
  const pendingDirs = useMemo(() => parseDirTokens(input).map(t => t.rel), [input])
  // Exact `@rel` composer token recorded per PICKER-PICKED file, so the file
  // chip's remove control can strip precisely the token the pick inserted —
  // the same remove contract folder chips have. Uploaded/dropped files never
  // get an entry (they have no token), so their remove stays state-only. A
  // ref, not state: it never drives rendering. Entries die with their chip.
  const pickedFileTokens = useRef<Record<string, string>>({})
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
  // Session references staged by dragging a session from the list onto this
  // pane. Serialized as LINKS on send — never the referenced transcript.
  const [pendingSessions, setPendingSessions] = useState<SessionRef[]>([])
  const pendingSessionsRef = useRef(pendingSessions)
  useEffect(() => {
    pendingSessionsRef.current = pendingSessions
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setSessionRefDraft(sessionRefDrafts.current, s, pendingSessions)
      saveDraftsDebounced()
    }
    // draft key is composerSlotRef; slot-change effect handles that transition.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingSessions, saveDraftsDebounced])
  /** Stage a dropped session. Ignores duplicates and overflow (addSessionRef
   *  returns the same array, so this is a no-op re-render-free path). */
  const stageSessionRef = useCallback((ref: SessionRef) => {
    setPendingSessions(prev => addSessionRef(prev, ref))
  }, [])
  const unstageSessionRef = useCallback((key: string) => {
    setPendingSessions(prev => removeSessionRef(prev, key))
  }, [])
  /**
   * Whether a dropped session reference has a composer to land in.
   *
   * This predicate exists because the same defect appeared on three separate
   * surfaces: a drop is accepted, `pendingSessions` is set, and nothing ever
   * renders it — a silent black hole. Naming the condition once means a fourth
   * surface cannot quietly reintroduce it.
   *
   *  - `splitMode`: SessionGridView renders its own ChatInput per cell and
   *    ChatPage's composer is unmounted.
   *  - no `activeSlot`: ChatPage renders an empty state instead of a composer,
   *    the per-slot persist effect has no key to write under, and the
   *    slot-restore effect resets `pendingSessions` to `[]` on the next
   *    activation — so the ref is discarded rather than merely hidden.
   *
   * (embed 'sessions' mode needs no clause: it renders no chat pane at all, so
   * there is no `chatPaneEl` to hand over.)
   */
  const canStageSessionRef = !splitMode && !!activeSlot
  // The chat pane element, held in STATE (not a ref) because ChatSidebar portals
  // its drop zone into it — a ref's assignment does not re-render, so the portal
  // would never mount on the first paint.
  const [chatPaneEl, setChatPaneEl] = useState<HTMLDivElement | null>(null)
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
    queryFn: () => api.sttConfig() as Promise<{ streaming?: boolean; enabled?: boolean; dictation_panel?: boolean; available?: boolean; provider?: string }>,
  })
  const sttStreaming = !!sttCfg?.streaming
  const sttEnabled = !!sttCfg?.enabled
  // The backend probes for the provider's binary and reports `available`.
  // Default true so a not-yet-loaded config doesn't flash the modal; the
  // separate sttConfigLoaded guard already covers the pre-load case.
  const sttAvailable = sttCfg?.available !== false
  const sttProvider = sttCfg?.provider || ''
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
  const [voiceDisabledReason, setVoiceDisabledReason] = useState<'disabled' | 'unavailable' | 'remote'>('disabled')
  const frozenInputRef = useRef<string | null>(null)
  // Caret snapshot taken alongside frozenInputRef, so a streaming partial (and
  // the final that replaces it) keeps inserting at the same spot. The batch
  // path leaves both null and reads the LIVE composer caret instead.
  const frozenCaretRef = useRef<{ start: number; end: number } | null>(null)
  // Live composer caret, kept current by ChatInput (onSelect / click / typing).
  // Dictation splices the transcript in HERE instead of always appending at end.
  const voiceCaretRef = useRef<{ start: number; end: number } | null>(null)
  // Caret offset ChatInput should restore after a dictation-driven value update
  // lands (set by the splice below, consumed + cleared inside ChatInput).
  const voicePendingCaretRef = useRef<number | null>(null)
  // Drops late-arriving partials/finals for the CURRENT slot after a send.
  // `stop()` is async (up to 5s for backend close) — without this guard, a
  // delayed onFinal would repopulate the composer with text the user already
  // sent. Cross-SLOT safety is handled separately by session-scoped routing
  // (see applyVoiceText + voice.sessionOwner).
  const sttDisarmedRef = useRef(false)
  // Narrower sibling of `sttDisarmedRef`, for a MANUAL STOP of a streaming
  // recording that already put a hypothesis in the composer.
  //
  // One flag was doing two jobs, and a manual stop only wants one of them.
  // `applyVoiceText` APPENDS (`base + ' ' + text`), so the close-time final
  // landing on a composer that already holds the hypothesis duplicates the
  // utterance ("hello hello") — that has to stay suppressed. But `onPartial`
  // REPLACES the region at the frozen boundary, and the hook re-emits
  // `finals.join(' ')` through it on every `final` message while `stop()`
  // deliberately leaves the socket draining. Suppressing that too meant every
  // segment Transcribe stabilised AFTER the release was dropped, so the user
  // was left holding the last UNSTABLE hypothesis. On a push-to-talk hold that
  // is the common case, not a corner: the hold is short, so the tail of the
  // utterance is exactly the part still unstable at release.
  //
  // So: this flag suppresses the append only, and leaves the drain's own
  // corrections free to keep replacing the region until the socket closes.
  // Cancel, send and slot-switch still want EVERYTHING suppressed and keep
  // using `sttDisarmedRef` — the user discarded, already sent, or left.
  const sttAppendDisarmedRef = useRef(false)
  // The composer content UP TO the end of the region onPartial last inserted,
  // plus the whole value it wrote. Dictation splices at the caret, so it can sit
  // mid-draft with an existing tail after it — and typing after the release
  // lands at the restored caret, i.e. between the two. Anchoring on the PREFIX
  // (not the whole value) is what lets a drain-time update replace the corrected
  // region and keep everything after it verbatim; anchoring on the whole value
  // would fail its own startsWith check mid-draft and drop the correction.
  // The full value distinguishes "the user typed" from "nothing changed", which
  // decides whether the caret may be moved.
  const lastDictationAnchorRef = useRef<string | null>(null)
  const lastDictationValueRef = useRef<string | null>(null)
  // Sticky for the whole post-stop drain: once the user has typed, the caret is
  // theirs until dictation restarts. Recomputing "did they edit?" per update is
  // not enough — after the first correction carries the suffix across, the
  // composer matches what we wrote again, so a second correction would decide
  // nothing was edited and yank the caret back in front of the typed text.
  const postStopEditedRef = useRef(false)
  // Suppresses ONLY the auto-submit route, and unlike the append flag it is set
  // by EVERY manual stop of a streaming recording — including a cold-stream stop
  // where no partial landed. "Stop capturing" is never "send": without this, a
  // short press against a cold stream leaves the endpointer armed, and a
  // trailing final's endpoint verdict submits the turn the user never asked to
  // send. The append flag cannot carry this, because with no partial landed the
  // close-time final is the only copy of the utterance and must still land.
  const sttEndpointDisarmedRef = useRef(false)
  // A frozen caret is a position in the composer as it stood at the release. Once
  // the user edits after that, it can go stale in two ways, and both corrupt the
  // splice: a RANGE (dictating over a selection replaces it) whose selection they
  // have since typed over, and an OFFSET whose meaning shifts when they edit text
  // BEFORE it. Rebase it onto the current text instead of trusting or discarding
  // it wholesale — discarding it would put the transcript after text they wrote
  // later, trusting it would cut into text they wrote earlier.
  const rebaseFrozenCaret = useCallback(() => {
    if (!sttEndpointDisarmedRef.current) return
    const frozen = frozenCaretRef.current
    const released = lastDictationValueRef.current
    const cur = inputRef.current ?? ''
    // Untouched composer: a selection here is still a legitimate replacement
    // target, which is what dictating over a selection is supposed to do.
    if (!frozen || released === null || cur === released) return
    // Bound the edit to the region between the longest common prefix and suffix.
    let lcp = 0
    while (lcp < released.length && lcp < cur.length && released[lcp] === cur[lcp]) lcp++
    let lcs = 0
    while (
      lcs < released.length - lcp && lcs < cur.length - lcp &&
      released[released.length - 1 - lcs] === cur[cur.length - 1 - lcs]
    ) lcs++
    const start = frozen.start
    let next: number
    if (start <= lcp) next = start                                    // edit is after it
    else if (start >= released.length - lcs) next = start + (cur.length - released.length)
    else next = voiceCaretRef.current?.start ?? start                 // edit straddles it
    next = Math.max(0, Math.min(next, cur.length))
    frozenCaretRef.current = { start: next, end: next }
  }, [])
  // The hook's EFFECTIVE streaming mode: streaming is only truly active when the
  // config asks for it AND the browser supports it (AudioWorklet/WS). Mirrored
  // from voice.streamEnabled (set by the effect below, once `voice` exists) so
  // the disarm + cross-slot-routing decisions gate on what the hook ACTUALLY
  // runs, not the raw config. Keying those on the config alone would, in a
  // browser without AudioWorklet, treat a batch-fallback session as streaming
  // and disarm/drop its (only) transcript.
  const streamEnabledRef = useRef(false)
  // Forward ref to send() (defined far below) so the streaming endpointer's
  // auto-submit callback — wired into the voice hook here, above send — can
  // fire it. Kept fresh by an effect after send is declared.
  const sendRef = useRef<((optionText?: string, targetSlot?: string) => void) | null>(null)
  // Deliver a finished transcript to the slot that INITIATED the recording,
  // using the session id useVoiceInput snapshotted at record-start (falling back
  // to the active slot for the ordinary same-slot case). Same-slot splices into
  // the live composer; a background slot gets it appended to its persisted draft
  // (recoverable, shown on return) instead of leaking into the active session or
  // being dropped. Mirrors handleOptimizeResult's cross-slot routing.
  // Splice a dictation transcript into `base` at the caret (frozen snapshot
  // when streaming, else the live caret), returning the new value and the caret
  // offset to restore. Falls back to appending when no caret is known (e.g. the
  // composer was never focused).
  const spliceDictation = useCallback((base: string, text: string): { value: string; caret: number } => {
    const caret = frozenCaretRef.current ?? voiceCaretRef.current
    // An empty transcript (e.g. a silent streaming partial) must NOT mutate the
    // draft: splicing "" across a selection would delete the selected range.
    // Leave the base untouched and collapse the caret to the insertion point.
    if (!text) return { value: base, caret: caret ? Math.min(caret.start, base.length) : base.length }
    if (!caret) {
      const value = base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text
      return { value, caret: value.length }
    }
    const start = Math.min(caret.start, base.length)
    const end = Math.min(caret.end, base.length)
    const before = base.slice(0, start)
    const after = base.slice(end)
    // Leading space only when joining onto a non-space char, so mid-sentence
    // dictation doesn't glue onto the preceding word.
    // Leading/trailing space uses whitespace-class checks (not only ' ') so a
    // caret beside a newline or tab doesn't get an unwanted literal space.
    const lead = before && !/\s$/.test(before) && !/^\s/.test(text) ? ' ' : ''
    const trail = after && !/^\s/.test(after) && !/\s$/.test(text) ? ' ' : ''
    const insert = lead + text
    return { value: before + insert + trail + after, caret: before.length + insert.length }
  }, [])
  const applyVoiceText = useCallback((text: string, sessionId: string | null, origin: TranscriptOrigin) => {
    // Disarmed after a send (streaming) — the transcript was already sent, so
    // drop it for EVERY route. Checked FIRST (before the cross-slot branch) so a
    // late final can't slip the already-sent text back into the originating
    // slot's draft.
    //
    // `sttAppendDisarmedRef` covers the narrower case: a manual stop whose
    // hypothesis is already in the composer. This route APPENDS, so letting the
    // close-time final through there would duplicate the utterance.
    //
    // Both are STREAMING-only states — every site that arms them is gated on
    // streaming — so they are keyed on where the text came from, not on the mode
    // selected right now. A batch transcription can outlive the page that started
    // it and land after streaming was switched on, and its onstop transcript is
    // always the only copy: suppressing it would delete what the user said.
    if (origin === 'stream' && (sttDisarmedRef.current || sttAppendDisarmedRef.current)) return
    const target = sessionId ?? activeSlotRef.current
    const append = (base: string) => (base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text)
    // Splice into the LIVE composer only when the target slot is both the active
    // slot AND the slot the composer's `input` currently belongs to. On a slot
    // switch, activeSlotRef updates synchronously in render, but the composer's
    // draft-restore + composerSlotRef advance run in LATER effects — splicing in
    // that unsettled window would let the pending draft restore overwrite the
    // transcript. Otherwise route to the target slot's persisted draft.
    const onScreen = target === activeSlotRef.current && composerSlotRef.current === target
    if (!onScreen) {
      // Off-screen (or not-yet-settled) delivery is BATCH ONLY. Streaming splices
      // its live hypothesis into `input`, which is flushed into the draft on
      // switch, so a cross-slot append would double it — a streaming final that
      // lands off its slot is dropped (pre-existing behaviour). Batch has no
      // partial, so appending to the slot's draft is unambiguous. Keyed on the
      // text's origin rather than the live streaming setting, which is a proxy
      // that goes wrong for a batch transcript arriving after the mode changed.
      if (!target || origin === 'stream') return
      const next = append(drafts.current[target] ?? '')
      setDraft(drafts.current, target, next)
      // Mid-switch guard: if the composer still belongs to `target` (activeSlot
      // has advanced in render but the outgoing-slot persist effect hasn't run
      // yet), that effect will flush inputRef.current into drafts[target] and
      // would overwrite this transcript with the pre-transcript input. Carry the
      // appended value into inputRef too so the flush preserves the transcript.
      if (composerSlotRef.current === target) inputRef.current = next
      saveDrafts()
      return
    }
    // Foreground: streaming seeds frozenInputRef/frozenCaretRef in onPartial
    // (the pre-dictation snapshot); the batch path never fires onPartial so both
    // are null — fall back to the live composer text + caret so the transcript
    // inserts at the cursor instead of overwriting (or blindly appending to)
    // what the user typed.
    rebaseFrozenCaret()
    const spliced = spliceDictation(frozenInputRef.current ?? inputRef.current ?? '', text)
    // Only arm the caret restore when the value actually changes. If a streaming
    // final equals the last partial, setInput is a no-op and the restore effect
    // (keyed on `value`) never fires — leaving a stale pending caret that would
    // hijack the user's NEXT edit.
    if (spliced.value !== inputRef.current) {
      setInput(spliced.value)
      voicePendingCaretRef.current = spliced.caret
    }
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
  }, [saveDrafts, spliceDictation, rebaseFrozenCaret])
  const voice = useVoiceInput(
    applyVoiceText,
    {
      streaming: sttStreaming,
      sessionId: activeSlot,
      onPartial: useCallback((text: string, sessionId: string | null) => {
        // Streaming partials only fire while the originating slot is on screen
        // (switching slots stops the stream), so a partial attributed to any
        // other slot is a late straggler — drop it rather than smear a
        // half-word into the wrong session.
        if (sessionId && sessionId !== activeSlotRef.current) return
        // Deliberately NOT gated on `sttAppendDisarmedRef`: after a manual stop
        // the socket is still draining, and this is the route that carries the
        // stabilised text. It REPLACES the region at the frozen boundary rather
        // than appending, so letting it keep firing cannot duplicate anything —
        // it is what turns the last unstable hypothesis into the real transcript.
        if (sttDisarmedRef.current) return
        // Snapshot the pre-dictation text AND caret on the first partial
        // (before setInput, so the updater stays pure — no ref mutation inside a
        // function React may invoke twice) so every later partial and the final
        // insert at the same spot, replacing the growing hypothesis.
        if (frozenInputRef.current === null) {
          frozenInputRef.current = inputRef.current
          // Do not clobber a caret a cold-stream stop already froze: that one is
          // the release-time insertion point, and the live caret is now wherever
          // the user has typed since.
          frozenCaretRef.current = frozenCaretRef.current ?? voiceCaretRef.current
        }
        rebaseFrozenCaret()
        const spliced = spliceDictation(frozenInputRef.current ?? '', text)
        // Everything up to and including the dictated insertion. What follows it
        // in the composer (an existing tail, and anything typed after release) is
        // carried across untouched rather than rebuilt from the snapshot.
        const anchor = spliced.value.slice(0, spliced.caret)
        let next = spliced.value
        // Where the caret should end up. Defaults to the end of the dictated
        // region (the ordinary "we own the composer" case); the post-stop branch
        // overrides it when the text is the user's to steer.
        let caretTarget: number | null = spliced.caret
        if (sttEndpointDisarmedRef.current) {
          // POST-STOP DRAIN. The user has let go, so as far as they are concerned
          // dictation is over and they may already be typing — at the restored
          // caret, which for mid-draft dictation sits in the MIDDLE of the text.
          // Rebuilding from the frozen snapshot would delete that typing, so
          // verify our own prefix is still intact and splice the correction in
          // ahead of whatever now follows it. If the prefix cannot be verified
          // the user edited inside the dictated region; leave the composer alone
          // rather than guess — same policy as cancelVoice, for the same reason:
          // a heuristic here deletes user-authored text.
          //
          // Gated on the ENDPOINT flag, not the append flag: a cold-stream stop
          // deliberately leaves the append armed (the close-time final is the
          // only copy of the utterance), so keying off it would skip this branch
          // in exactly the case where it is still needed.
          //
          // During recording this does not apply: the region is being actively
          // rewritten and that behaviour is unchanged.
          const prev = lastDictationAnchorRef.current
          const cur = inputRef.current ?? ''
          // The composer now holds a copy of the utterance, which is the exact
          // condition the append flag encodes — so close the close-time route
          // here rather than at stop time. stopVoice could not decide this: with
          // frozenInputRef still null it had to leave the append armed, because
          // back then the close-time final really was the only copy. Once a drain
          // partial has landed that is no longer true, and letting the final
          // through would re-splice from the snapshot and delete whatever the
          // user typed after the release.
          sttAppendDisarmedRef.current = true
          // Checked OUTSIDE the anchor guard: on a cold stream the first drain
          // partial has no anchor yet, but the user may already have typed since
          // the release, and their caret must still be left alone.
          if (cur !== lastDictationValueRef.current) postStopEditedRef.current = true
          // A null anchor means no partial has landed yet — the cold-stream stop.
          // This IS the first write: there is nothing to preserve and nothing to
          // verify, and returning here would drop the utterance. Fall through to
          // the plain write, which establishes the anchor for the next update.
          // The typed text is inside the snapshot (taken from the LIVE composer)
          // and the insertion point is the caret stopVoice froze at the release,
          // so the transcript lands where the user was speaking rather than after
          // what they wrote afterwards.
          if (prev !== null) {
            if (!cur.startsWith(prev)) return
            next = anchor + cur.slice(prev.length)
            if (postStopEditedRef.current) {
              // Their caret is in their own text, so it must not be dragged to the
              // end of the dictation — but NOT arming it is not "leaving it
              // alone" either: React replaces the textarea value and the browser
              // resets the DOM caret to the end. Re-arm it at the same LOGICAL
              // spot, shifted by how much the region ahead of it grew or shrank.
              const live = voiceCaretRef.current
              caretTarget = live && live.start >= prev.length
                ? live.start + (anchor.length - prev.length)
                : null
            }
          } else if (postStopEditedRef.current) {
            // Cold-stream first write with typing already done: there is no old
            // anchor to measure a shift against, and the value commit leaves the
            // caret at the end — which is past their text, a sane place to be.
            caretTarget = null
          }
        }
        if (next !== inputRef.current) {
          setInput(next)
          if (caretTarget !== null) voicePendingCaretRef.current = caretTarget
        }
        lastDictationAnchorRef.current = anchor
        lastDictationValueRef.current = next
      }, [spliceDictation, rebaseFrozenCaret]),
      // Semantic endpointing (stt.endpointing) judged the utterance complete:
      // auto-submit. The composer already holds the streamed transcript via
      // onPartial, and send() reads inputRef.current + stops the live capture
      // itself (its recording+streaming branch), so this is the same path as
      // pressing Enter mid-dictation — just triggered by the backend verdict.
      onEndpoint: useCallback(() => {
        // A manual stop is the user saying "stop capturing", so a backend
        // endpoint verdict arriving during the drain must not turn that into an
        // unrequested send. The endpoint flag is what covers a COLD-stream stop,
        // where no partial landed and the append flag is deliberately left unset
        // so the close-time final can still deliver the utterance.
        if (sttDisarmedRef.current || sttAppendDisarmedRef.current || sttEndpointDisarmedRef.current) return
        sendRef.current?.()
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
  // Keep streamEnabledRef in sync with the hook's EFFECTIVE streaming mode (see
  // its declaration above). send()/the slot-switch effect/toggleVoice read it to
  // decide whether a draining final should be disarmed — which must reflect what
  // the hook actually runs, not the raw config.
  useEffect(() => { streamEnabledRef.current = voice.streamEnabled }, [voice.streamEnabled])
  // Re-arm when the user explicitly (re)starts recording — wrap toggle.
  // Depend on the individual stable members actually read so this callback
  // is only re-created when they change. `[voice]` would recreate every
  // render (hooks don't memoize their return by default), re-rendering all
  // child components that receive `toggleVoice` as a prop.
  /**
   * Start voice capture, with the gating and state resets every entry point
   * needs. Extracted from `toggleVoice` so the push-to-talk key driver
   * (`usePushToTalk`) goes through the SAME preamble — calling `voice.start()`
   * raw would skip the disarm reset and the frozen-snapshot clear, and a
   * key-started dictation would then be rebuilt from stale pre-dictation text.
   *
   * RETURNS the start promise. Load-bearing, not incidental: `usePushToTalk`
   * chains on it to stop a session whose async startup only finished after the
   * key was already released. Swallowing it here leaves that guard unreachable
   * and the microphone open with nothing holding it.
   *
   * `silent` suppresses the "voice needs setting up" modal. The key binding is a
   * PASSIVE trigger — a bare modifier is also an ordinary typing modifier — so a
   * keystroke that used to type a character must never throw an unsolicited
   * dialog. Clicking the mic button is a deliberate request and still explains
   * itself.
   */
  const startVoice = useCallback((opts?: { silent?: boolean }): Promise<void> | void => {
    // Remote instances (inside an iframe) cannot reliably capture audio from
    // the parent machine's mic due to cross-origin delegation constraints.
    if (isEmbeddedPane()) {
      if (!opts?.silent) { setVoiceDisabledReason('remote'); setVoiceSetupOpen(true) }
      return
    }
    // Starting a recording while server-side STT is disabled would capture
    // audio that never gets transcribed. Point the user at the enable setting
    // instead — unless this came from the keyboard (see `silent`).
    if (!sttConfigLoaded || !sttEnabled || !sttAvailable) {
      if (!opts?.silent) { setVoiceDisabledReason(sttEnabled && !sttAvailable ? 'unavailable' : 'disabled'); setVoiceSetupOpen(true) }
      return
    }
    // Exclusive sessions: the mic is a single shared device, so refuse to
    // START a new recording while another session's transcription is still
    // in flight (voice.transcribing). This is what keeps voice single-session
    // — no two recordings/transcriptions ever overlap — so the busy state
    // needs only a single owner and can never be misattributed.
    if (voice.transcribing) return
    sttDisarmedRef.current = false
    sttAppendDisarmedRef.current = false
    sttEndpointDisarmedRef.current = false
    // Reset stale snapshot from a prior session that ended without
    // finals — otherwise onPartial sees a non-null ref, skips
    // re-snapshotting, and text typed between sessions is dropped.
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
    return voice.start()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.transcribing, voice.start, sttEnabled, sttConfigLoaded, sttAvailable])

  /** Stop voice capture. Always allowed — only starting is gated. */
  const stopVoice = useCallback(() => {
    // Manual stop of a STREAMING recording: streamStop() drains the socket
    // asynchronously, so more of the utterance can still arrive. Two routes are
    // in play and they need OPPOSITE treatment, which is why this sets the
    // narrow flag rather than the blanket one:
    //
    //   - `applyVoiceText` (close-time) APPENDS. The composer already holds the
    //     hypothesis, so letting it through duplicates the utterance. Suppress.
    //   - `onPartial` (drain-time) REPLACES the region at the frozen boundary,
    //     and the hook re-emits `finals.join(' ')` through it as Transcribe
    //     stabilises each segment. That is the authoritative text. Keep armed.
    //
    // Only suppress once the composer actually holds a copy of the speech, which
    // is exactly what frozenInputRef being set means (onPartial snapshots it on
    // the FIRST partial, then writes each hypothesis into `input`).
    //
    // With frozenInputRef still null NO partial has landed, so the composer
    // holds nothing and the close-time final is the ONLY copy of the utterance:
    // suppressing there silently deletes what the user just said. That is the
    // ordinary case for a short press against a COLD stream, where the release
    // beats the server's first partial. (Batch is likewise never suppressed
    // here: its onstop transcript is always the only copy.)
    if (streamEnabledRef.current && frozenInputRef.current !== null) {
      sttAppendDisarmedRef.current = true
    }
    // Unconditional for a streaming stop: the auto-submit route must close even
    // when the append route stays open (the cold-stream case above).
    if (streamEnabledRef.current) {
      sttEndpointDisarmedRef.current = true
    }
    if (streamEnabledRef.current && frozenInputRef.current === null) {
      // COLD STREAM: no partial landed, so nothing has pinned the insertion point
      // yet. Freeze the CARET at the release, so a drain partial arriving after
      // the user has started typing still inserts where they were speaking
      // instead of after the text they wrote afterwards.
      //
      // Deliberately NOT freezing the text as well: with no partial landed the
      // close-time final is the only copy of the utterance and must splice into
      // the LIVE composer. Pinning the text here would make it rebuild from the
      // release-time snapshot and delete anything typed after the release —
      // trading a wrong insertion point for lost text.
      //
      // The value fingerprint is seeded too, so the first drain partial can tell
      // that the user has typed since the release and leave their caret alone.
      frozenCaretRef.current = voiceCaretRef.current
      lastDictationValueRef.current = inputRef.current
    }
    voice.stop()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.stop])

  const toggleVoice = useCallback(() => {
    if (voice.recording) stopVoice()
    else startVoice()
    // Depends on the individual member actually read (`voice.recording`), not the
    // whole `voice` object — `[voice]` would recreate this callback every render
    // and re-render every child that receives `toggleVoice`. No suppression is
    // needed here because the split into startVoice/stopVoice left this list
    // genuinely exhaustive.
  }, [voice.recording, startVoice, stopVoice])
  // Cancel (discard) the in-progress dictation — Esc. Batch simply drops the
  // pending audio (the hook's onstop skips transcription), so nothing lands in
  // the composer. Streaming additionally disarms the draining final AND removes
  // the live dictated region from the composer at the frozenInputRef boundary:
  // the region is recomputed with the same `spliceDictation` call onPartial used
  // (so it matches a mid-draft caret splice, not just an append), and we drop
  // exactly that region — preserving the pre-dictation text verbatim (including
  // its own trailing whitespace) AND any suffix typed after the dictation. When
  // the region can't be verified (the user replaced/edited it), leave the
  // composer unchanged rather than restoring the snapshot and losing that edit.
  // Uses voiceRef.current (not `voice`) so this prop stays referentially stable
  // and does not re-render the composer every render — matching toggleVoice.
  const cancelVoice = useCallback(() => {
    if (streamEnabledRef.current) {
      sttDisarmedRef.current = true
      // Remove the dictated region at the frozenInputRef boundary, preserving
      // the pre-dictation text EXACTLY (including its own trailing whitespace)
      // and any suffix the user typed after the dictation. onPartial rebuilt the
      // composer as `frozen [+ ' ' separator] + partial`, so reconstruct that
      // exact region and drop only it — never a blanket trailing-space strip.
      const cur = inputRef.current ?? ''
      const frozen = frozenInputRef.current
      const p = voiceRef.current.partial
      if (frozen !== null && p) {
        // Reconstruct the composer value through the SAME pure function that
        // wrote it. onPartial splices at the snapshotted caret, so for a
        // mid-draft caret the value is `before + lead + partial + trail + after`
        // — NOT `frozen + separator + partial`. Re-deriving the region with an
        // append-only formula failed `startsWith` for every mid-draft dictation
        // and fell through to the leave-unchanged branch, stranding the partial
        // in the draft. spliceDictation reads the same frozen caret, so this
        // reproduces the write exactly for both the append and mid-caret shapes.
        const written = spliceDictation(frozen, p).value
        if (cur.startsWith(written)) {
          // The composer still begins with exactly the region onPartial wrote.
          // Restore the pre-dictation text verbatim and keep any suffix the user
          // typed after it.
          setInput(frozen + cur.slice(written.length))
        }
        // else: the dictated region can't be verified exactly — the user edited
        // or replaced it (e.g. deleted the separator, or typed their own text
        // that merely ends in the same word as the partial). Leave the composer
        // UNCHANGED: a suffix-match heuristic here would delete user-authored
        // text ("say hello" -> "say"). The disarm above still drops the draining
        // final, so no dictation is committed; at worst the visible partial
        // lingers for the user to clear.
      }
      // (frozen===null, or no current partial: nothing verifiably removable —
      // leave the composer as-is rather than risk clobbering user text.)
      // Clear BOTH halves of the snapshot: they are written together in
      // onPartial and a surviving caret would aim the next session's first
      // splice at a position from the discarded one.
      frozenInputRef.current = null
      lastDictationAnchorRef.current = null
      lastDictationValueRef.current = null
      postStopEditedRef.current = false
      frozenCaretRef.current = null
    }
    voiceRef.current.cancel()
  }, [spliceDictation])

  // Push-to-talk / tap-to-toggle keyboard binding (default: hold right ⌥ on
  // macOS, ⌥⇧Space elsewhere). Routed through startVoice/stopVoice rather than
  // voice.start/stop so a key-driven dictation gets the same gating and
  // snapshot resets as the mic button, and `cancelVoice` — NOT the hook's raw
  // cancel — for the discard. Since capture now opens on the keydown, a fast
  // partial can reach the composer before the press is revealed as a chord or a
  // sub-threshold tap, and the raw cancel would strand that text; `cancelVoice`
  // runs the streaming rollback that removes the dictated region (and no-ops
  // when nothing verifiably removable was written). No `prewarm`: the driver
  // opens capture on the keydown itself, so there is no warm-up step to
  // schedule.
  usePushToTalk(
    {
      recording: voice.recording,
      // silent: a bare modifier is also an ordinary typing modifier, so a
      // keystroke must never raise the voice-setup modal on its own.
      start: () => startVoice({ silent: true }),
      stop: stopVoice,
      cancel: cancelVoice,
    },
    { disabled: !voiceInputSupported },
  )
  // Stop any in-flight recording and clear the streaming prefix when the user
  // switches slots. The mic is a single shared device, so a recording can't
  // follow the user to another session; a BATCH transcript is still delivered
  // to the originating slot via applyVoiceText's session-scoped routing (which
  // prevents cross-slot leakage precisely — no blanket disarm needed here).
  // Clearing frozenInputRef here means a streaming final that lands after a
  // switch-and-return rebases on the LIVE input, so edits made after returning
  // are preserved rather than clobbered by a stale snapshot.
  useEffect(() => {
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
    // Drop the previous slot's caret so dictating in a freshly switched-to slot
    // (without touching its composer) appends to that slot's draft instead of
    // inserting at the old slot's offset.
    voiceCaretRef.current = null
    // Streaming ONLY: disarm so a delayed streaming final arriving after this
    // switch is dropped instead of appended. Its live partial was already
    // flushed into the outgoing slot's draft, so appending the full final on
    // return would duplicate the dictated text ("hello hello"). Batch is NOT
    // disarmed — its single final is routed to the originating slot's draft by
    // applyVoiceText. (Cross-slot streaming delivery is a follow-up; streaming
    // is opt-in and off by default.)
    if (streamEnabledRef.current) sttDisarmedRef.current = true
    if (voiceRef.current.recording) voiceRef.current.toggle()
  }, [activeSlot])
  // True when the current voice session (owned by the slot where recording
  // actually started — see useVoiceInput's sessionOwner) is the slot on screen.
  // Gates the recording/transcribing UI so a session transcribing in the
  // background never shows a busy/locked mic in the session the user switched to.
  const voiceOwned = voice.sessionOwner === activeSlot
  // (Streaming-off teardown now lives in useVoiceInput — see its effect on
  // [streamEnabled, streamRecording, streamStop]. Routing through voice.toggle
  // here is racy because `useVoiceInput` flips its returned `recording` to the
  // batch value on the same render that `streamEnabled` goes false.)

  const tabsCtl = usePanelTabs(activeSlot)
  // An MCP App tab hosts a null-origin iframe with no storage: unmounting it
  // reloads the app and destroys whatever the user has drawn (see
  // docs/dashboard-iframe-hosts.md). The whole SidePanel subtree is normally
  // gated on `activityOpen`, so closing the panel would unmount it. While an app
  // tab is live we therefore keep the subtree MOUNTED and hide it instead — the
  // same hide-not-unmount rule SidePanel already applies to its own tab bodies.
  // With no app tab, behaviour is unchanged (the panel still unmounts on close,
  // preserving the existing exit animation).
  // Across ALL slots, not just the active one: with cross-slot hosting a frame
  // belonging to another chat lives in this panel subtree, so deciding to unmount
  // on the active slot's (possibly empty) tab list would destroy that canvas.
  const hasLiveAppTab = useAnyLiveAppTab()
  // Current slot only — unlike app tabs (hosted cross-slot via `allAppTabs`), a
  // Browser tab renders solely from the active slot's strip, and a background
  // slot's browser view already unmounts (its WebContentsView released) on the
  // slot switch. So keep-mounted follows THIS slot's tabs, not every slot's.
  const hasBrowserTab = tabsCtl.tabs.some(t => t.kind === 'browser')
  // Find/search pane state. Declared above handleFileOpen / handleOpenDiff so
  // those handlers can call search.close() directly when opening a dock panel
  // (the right-hand dock is a single slot and the file/diff panes are
  // render-gated behind !search.isOpen).
  const search = useMessageSearch(messages, activeSlot)
  const sourceLinkIndex = useRef(new PullRequestLinkIndex())
  // Self-managed GitLab hosts the operator authorized (config-only, read-only
  // here). Without them a pasted self-hosted MR link is not a Changes source.
  // No refetchInterval: polling this shared ['dashboardConfig'] key turned every
  // same-key observer into a poller and wrote a dashboard_config_read SEL entry
  // on each tick. Instead the WS 'slots' push carries the allowlist generation
  // (see useWebSocket), which invalidates this query only when the allowlist
  // actually changes — an edit on disk still propagates, without the churn.
  const { data: sourceHostCfg } = useQuery<{ gitlab_hosts?: string[]; jira_hosts?: string[] }>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
    staleTime: 30_000,
  })
  const sourceHosts = sourceHostCfg?.gitlab_hosts ?? []
  const jiraSourceHosts = sourceHostCfg?.jira_hosts ?? []
  // Read through refs by callbacks that must stay identity-stable (they are
  // handed to the sidebar, which re-renders every session row).
  const sourceHostsRef = useRef(sourceHosts)
  sourceHostsRef.current = sourceHosts
  const jiraSourceHostsRef = useRef(jiraSourceHosts)
  jiraSourceHostsRef.current = jiraSourceHosts
  const indexedSourceLinks = sourceLinkIndex.current.update(
    activeSlot,
    messages,
    sourceHosts,
    jiraSourceHosts,
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
  // Which Change / Issue tab is focused, PER SLOT and persisted (see
  // pullRequestLinks.SourceSelections). Per-slot because a single shared value
  // reconciles to the first link of whichever transcript is active, so switching
  // A→B→A dropped A's selection; persisted because the panel tab strip itself
  // survives reloads (mc-panel-tabs:<slot>) and a strip that comes back focused
  // on a tab the user never chose is the bug this closes.
  //
  // React state holds this window's view for rendering; commitSourceSelection
  // does the durable write, merging ONE slot into a freshly read snapshot so a
  // second chat window (a popped-out session shares this localStorage) cannot
  // publish its stale view of the slots it is not looking at. That means this
  // window's map can lag another window's writes to OTHER slots — harmless,
  // since only the active slot is ever read, and far better than losing them.
  const [sourceSelections, setSourceSelections] = useState(loadSourceSelections)
  const selectedSourceUrl = sourceSelection(sourceSelections, activeSlot, 'change')
  const selectedIssueUrl = sourceSelection(sourceSelections, activeSlot, 'issue')
  // The links sidebar chips asked to see, per slot and per kind.
  //
  // The chips and these panels do NOT scan for links the same way: the backend
  // chip scan (state.py) keeps every provider url in the transcript, while the
  // panel's extractor emits only links the AGENT surfaced — a pull request the
  // USER pasted is deliberately a Resource, not a Change. A chip is also drawn
  // from the whole server-side transcript, while the extractor sees only the
  // messages this window has loaded. Either gap would make the chip a dead end
  // (the panel would normalise straight back to the first link it does know), so
  // the clicked link is injected into the list for the session it belongs to.
  //
  // Keyed by slot AND kind, matching the two selection ledgers below. A single
  // last-one-wins record could not hold a revealed pull request and a revealed
  // issue at the same time: revealing an issue evicted the pull request, its
  // injection vanished from `panelSources`, and the Changes reconciliation then
  // normalised the selection onto a DIFFERENT pull request behind the user's back.
  //
  // Durable, for the same reason. The SELECTION pointing at a revealed link is
  // already persisted; without persisting the link too, a reload remembered the
  // url but could no longer produce it, and reconciliation performed that same
  // silent swap one page load later.
  const [revealedSources, setRevealedSources] = useState<RevealedSources>(loadRevealedSources)
  const revealedForSlot = activeSlot ? revealedSources[activeSlot] : undefined
  const revealedChange = revealedForSlot?.change ?? null
  const revealedIssue = revealedForSlot?.issue ?? null
  const panelSources = useMemo(() => (
    revealedChange && !sourceLinks.some(link => link.url === revealedChange.url)
      ? [revealedChange, ...sourceLinks]
      : sourceLinks
  ), [sourceLinks, revealedChange])
  const panelIssues = useMemo(() => (
    revealedIssue && !issueLinks.some(link => link.url === revealedIssue.url)
      ? [revealedIssue, ...issueLinks]
      : issueLinks
  ), [issueLinks, revealedIssue])
  // Fields whose durable write storage REFUSED, per slot. Storage then holds an
  // older url than the user's live choice, so adoption must not take it back
  // (see adoptSourceSelections). A ref, not state: it changes nothing on screen
  // and must not re-render.
  const unpersistedSelectionsRef = useRef<Record<string, Partial<Record<SourceLinkKind, boolean>>>>({})
  // Fields whose on-screen value is a provisional fallback rather than a real
  // choice. The value is the link count seen when the fallback was taken, so the
  // storage re-read below can retry only once the transcript has actually GROWN
  // rather than on every render. Cleared by an explicit pick or a successful
  // restore.
  const provisionalFallbackRef = useRef<Record<string, Partial<Record<SourceLinkKind, number>>>>({})
  const selectSource = useCallback((kind: SourceLinkKind, url: string, forSlot?: string) => {
    // `forSlot` is for a pick made on a session that is not on screen yet — a
    // sidebar chip switches sessions and selects in one gesture, and
    // activeSlotRef is assigned during RENDER, so at call time it still names the
    // chat being left.
    const slot = forSlot ?? activeSlotRef.current
    setSourceSelections(previous => withSourceSelection(previous, slot, kind, url))
    const outcome = commitSourceSelection(slot, kind, url)
    if (!slot) return
    // An explicit choice supersedes any provisional fallback for this field.
    const provisional = { ...provisionalFallbackRef.current[slot] }
    delete provisional[kind]
    provisionalFallbackRef.current = { ...provisionalFallbackRef.current, [slot]: provisional }
    const failed = { ...unpersistedSelectionsRef.current[slot] }
    // 'failed' means storage refused the write and still holds an older url;
    // 'unchanged' means storage already agrees. Both are explicit writes, so the
    // ledger records exactly whether this selection reached storage.
    if (outcome === 'failed') failed[kind] = true
    else delete failed[kind]
    unpersistedSelectionsRef.current = { ...unpersistedSelectionsRef.current, [slot]: failed }
  }, [])
  const selectSourceUrl = useCallback((url: string) => selectSource('change', url), [selectSource])
  const selectIssueUrl = useCallback((url: string) => selectSource('issue', url), [selectSource])
  // A RECONCILED pick is derived from the transcript, not chosen by the user, and
  // is deliberately IN-MEMORY ONLY — it never writes to storage.
  //
  // Persisting it bought nothing and cost correctness. The fallback is
  // deterministic (`sourceLinks[0]`), so a session where the user never picked a
  // tab recomputes the same answer on return without any stored value; the only
  // case persistence changes is a choice that DIFFERS from the first link, which
  // is exactly what an explicit click already records. Meanwhile every write from
  // here could destroy a real choice, because the fallback also fires whenever the
  // transcript on screen is provisional — `switchSlot.pending` serves a cached
  // transcript with `slotLoading` already false while the fetch is still in
  // flight, and a transcript missing a url is not proof the url is gone.
  //
  // The slot is marked provisional so the reconciliation effects know to look in
  // storage once for a better answer (see the effects below).
  const reconcileSelection = useCallback((kind: SourceLinkKind, url: string, seen = 0) => {
    const slot = activeSlotRef.current
    setSourceSelections(previous => withSourceSelection(previous, slot, kind, url))
    if (!slot) return
    provisionalFallbackRef.current = {
      ...provisionalFallbackRef.current,
      [slot]: { ...provisionalFallbackRef.current[slot], [kind]: seen },
    }
  }, [])
  // The panels normalize their own selection when the remembered url is not among
  // the tabs they render, and that is NOT a user choice — route it to the
  // in-memory path so it cannot overwrite storage. Before this split the panels
  // were handed the persisting callback, which made their normalize a durable
  // write and defeated the whole in-memory-only rule.
  const reconcileSourceUrl = useCallback(
    (url: string) => reconcileSelection('change', url, panelSources.length),
    [reconcileSelection, panelSources.length],
  )
  const reconcileIssueUrl = useCallback(
    (url: string) => reconcileSelection('issue', url, panelIssues.length),
    [reconcileSelection, panelIssues.length],
  )

  // Re-read storage for a slot whose on-screen value is a provisional fallback.
  //
  // Without this the fallback would stick for the life of the document: nothing
  // else re-reads storage in the window that wrote it — loadSourceSelections runs
  // only in the useState initializer, and the `storage` event never fires in the
  // writing document — so the user would keep seeing the fallback instead of the
  // tab they left open until a reload.
  //
  // Retried only when the transcript has GROWN since the fallback was taken. A
  // transcript is append-only within a slot, so growth is the only way a
  // previously-absent url can appear, and gating on it keeps this off the
  // per-render (and per-streaming-chunk) path. Membership in `links` is the
  // "the fetch proved it still exists" condition.
  const restoreFromStorage = useCallback((
    kind: SourceLinkKind,
    links: readonly { url: string }[],
  ): boolean => {
    const slot = activeSlotRef.current
    if (!slot) return false
    const seen = provisionalFallbackRef.current[slot]?.[kind]
    if (seen === undefined || links.length <= seen) return false

    const stored = sourceSelection(loadSourceSelections(), slot, kind)
    if (stored && links.some(link => link.url === stored)) {
      const provisional = { ...provisionalFallbackRef.current[slot] }
      delete provisional[kind]
      provisionalFallbackRef.current = { ...provisionalFallbackRef.current, [slot]: provisional }
      setSourceSelections(previous => withSourceSelection(previous, slot, kind, stored))
      return true
    }
    // Not there yet — wait for further growth rather than re-reading every render.
    provisionalFallbackRef.current = {
      ...provisionalFallbackRef.current,
      [slot]: { ...provisionalFallbackRef.current[slot], [kind]: links.length },
    }
    return false
  }, [])

  // Adopt a sibling window's writes. `storage` fires in every OTHER document on
  // this origin, so the window that did NOT write is the one that needs to
  // re-read. Without this, a window carries its mount-time view until reload and
  // two windows focused on the same session would each show their own last
  // choice. The event's newValue is ignored in favour of a full re-read, so the
  // loader's own validation and bounds apply to whatever a sibling wrote.
  //
  // The urls THIS window can actually SHOW go in with the read: adoption is
  // conditional on them for the active slot, which is what keeps two windows
  // with divergent transcripts from overwriting each other in a loop (see
  // adoptSourceSelections). The panel lists rather than the raw scan, so a link
  // revealed from a sidebar chip is not taken back by a sibling's write. Read
  // through a ref because the listener is registered once and must see the
  // current lists at event time.
  const availableSourceUrls = useMemo(() => ({
    change: panelSources.map(source => source.url),
    issue: panelIssues.map(issue => issue.url),
  }), [panelSources, panelIssues])
  const availableSourceUrlsRef = useRef(availableSourceUrls)
  availableSourceUrlsRef.current = availableSourceUrls
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.storageArea && event.storageArea !== localStorage) return
      // key === null is a storage.clear(), which does concern us. Otherwise
      // match the store's key prefix — the selection lives in one key per
      // (slot, kind), so there is no single literal to compare against.
      if (event.key !== null && !isSourceSelectionKey(event.key)) return
      setSourceSelections(previous => adoptSourceSelections(
        previous,
        activeSlotRef.current,
        availableSourceUrlsRef.current,
        unpersistedSelectionsRef.current,
      ))
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

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
    // Reconciled against the list the PANEL renders, not the raw transcript scan:
    // a link revealed from a sidebar chip is a real, user-chosen tab, and judging
    // it against the scan alone would normalise the selection straight off it.
    // A previous provisional render may have fallen back in memory while storage
    // still holds the tab the user chose; look there first once links appear.
    if (restoreFromStorage('change', panelSources)) return
    if (panelSources.length === 0) {
      // Changes is a permanently pinned tab (SidePanel.syncPinned) — never
      // auto-close it here. Just clear the source selection; the tab stays put
      // and renders its empty state until sources are detected again.
      //
      // Two guards, both load-bearing:
      //  - transcript LOADED, not merely empty. switchSlot.rejected (a dropped
      //    history fetch) empties `messages` AND drops slotLoading in one reducer
      //    pass, so the guard above does not hold; since the selection is durable,
      //    clearing there would outlive the failure and lose the tab on retry.
      //  - something to clear. commitSourceSelection enumerates storage to decide
      //    whether the value already matches, and these effects re-run on every
      //    streaming chunk (the link index hands back a fresh array per chunk), so
      //    an unconditional clear costs a full enumeration per chunk for every
      //    session that never mentions a pull request — the common case.
      if (messages.length && selectedSourceUrl) reconcileSelection('change', '')
      return
    }
    // First-wins fallback ONLY when the remembered url is gone from the
    // transcript: while it is still present, selectedSourceUrl already carries
    // the restored per-slot choice and this reconciliation leaves it alone.
    if (!panelSources.some(source => source.url === selectedSourceUrl)) {
      // Storage may still hold the tab the user actually chose — absent from an
      // earlier PROVISIONAL transcript but present now that the fetch landed.
      // Look there once before falling back, gated on the url being in THIS
      // transcript (that gate IS the "the fetch proved it exists" condition).
      reconcileSelection('change', panelSources[0].url, panelSources.length)
    }
    // reconcileSourceUrl reads the active slot through a ref, so it is stable and
    // this effect reacts only to sources, selection, and hydration state.
  }, [panelSources, selectedSourceUrl, slotLoading, messages.length, reconcileSelection, restoreFromStorage])

  useEffect(() => {
    // Same first-wins / clear-on-empty reconciliation as the Changes selection
    // above, including the loaded-transcript guard on the clear.
    if (slotLoading) return
    if (restoreFromStorage('issue', panelIssues)) return
    if (panelIssues.length === 0) {
      if (messages.length && selectedIssueUrl) reconcileSelection('issue', '')
      return
    }
    if (!panelIssues.some(issue => issue.url === selectedIssueUrl)) {
      reconcileSelection('issue', panelIssues[0].url, panelIssues.length)
    }
  }, [panelIssues, selectedIssueUrl, slotLoading, messages.length, reconcileSelection, restoreFromStorage])

  const addSourceCommentToChat = useCallback((text: string) => {
    setInput(previous => previous.trim() ? `${previous.trimEnd()}\n\n${text}` : text)
  }, [])

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
  const handleFileOpen = useCallback(async (filePath: string, opts?: { replaceId?: string; line?: number; endLine?: number; diffMode?: boolean; canReplace?: () => boolean }) => {
    // Plugin host integration: notify the IntelliJ plugin (if active) so
    // it can open the file natively in the IDE editor. If the plugin
    // handles file opens, skip the dashboard's DiffPanel — the user wanted
    // IDE-native, not in-dashboard.
    try { window.dispatchEvent(new CustomEvent('kirocrew-file-open', { detail: { path: filePath } })) } catch { /* ignore */ }
    if ((window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles) return
    try {
      const [{ text }] = await Promise.all([
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
      tabsCtl.openFile(filePath, text, activeSlotRef.current ?? null, optsForReplace(opts))
      dispatch(openActivityPanel())
      // The right-hand dock is a single slot; the file viewer is render-gated
      // behind !search.isOpen. Close the find pane so the opened file actually
      // shows instead of being silently suppressed.
      search.close()
    } catch {
      tabsCtl.openFile(filePath, i18nT('pages.chatPage.error_reading_file'), activeSlotRef.current ?? null, optsForReplace(opts))
      dispatch(openActivityPanel())
      search.close()
    }
    // Depend on the stable member, not the whole hook object: `search.close` is a
    // useCallback([]) in useMessageSearch, while the `search` object changes
    // identity on every search-state change (isOpen/term/matches), which would
    // churn this callback and the onFileOpen prop on every row. (tabsCtl still
    // churns on tab changes, but those are user actions, not per-chunk.)
  }, [queryClient, tabsCtl, dispatch, search.close])

  /** Open a DIRECTORY as a panel tab.
   *
   *  The folder twin of handleFileOpen, and deliberately much thinner: there is
   *  no content to prefetch (FolderPanel owns its own ['browse-files', path]
   *  query). Only reachable for paths the backend already
   *  confirmed are directories, so there is no not-found branch to handle. */
  const handleFolderOpen = useCallback((dirPath: string) => {
    tabsCtl.openFolder(dirPath, activeSlotRef.current ?? null)
    dispatch(openActivityPanel())
    search.close()
  }, [tabsCtl, dispatch, search.close])

  // Open the Subagents panel from a completion card. A per-agent event
  // deep-links to the agent it reports on, so the panel lands on that
  // transcript rather than whatever was last selected; a wave digest names no
  // single agent and just opens the tab.
  const handleSubagentPanelOpen = useCallback((parsed: ParsedSubagentCompletion) => {
    if (parsed.kind === 'single') dispatch(selectSubagent(parsed.agentId))
    dispatch(openActivityToTab('subagents'))
  }, [dispatch])

  // Open an artifact as a side-panel tab — the artifact twin of
  // handleFileOpen, and the single entry point every in-chat artifact
  // affordance routes through (the Artifacts tab's rows and `/artifacts/<slug>`
  // links inside messages). Routing them here renders the document inline in the
  // panel instead of hard-navigating to the standalone detail page, which would
  // tear down the chat and make artifacts the only panel-capable content that
  // could not be flipped between like files.
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

  // Open the diff panel from a file-change chip click. Closes the
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
      // forkCfg is undefined until the dashboardConfig query resolves for the
      // first time. Use the cache when warm; otherwise fetch a fresh value
      // directly so direction never silently falls back to an undefined config
      // — which would downgrade an intended tail-fork to a head-fork whenever
      // the query has errored or settled with no data, not just while loading.
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
    if (big) { setUploadError(i18nT('pages.chatPage.file_too_large', { name: big.name })); return }
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
    // Classify BEFORE acting (issue #743): a dropped folder inserts its path
    // into the composer as an `@rel/` token — the same reference the @-picker
    // stages — instead of taking the upload route, which cannot ingest a
    // directory. Files keep uploading; a mixed drop takes both routes. In a
    // plain browser no real path is visible, so classifyDrop leaves folders
    // on the upload route there (today's behaviour) rather than inserting a
    // misleading bare name.
    const { files, dirPaths } = classifyDrop(e.dataTransfer)
    if (dirPaths.length) {
      // Short relative form when the folder lies inside the project root,
      // absolute otherwise — exactly the picker's own fallback convention.
      const rels = dirPaths.map(p => makeRelative(p, currentProjectRef.current || ''))
      const spliced = spliceDirTokens(inputRef.current, voiceCaretRef.current?.start ?? null, rels)
      if (spliced.changed) {
        // Arm the caret restore the same way the dictation splice does, so the
        // cursor lands just past the inserted tokens once the value commits.
        // Only on a real change: an all-duplicates drop leaves the value
        // identical, React bails out of the no-op setInput, the restore effect
        // never fires, and the armed offset would fire stale on the next
        // unrelated edit, yanking the user's cursor.
        voicePendingCaretRef.current = spliced.caret
        setInput(spliced.value)
      }
    }
    if (files.length) {
      uploadFiles(files)
    }
  }, [uploadFiles])

  // Scroll to bottom helper — delegates to the virtualizer (single controller).
  const scrollBottom = useCallback((instant: boolean = false) => {
    vScrollToBottomRef.current(instant ? 'auto' : 'smooth')
  }, [])

  // Scroll compensation for two in-flow bands that render outside the
  // virtualizer's measured rows: the tip card and the session-pulse survey
  // card. Mounting or resizing either shrinks the scroll viewport without the
  // virtualizer re-anchoring, so when the user is parked at the bottom of a
  // streaming turn the last line gets clipped, or a new turn renders behind the
  // card instead of pushing it out of view. Re-anchor whenever the tip changes
  // OR the survey reports a height change (double rAF: let the band's layout
  // commit before measuring).
  //
  // `surveyLayoutTick` is a counter, not a boolean: the card can report the
  // same "still visible" state across several distinct height changes
  // (mount/unmount, expand/collapse, the post-submit thank-you collapse), and
  // this effect only cares that SOMETHING changed, not the value.
  const [surveyLayoutTick, setSurveyLayoutTick] = useState(0)
  const handleSurveyLayoutChange = useCallback(() => setSurveyLayoutTick((t) => t + 1), [])
  useEffect(() => {
    if (!isAtBottomRef.current) return
    const raf = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (isAtBottomRef.current) scrollBottom(true)
      })
    })
    return () => cancelAnimationFrame(raf)
  }, [activeTip, surveyLayoutTick, scrollBottom])

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
    // its iframe builds (PROGRAMMATIC_BUILD_DELAY_MS). A fixed frame-count
    // ceiling (~0.5s) gives up before the widget settles, so the jump silently
    // no-ops and only works on a second click once cached. Condition-based
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
    // ABORTS the convergence, exactly as scrollCurrentMatchIntoView does. (A
    // fixed frame-count ceiling short enough (~0.5s) masks this; the
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
      // poll ends (the same restart trap removed from the streaming pin).
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
  const [pinned, setPinned] = useState<{ idx: number; ts?: string; text: string; raw: string; full: string; images: string[]; bodyBeyondPreview: boolean; push: number; bannerH: number } | null>(null)
  const [pinExpanded, setPinExpanded] = useState(false)
  // Collapsed card height — the hand-off line is derived from it, so it must be
  // known even while nothing is pinned (no card mounted to measure). Seeded with
  // the computed default and then reported by PinnedPrompt itself, which is the
  // only place the SETTLED height is knowable: measuring the card from here would
  // sample the expand/collapse morph mid-flight and drag the line with it.
  const pinCollapsedHRef = useRef(DEFAULT_PINNED_CARD_H)
  const onPinCollapsedHeight = useCallback((h: number) => {
    if (h > 0) pinCollapsedHRef.current = h
  }, [])
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
    // A prompt hands over to the banner only once it is entirely behind the band
    // (bottom edge at or above the band's bottom), so a prompt taller than the
    // band scrolls away line by line instead of collapsing the moment it is sent.
    const handoffY = pinHandoffY(foldY, pinCollapsedHRef.current)
    // First row whose bottom is still below that line = the topmost row not yet
    // fully scrolled behind the band.
    let handoffIdx = -1
    for (const item of items) {
      const htmlItem = item as HTMLElement
      if (htmlItem.getBoundingClientRect().bottom > handoffY) {
        handoffIdx = parseInt(htmlItem.getAttribute('data-display-index') || '0', 10)
        break
      }
    }

    if (!pinEnabledRef.current || handoffIdx < 0) { setPinned(null); return }
    const list = displayItemsRef.current
    const pinIdx = findPinnedPromptIdx(list, handoffIdx)
    const pinItem = pinIdx >= 0 ? list[pinIdx] : undefined
    if (!pinItem || pinItem.kind !== 'single') { setPinned(null); return }
    // The incoming prompt pushes the banner out; when its row is not mounted it
    // is still far below the fold, so there is nothing to push against yet. Its
    // TOP edge against the fold drives the push (see computePinPush) — an earlier
    // line than the hand-off, so a tall prompt shoves the card fully out while it
    // scrolls in, and only takes the pin once its own bottom clears the band.
    const nextIdx = findNextPromptIdx(list, pinIdx)
    const nextEl = nextIdx >= 0
      ? el.querySelector(`[data-display-index="${nextIdx}"]`) as HTMLElement | null
      : null
    const nextTop = nextEl ? nextEl.getBoundingClientRect().top : null
    // Measure the live card when it is mounted, and otherwise fall back to the
    // last SETTLED collapsed height PinnedPrompt reported: the push threshold
    // below has to be decidable even while nothing is mounted, or dropping the
    // banner would zero the height, zero the push, re-mount it, and oscillate at
    // frame rate.
    const measured = pinCardRef.current?.getBoundingClientRect().height ?? 0
    const bannerH = measured > 0 ? measured : pinCollapsedHRef.current
    const push = computePinPush(bannerH, foldY, nextTop)
    // Fully pushed out: DROP the banner instead of rendering it clipped to
    // nothing. A tall incoming prompt holds this state for its whole length (it
    // takes the pin only once its own bottom clears the band), and a card clipped
    // to zero still shows a hairline of its bottom edge under sub-pixel rounding
    // and browser zoom — a bubble fragment parked over the prompt being read.
    if (push >= pinPushTravel(bannerH)) { setPinned(null); return }
    const full = pinItem.msg.content
    // A nudge's content is a machine-facing instruction payload behind an
    // `[auto-nudge cycle N]` tag, and a subagent completion's is a header block
    // plus digest. Quoting either verbatim would park kilobytes of machine text
    // over the transcript, so both reuse the compact label their transcript card
    // already shows and keep the body for the expanded state.
    const nudge = pinItem.msg.role === 'nudge' ? parseNudgeMessage(pinItem.msg) : null
    // Detected by PARSING, not by role: the same completion event reaches the
    // transcript under `subagent`, `assistant` (delivery-timeout variant) and
    // `user` (older scrollback), and the parser already tolerates all three.
    // Matching on the role here would both miss those variants and duplicate
    // dispatch knowledge this file has no business holding.
    const sub = nudge ? null : parseSubagentCompletionMessage(pinItem.msg)
    const machineLabel = nudge
      ? nudgeLabel(nudge.cycle)
      : sub
        ? subagentHeadline(sub)
        : null
    const text = machineLabel ?? promptPreview(full)
    // Compare the RAW content (`prev.raw`), not `text` or the derived body:
    // `text`, `full` and `images` are all derived from it, and an edit-and-resend
    // that changes ONLY an attached image leaves the flattened preview text
    // byte-identical. Comparing the source covers every derived value with one
    // string compare — and returning `prev` unchanged matters because this runs
    // once per animation frame during a scroll, so a fresh object (or a fresh
    // `images` array) would re-render the banner on every one of them.
    setPinned(prev => (prev && prev.idx === pinIdx && prev.push === push
      && prev.raw === full && prev.bannerH === bannerH && prev.ts === pinItem.msg.ts)
      ? prev
      : { idx: pinIdx, ts: pinItem.msg.ts, text, raw: full, full: nudge ? nudge.body : (sub ? full : promptBody(full)), images: machineLabel ? [] : promptImages(full), bodyBeyondPreview: !!machineLabel, push, bannerH })
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
  /** Landing inset for a pinned-prompt jump, solved from the banner's own
   *  push geometry so the PREVIOUS turn's banner pins COMPLETELY at the
   *  landing — the chained-jump flow: click the banner, land on the prompt's
   *  start, the previous prompt's banner is already fully formed above it,
   *  click again to keep walking back. computePinPush returns 0 (no push, no
   *  clipping) iff the landed row's top clears the fold by at least
   *  pinPushTravel(bannerH). The incoming banner's height is unknowable until
   *  it pins (different prompt, different wrap), so reserve for the SETTLED
   *  collapsed height (pinCollapsedHRef, what a clamped card measures) with a
   *  slack margin absorbing wrap variance and mid-glide shifts — over-reserving
   *  only shows a little more of the turn above; under-reserving clips the
   *  banner and breaks the chain. */
  const PINNED_JUMP_SLACK_PX = 24
  const pinnedJumpChrome = useCallback(() => {
    const el = scrollerRef.current
    const foldTop = pinFoldRef.current?.getBoundingClientRect().top
    const srTop = el?.getBoundingClientRect().top
    const fold = (foldTop != null && srTop != null) ? (foldTop - srTop) : 48
    // The banner that must fit is the PREVIOUS turn's, which pins mid-glide —
    // its height is unknowable at launch (different prompt, different wrap:
    // measured 69.5-92.3px across the same session). Read the LIVE card when
    // one is pinned (after the mid-glide swap that is already the incoming
    // banner), floored by the settled collapsed height for the gap while
    // nothing is pinned. The converging glide re-reads this every frame, so
    // the reserve tracks the swap instead of freezing at the old banner.
    const live = pinCardRef.current?.getBoundingClientRect().height ?? 0
    const bannerH = Math.max(live, pinCollapsedHRef.current)
    return fold + pinPushTravel(bannerH) + PINNED_JUMP_SLACK_PX
  }, [scrollerRef])
  const scrollToPinnedPrompt = useCallback((target: number) => {
    const chrome = pinnedJumpChrome()
    cancelAnimationFrame(navScrollRafRef.current)
    navPollCancelRef.current?.()
    // The jump lands at the head of the target's consecutive prompt run — a
    // steer pair, a subagent fan-out, an unanswered nudge run — so the row on
    // the hand-off line is a non-prompt and the previous turn's banner
    // survives the landing. Rationale and near/far interaction: see
    // jumpAnchorIdx's docblock (utils/pinnedPrompt.ts).
    const anchor = jumpAnchorIdx(displayItemsRef.current, target)
    const jumpedFar = mountIndexRef.current(anchor)
    if (jumpedFar) {
      // Far target: the window was REPLACED, the path between is unmounted
      // spacer — a glide would scrub blank. Teleport via the convergence
      // path, same as every other far jump.
      navToDisplayIndex(anchor, { behavior: 'auto', align: 'start', offset: -chrome })
      return
    }
    // NEAR jump — the common case: the pinned prompt is the previous turn.
    // mountIndex UNIONED the whole path above, so every row between here and
    // the target is now mounting. Wait the few frames those rows take to
    // measure (reading, not scrolling), then compute the distance ONCE from
    // live geometry and glide in a single smooth scroll. Measuring first is
    // what makes the one glide land exactly (no estimatedHeight rows left on
    // the path); gliding once is what keeps it a real scroll — a convergence
    // poll's per-frame auto writes would cancel the animation and read as a
    // teleport. A user scroll or a newer navigation aborts the wait.
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    const rowEl = (): HTMLElement | null =>
      (scrollerRef.current?.querySelector(`[data-display-index="${anchor}"]`) as HTMLElement | null)
    let lastH: number | null = null
    let stable = 0
    let frames = 0
    let cancelled = false
    let detach2: (() => void) | null = null
    const detach = attachUserScrollIntent(scrollerRef.current ?? undefined, () => { cancelled = true })
    navPollCancelRef.current = () => { cancelled = true; detach() }
    const tick = () => {
      if (cancelled) { detach(); return }
      const el = rowEl()
      const h = el ? el.getBoundingClientRect().height : null
      if (h != null && lastH != null && Math.abs(h - lastH) < 1) stable += 1
      else stable = 0
      lastH = h
      frames += 1
      // 2 stable frames is enough: rows measure synchronously on mount via
      // measureRef; the wait only covers React committing the unioned window.
      // The frame cap (~0.5s) guarantees the glide still happens if some row
      // never stops moving (e.g. an animated widget).
      if ((h != null && stable >= 2) || frames >= 30) {
        // SELF-DRIVEN converging glide, not a native smooth scroll. A native
        // animation is cancelled by ANY other scrollTop write — and writes DO
        // land mid-glide: the upward window expansion's anchor compensation,
        // the height-sync compensation, a re-measuring row. Each cancellation
        // strands the scroll wherever the write happened (the probe showed
        // landings at 34-61px with the banner clipped or dropped — the exact
        // "some fixed spots never reach the previous message" report). Owning
        // every frame's write makes the glide uncancellable, and re-deriving
        // the destination each frame from LIVE geometry (row rect + the
        // banner currently pinned) absorbs those same mid-flight shifts —
        // mid-glide image loads and the banner swap included — so the glide
        // CONVERGES on the true landing instead of a stale one. One motion,
        // no post-landing correction. User scroll intent still aborts.
        detach()
        detach2 = attachUserScrollIntent(scrollerRef.current ?? undefined, () => { cancelled = true })
        navPollCancelRef.current = () => { cancelled = true; detach2?.() }
        const GLIDE_MS = 450
        const t0 = performance.now()
        const sc0 = scrollerRef.current
        const from = sc0 ? sc0.scrollTop : 0
        const reduced = typeof window.matchMedia === 'function'
          && window.matchMedia('(prefers-reduced-motion: reduce)').matches
        const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3)
        const glide = () => {
          if (cancelled) { detach2?.(); return }
          const sc = scrollerRef.current
          const row = rowEl()
          if (!sc || !row) { detach2?.(); navPollCancelRef.current = null; return }
          const liveTarget = sc.scrollTop
            + (row.getBoundingClientRect().top - sc.getBoundingClientRect().top)
            - pinnedJumpChrome()
          const goal = Math.max(0, Math.min(sc.scrollHeight - sc.clientHeight, liveTarget))
          const t = reduced ? 1 : Math.min(1, (performance.now() - t0) / GLIDE_MS)
          sc.scrollTop = from + (goal - from) * easeOutCubic(t)
          if (t >= 1) { detach2?.(); navPollCancelRef.current = null; return }
          navScrollRafRef.current = requestAnimationFrame(glide)
        }
        navScrollRafRef.current = requestAnimationFrame(glide)
        return
      }
      navScrollRafRef.current = requestAnimationFrame(tick)
    }
    navScrollRafRef.current = requestAnimationFrame(tick)
  }, [navToDisplayIndex, scrollToDisplayIndex, pinnedJumpChrome, scrollerRef])

  // Sticky-bottom scroll state is owned by the virtualizer (`virt.isAtBottom`,
  // wired below). No local mirror — a single source of truth avoids
  // dual-controller drift.

  // New content while following is handled inside the virtualizer (RO re-pin
  // for in-place growth + append layout-effect pin for new items), so ChatPage
  // does not run its own message-length scroll effect.
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
  // The active slot as of MOUNT. Redux outlives this component, so `activeSlot`
  // being set says nothing about whether the USER chose it during this visit —
  // only a change away from this snapshot does.
  const mountSlotRef = useRef(activeSlot)
  // A deep link (?sid=) naming a DIFFERENT session than the one Redux carried
  // over owns the first switch of this mount — see the mount re-fetch effect.
  const deepLinkPendingRef = useRef(!!initialSidRef.current && initialSidRef.current !== activeSlot)
  const initialMsgRef = useRef(searchParams.get('msg'))
  const initialNewRef = useRef(searchParams.get('new') === '1')
  // Deep-link mount activation in progress — stops the sync effect from stripping
  // ?sid before activation lands. Cleared once activeSlot is truthy.
  const pendingSidRef = useRef(!!initialSidRef.current)
  // Back/Forward (POP) in flight — set ONLY by the POP effect. Kept separate from
  // pendingSidRef so a deep-link load doesn't trip the POP bail and freeze the
  // first sidebar switch.
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
    if (!urlSlot) return
    // The deep-link ?sid only sets the INITIAL active slot. The slot list can
    // populate AFTER the user has already clicked a different session in the
    // sidebar (switchSlot.pending sets activeSlot synchronously); without this
    // guard the delayed activation would override that click and snap the UI
    // back to the deep-linked session.
    //
    // The comparison is against the slot as of MOUNT, not against "is there any
    // active slot at all". `activeSlot` lives in Redux, which outlives this
    // component: a deep link followed from another dashboard page (the System
    // page's Session & Task Memory rows, Telemetry's conversation links) mounts
    // here with the previously-visited session already active, and a bare
    // truthiness check read that as "the user already chose" and silently
    // dropped the link — you clicked a session and landed on a different one.
    // Only a switch that happened AFTER this mount is a real user choice.
    // Both abandon paths clear the in-flight flag, because arming happens BELOW
    // and this effect re-runs: an earlier run can have armed it while waiting for
    // a slot that had not arrived, and the run that abandons the link is a
    // different one. Leaving it set would kill URL sync for the rest of the mount
    // — and the not-found timeout is no backstop here, since it only acts while
    // `initialSidRef` is still set, which these branches clear.
    if (activeSlot !== mountSlotRef.current) {
      initialSidRef.current = null
      popInFlightRef.current = false
      return
    }
    if (activeSlot === urlSlot) {
      initialSidRef.current = null
      popInFlightRef.current = false
      return
    }
    // Armed BEFORE the slot is known to exist, because the wait is exactly when
    // the damage happens: a session created and linked in one go (the app pages'
    // create-then-navigate) puts `?sid=` in the URL before its slots frame
    // arrives, and during that window the URL-sync effect below sees a `sid` it
    // cannot match and PUSHes a history entry for the carried-over session — so
    // Back opens that session instead of the page the link came from. Same
    // stale-closure hazard a Back/Forward has, so it takes the same guard.
    // Released by the sync effect once activeSlot matches the URL, and by the
    // not-found timeout, so a link that never resolves cannot wedge URL sync.
    popInFlightRef.current = true
    // `some` on an empty list is false, so an unpopulated slot list waits here
    // too; this effect re-runs when `filteredSlots` arrives.
    if (filteredSlots.some(s => s.key === urlSlot)) {
      initialSidRef.current = null
      popInFlightRef.current = true
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
  // — that is what avoids the activeSlot↔URL ping-pong. A session switch pushes
  // a ?sid history entry (sync effect
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
        setSidError(i18nT('pages.chatPage.session_not_found', { name: urlSlot }))
        // Deliberately does NOT refresh the session on screen. The deep link did
        // own this mount's fetch, so that session's messages can be as stale as
        // Redux left them — but a refresh here races the user: five seconds is
        // long enough to type and send, and the in-flight response would land
        // after the optimistic row and replace both it and `running`, making the
        // turn they just sent disappear. Stale-until-next-interaction is the
        // lesser fault, and the banner above tells them the link failed.
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
      // `sid || slot` — the same pair the READ paths accept. A legacy `?slot=`
      // link resolves through this flag too, and matching on `sid` alone would
      // never release it: the flag would stay armed for the life of the mount,
      // so URL sync would be dead and a later session switch would leave the
      // URL (and therefore a reload) pointing at the wrong session.
      const urlSlot = sp.get('sid') || sp.get('slot')
      if (!activeSlot || activeSlot !== urlSlot) return
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
  //
  // Also skipped while a deep link (?sid=) names a DIFFERENT session: this
  // effect runs after the sid-activation effect above, so re-fetching the slot
  // Redux carried over from the previous page would switch straight back and
  // silently undo the link — clicking a session on the System page landed you
  // in whatever chat you had open before. The sid effect's own switchSlot
  // fetches, so nothing is lost by skipping here.
  useEffect(() => { if (!deepLinkPendingRef.current && activeSlot && !newSessionRef.current && filteredSlotsRef.current.find(s => s.key === activeSlot)) dispatch(switchSlot(activeSlot)) }, []) // eslint-disable-line react-hooks/exhaustive-deps
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
  // A pending question card suppresses them: both would offer the same choices in
  // the same band, and only the card can answer the blocked tool call.
  const { followUpOptions, followUpIsPlan } = useMemo(
    () => deriveFollowUpOptions(messages, isStreaming, !!pendingQuestion),
    [messages, isStreaming, pendingQuestion],
  )
  // Visual-only highlight state; text in the input is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the active chat switches — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  // Read by the option handler instead of the state: two clicks landing before a
  // re-render would both see the same set and both take the append branch.
  const followUpPickedRef = useRef(followUpPicked); followUpPickedRef.current = followUpPicked
  const followUpOptionsKey = followUpOptions.join('\x00')
  useEffect(() => { setFollowUpPicked(new Set()) }, [followUpOptionsKey, activeSlot])
  const { data: dashCfg } = useQuery<{ quick_send?: boolean; session_grid?: boolean; link_previews?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000 })
  // Session grid (split view) is an opt-in feature flag (Settings › Chat › Split View). Gates ⌘D, the Columns2 button, and the grid render.
  const splitFeatureEnabled = dashCfg?.session_grid === true
  // Link previews are opt-in too (Settings › Chat › Link Previews): enabling them
  // lets this machine fetch every http(s) link the model emits. Hoisted to a
  // stable primitive so it can sit in the transcript renderer's dep list — flipping
  // the toggle has to re-render already-rendered messages, not just the next one.
  const linkPreviewsOn = dashCfg?.link_previews === true
  // Connections cards own consent for the providers they render, so chat drops
  // the duplicate OAuth banner — but only while that gallery is reachable.
  const connectionsUiOn = useConnectionsUiEnabled()
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

  const send = useCallback(async (optionText?: string, targetSlot?: string, steerNow?: boolean) => {
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
    if (!raw && !pendingFilesRef.current.length && !pendingSessionsRef.current.length) return

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
    if (voiceRef.current.recording && streamEnabledRef.current) {
      sttDisarmedRef.current = true
      frozenInputRef.current = null
      lastDictationAnchorRef.current = null
      lastDictationValueRef.current = null
      postStopEditedRef.current = false
      voiceRef.current.toggle()
    }

    // The session actually on screen at send time. Read from the ref (fresh
    // every render), not the closure `activeSlot` (stale until send() is
    // re-memoized). Under lag a reducer-driven activeSlot change can move the
    // active slot before ChatPage re-renders, so the closure would route into
    // the slot the user just left. Used for slash routing, the composer draft
    // clear, and (below) the send target.
    const uiSlot = activeSlotRef.current

    // Capture the stateless card pending at ENTRY — before the first await
    // below. This send consumes the answer channel of the card the user saw
    // when they hit send; captured after an await, the card-submit flow can
    // clear the card (or a newer one can land) in the gap, and the capture
    // would compare against the wrong baseline (fork GPT review, 995718f).
    const entrySendSlot = targetSlot ?? uiSlot
    const cardAtSend = captureStatelessCard(store.getState().chat.pendingQuestions, entrySendSlot)
    // Same entry-time capture for a BLOCKING card, whose staleness is resolved
    // over the network instead of in the store.
    const askAtSend = capturePendingAskId(store.getState().chat.pendingQuestions, entrySendSlot)

    // Slash command interception (e.g. /side): runs before knowledge so a
    // bare prefix like /side returns immediately without touching input parse.
    // Gate on the RAW composer text first — a pasted block whose content
    // happens to start with "/side " must stay main-chat content, never
    // become a command. Only a command the user actually typed is expanded
    // (so a paste after "/side " reaches the side chat as content) and
    // delegated. On failure keep the composer intact so the question stays
    // recoverable — same rules as steer()'s guard.
    if (isInterceptedSlashCommand(raw)) {
      const slashPastes = pasteBlocksRef.current
      const slashTxt = slashPastes.length ? expandPasteTokens(raw, slashPastes) : raw
      const slashResult = await interceptSlashCommand(slashTxt, uiSlot, dispatch)
      if (slashResult.intercepted) {
        if (!optionText && !slashResult.failed) { setInput(''); setPasteBlocks([]) }
        return
      }
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
    // Staged refs belong to the COMPOSER, so only a send that consumes the
    // composer may carry them. An `optionText` send (a follow-up option click)
    // supplies its own text and deliberately leaves the composer untouched —
    // the clear below is skipped for exactly that reason. Consuming refs there
    // anyway would attach them to an unrelated message AND leave them staged, so
    // the same links would go out again on the user's next real send.
    //
    // Gated on the same condition as the clear, so the two can never disagree in
    // either direction: no send-without-clear (duplicate) and no clear-without-
    // send (silent loss). Scoped to refs on purpose — `pendingFiles` has carried
    // this shape since long before this feature, and changing it here would widen
    // the PR into pre-existing attachment behaviour.
    const sentSessionRefs = optionText ? [] : pendingSessionsRef.current.slice()
    const { txt: typedTxt, displayTxt: typedDisplayTxt, filePaths } = prepareSendPayload(raw, pendingFilesRef.current)
    // Folder references serialize like files but from the text alone: each
    // `@rel/` token becomes `[attached_dir N] /abs/path` in the LLM-facing
    // text (absolute, so the reference survives a cwd/project mismatch and
    // history replay), while the display text keeps the `@rel/` token for the
    // bubble chip — the same fresh-vs-wire split files use. Runs AFTER the
    // file pass: file tokens never end in `/`, so the two rewrites are
    // disjoint. `dirPaths` rides `meta.dirs`, ordered so marker N indexes
    // dirPaths[N-1] losslessly.
    const { llm: typedTxtDirs, dirPaths } = serializeDirTokens(typedTxt, currentProjectRef.current || '')
    // Staged session references become plain markdown links appended to the
    // message — deliberately a POINTER, not the referenced transcript. Inlining
    // another session's content would spend a large share of THIS session's
    // context window in one turn and can trip autocompact, compacting away the
    // conversation the reference was meant to enrich. The agent follows the link
    // on demand instead, through a read path that is already bounded, redacted,
    // and incognito-refusing server-side.
    //
    // The link is built by the SAME helper the session menu's "Copy link" uses,
    // so a referenced session and a hand-copied one are the same string.
    //
    // Appended to the sent and displayed text alike: unlike a paste token there
    // is no collapsed form to preserve in the bubble, so what the user sees is
    // exactly what was sent. Appending (never splicing) also means paste-token
    // ranges found earlier in the string are untouched.
    const txt = appendSessionRefLinks(typedTxtDirs, sentSessionRefs)
    const displayTxt = appendSessionRefLinks(typedDisplayTxt, sentSessionRefs)
    // Expand paste tokens for the LLM; UI-facing displayTxt keeps the tokens
    // intact so the user bubble can render them as clickable chips.
    const activePastes = pasteBlocksRef.current
    let llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
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
      setInput(''); setPendingFiles([]); pickedFileTokens.current = {}; setPasteBlocks([]); setPendingSessions([]); if (uiSlot) { delete drafts.current[uiSlot]; delete fileDrafts.current[uiSlot]; delete pasteDrafts.current[uiSlot]; delete sessionRefDrafts.current[uiSlot]; saveDrafts() }
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
        // Session refs merge by key (they carry no sequence to collide on, unlike
        // pastes), keeping whatever the user staged since the failed send.
        const keepRefs = onScreen ? pendingSessionsRef.current : (uiSlot ? sessionRefDrafts.current[uiSlot] ?? [] : [])
        const restoredRefs = mergeSessionRefs(keepRefs, sentSessionRefs)
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
          setInput(restoredText); setPasteBlocks(restoredPastes); setPendingFiles(restoredFiles); setPendingSessions(restoredRefs)
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
          dispatch(appendMessage({ role: 'error', content: i18nT('pages.chatPage.could_not_start_session_message_restored', { error: createFailReason(e) }), cls: '' }))
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
              body: i18nT('pages.chatPage.message_queued_until_session_ready', { error: createFailReason(e) }),
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
            body: i18nT('pages.chatPage.message_saved_as_draft', { error: createFailReason(e), extra: lostContext }),
            slot: uiSlot,
          }))
        }
        if (uiSlot) {
          setDraft(drafts.current, uiSlot, restoredText)
          setPasteDraft(pasteDrafts.current, uiSlot, restoredPastes)
          setFileDraft(fileDrafts.current, uiSlot, restoredFiles)
          setSessionRefDraft(sessionRefDrafts.current, uiSlot, restoredRefs)
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
    if (dirPaths.length) meta.dirs = dirPaths
    if (bubblePastes.length) meta.pastes = bubblePastes
    if (knowledgeBlock) meta.knowledge = { items: knowledgeBlock.items.length, tokens: knowledgeBlock.totalTokens, titles: knowledgeBlock.items.map(i => i.title), content: knowledgeBlock.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }
    if (widgetOrigin) meta.origin = 'widget'
    // A client-generated correlation ID so the server echo can be matched
    // to this exact optimistic bubble without relying on content equality.
    // The server preserves meta fields on the user row it appends, so the
    // echo carries both this sendId AND the server-minted `mid` (#2845).
    const sendId = `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    meta.sendId = sendId
    const metaPayload = meta
    // Skip optimistic user bubble when the slot is busy (shared rule:
    // chatSlice.selectComposerBusy) — the backend sends a "queued" role
    // message instead, avoiding a duplicate. A steer-flagged send usually
    // bypasses the queue and starts a turn, so nothing would represent it; its
    // bubble is appended from the response instead (see below), because only
    // the server knows whether this particular send got queued after all.
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
    /**
     * Put the composer back the way it was before this send.
     *
     * Called from BOTH failure shapes: a transport error (fetch rejected) and a
     * REJECTED RESPONSE (`!body.queued && !body.ok` — e.g. an expired cookie
     * answering 403). Both mean the message did not go out, so both must recover
     * identically; previously only the transport branch restored, so a dropped
     * connection kept the user's message while a 403 discarded it.
     *
     * Persist for `slot` unconditionally (recoverable on disk), but only touch
     * the live input/blocks when `slot` is the one on screen. Compare against
     * activeSlotRef.current, NOT the closure's `activeSlot`: a new-session /
     * forceNew send creates a fresh slot and switches the UI to it, so the
     * closure value is stale — using it would leave the user's just-typed message
     * empty on the very session they are now viewing. The ref reflects what is
     * actually on screen, so it restores visibly for a new-session failure while
     * still not splicing a targeted send's text into an unrelated slot.
     *
     * Restores `typedTxt` — what the user actually TYPED — and brings the staged
     * references back as chips, rather than restoring the link-appended `txt`.
     * Restoring `txt` preserved the reference (the link is in the text) but left
     * it as a raw URL, and re-staging the chips ON TOP of that text would make
     * the retry append each link a SECOND time. Splitting them puts the composer
     * back in exactly its pre-send state: chip visible, link appended once on
     * retry. Paste blocks come back too, or the restored text would show a dead
     * `[ Paste #N · M lines ]` literal. Shares the create-failure path's merge
     * rule so a reference staged while the send was in flight is not clobbered.
     */
    const restoreComposerAfterFailedSend = () => {
      if (!slot) return
      const onScreenNow = slot === activeSlotRef.current
      const liveRefs = onScreenNow ? pendingSessionsRef.current : (sessionRefDrafts.current[slot] ?? [])
      const refsBack = mergeSessionRefs(liveRefs, sentSessionRefs)
      // MERGE, never overwrite. The send is in flight for up to 10s, and the user
      // can type a fresh message in that window — clobbering it with the failed
      // payload would lose newer work to recover older. Mirrors the create-failure
      // path above: keep what is there, append the failed payload unless it is
      // already the same text, and re-sequence the carried paste blocks so two
      // blocks cannot claim one `[ Paste #N ]` marker.
      const keepText = onScreenNow ? inputRef.current : (drafts.current[slot] ?? '')
      const keepPastes = onScreenNow ? pasteBlocksRef.current : (pasteDrafts.current[slot] ?? [])
      const keptIds = new Set(keepPastes.map(b => b.id))
      const { text: carriedText, blocks: carriedPastes } = remapCarriedBlocks(
        typedTxt,
        activePastes.filter(b => !keptIds.has(b.id)),
        new Set(keepPastes.map(b => b.seq)),
      )
      const pastesBack = [...keepPastes, ...carriedPastes]
      // Joined rather than interpolated: the blank line between the kept draft and
      // the recovered payload is message structure, not copy, and keeping it out of
      // a template literal keeps this line off the i18n gate honestly rather than
      // by exemption (same treatment as appendSessionRefLinks).
      const textBack = !keepText.trim()
        ? carriedText
        : keepText.trim() === carriedText.trim()
          ? keepText
          : [keepText.replace(/\s+$/, ''), carriedText].join('\n\n')
      setDraft(drafts.current, slot, textBack)
      setPasteDraft(pasteDrafts.current, slot, pastesBack)
      setSessionRefDraft(sessionRefDrafts.current, slot, refsBack)
      saveDrafts()
      if (onScreenNow) {
        setInput(textBack); setPasteBlocks(pastesBack); setPendingSessions(refsBack)
      }
    }
    try {
      const r = await api.sendChat(llmTxt, slot ?? undefined, colorThemeRef.current, controller.signal, metaPayload, steerNow)
      clearTimeout(timeout)
      const body = await r.json().catch(() => ({}))
      if (!body.queued && !body.ok) {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage({ role: 'error', content: body.error || i18nT('pages.chatPage.send_failed'), cls: '' }))
        // The server explicitly accepted neither (`ok` nor `queued`), so nothing
        // was sent — recovering the composer cannot duplicate a delivered turn.
        restoreComposerAfterFailedSend()
      } else if (steerNow && _busy && !body.queued && !body.steered) {
        // A steer-flagged send the server neither queued nor injected: it
        // started a turn, so no `queue_push` or `steer_push` echo is coming and
        // the busy rule above left the text with nothing to represent it.
        // Append only once the answer rules out both echoes — a mid-plan send
        // is queued, and a child turn that started while this POST was in
        // flight is injected mid-turn, each of which brings its own bubble.
        // Addressed to the SENDING slot, not the active one: the user can
        // switch sessions while the POST is in flight, and this text belongs to
        // the transcript it was typed into (same reason `steer_push` uses this).
        if (slot) {
          dispatch(appendSlotMessage({
            slot,
            message: { role: 'user', content: displayTxt, cls: '', ts: new Date().toISOString(), meta: metaPayload },
          }))
        }
      }
      if (slot && confirmedDelivered(body)) {
        // The response IS the delivery receipt (#4131). The server accepted the
        // message and appended (or queued) the row, so the optimistic bubble is
        // confirmed and must stop being a candidate for the 30s "may not have
        // been delivered" sweep. Nothing else can retire it on this surface: the
        // `chat_message` user echo `reconcileOptimisticEcho` waits for is
        // suppressed for every dashboard send by design (`DashboardState.append`
        // defaults `broadcast_user=False` precisely because the composer already
        // rendered this bubble), so before this the flag survived the whole turn
        // and only vanished when `chat_done`'s refresh rebuilt the transcript
        // from disk.
        //
        // Addressed to the SENDING slot for the same reason as the steer-echo
        // append above. Harmless when the busy rule appended no bubble — no row
        // carries this `sendId`, so it is a no-op. Deliberately NOT dispatched on
        // a rejected response, a queued acceptance, or the abort-timeout path:
        // there delivery of THIS row is unknown, which is what the indicator
        // exists to say (see `confirmedDelivered`).
        dispatch(confirmOptimisticSend({ slot, sendId }))
      }
      if (body.ok && !body.queued && cardAtSend && slot === entrySendSlot) {
        // Immediate dispatch confirmed (`ok`): the message consumed the slot's
        // next-turn channel, so the card captured at entry is now stale. An
        // independent check, not part of the else-if chain above — the
        // steer-echo branch also implies `ok && !queued`, and the card must
        // retire regardless of which transcript-echo rule applied. A QUEUED
        // acceptance deliberately does NOT retire here — the queued message is
        // still cancellable, and cancelling must keep the card; it retires at
        // its queue_pop instead (removeQueuedMessage). The slot-identity guard
        // covers forceNew rerouting the send into a freshly created session —
        // that send answers nothing in the entry slot, whose card must stay.
        // Deliberately NOT done on the optimistic append (a failed send must
        // keep the card) nor on the abort-timeout path below (delivery
        // unconfirmed — a wrongly kept card is dismissible, a wrongly deleted
        // one is not recoverable).
        dispatch(retireStatelessQuestion({ slot, expected: cardAtSend }))
      }
      // The user answered in the composer instead of the card; a blocking card
      // is resolved over the network, so this cannot be a store-only retirement.
      void resolveAskAfterSend(body, slot === entrySendSlot ? askAtSend : null, dispatch)
    } catch (e: unknown) {
      clearTimeout(timeout)
      if (e instanceof DOMException && e.name === 'AbortError') {
        // Timeout — message was received, WS will deliver response
      } else {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage({ role: 'error', content: i18nT('pages.chatPage.connection_error'), cls: '' }))
        restoreComposerAfterFailedSend()
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
  // Keep sendRef current so the streaming endpointer's auto-submit callback
  // (wired into the voice hook above, before send is declared) always invokes
  // the latest send(). Assigned in render like inputRef.current = input above.
  sendRef.current = send
  const submitComments = useCallback((message: string) => {
    const target = tabsCtl.activeTab?.slot ?? null
    if (target && target !== activeSlot) dispatch(switchSlot(target))
    send(message, target ?? undefined)
  }, [tabsCtl.activeTab, activeSlot, dispatch, send])

  // Auto-send when navigated with ?autoSend=1 or ?token= with prompt
  useEffect(() => { if (connected && autoSendRef.current) { const txt = autoSendRef.current; autoSendRef.current = null; send(txt) } }, [send, connected, autoSendTick])  

  // Widget interactivity: when a mcwidget iframe fires an action, PRE-FILL the
 // composer instead of auto-submitting. Auto-submitting would be a
  // trust-boundary bypass: LLM-emitted <script> inside the sandboxed widget
  // iframe can call parent.postMessage directly, bypassing the in-iframe
  // isTrusted click guard, and the parent cannot distinguish that from a
  // genuine click. So a widget action must never become a user-role turn
  // without an explicit human gesture — the user reviews the pre-filled text
  // and presses Enter. We also record the pre-filled text so the resulting
  // send is tagged meta.origin='widget' for forensics.
  useEffect(() => {
    const handler = (e: Event) => {
      const text = (e as CustomEvent).detail?.text
      if (typeof text !== 'string' || !text) return
      widgetPrefillRef.current = text
      setInput(prev => (prev.trim() ? `${prev.trimEnd()}\n${text}` : text))
      setPrefillHint(true)
      revealComposer()
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
      // Clear any explicit pick made for the PREVIOUS agent rather than
      // re-seeding a resolved model: an empty pendingModel makes createSlot omit
      // `model`, which lets the backend resolve the new agent's own chain at
      // create time. Seeding the resolved id here pinned it instead (#2035).
      setPendingModel('')
      return
    }
    dispatch(setAgentSwitchNotice(null))
    try {
      await api.chatSlotAgent(activeSlot, agentName)
    } catch (error) {
      // Closing the picker is the call sites' job and already happens
      // synchronously alongside this call, so a failure surfaces as the shared
      // notice rather than by holding the dropdown open.
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
    }
    // queryClient and the setPending* setters are all stable (react-query
    // client / useState setters / useCallback([])), so listing them satisfies
    // the linter without re-creating this callback.
  }, [activeSlot, dispatch, installedAgents, provider, queryClient, setPendingAgent, setPendingModel])
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
  // One source for both same-meaning markers in the agent pop-up: the row's check and
  // the default-agent row's label. Reading the slot twice let them disagree.
  const activeAgentName = currentSlot?.agent || 'default'
  // Refs so the "run in terminal" listener (registered once) always sees the
  // live panel controller + this chat's working directory.
  const tabsCtlRef = useRef(tabsCtl); tabsCtlRef.current = tabsCtl

  /** Bring an app's panel tab back — focusing it if open, re-creating it if the
   *  user closed it (`openApp` upserts).
   *
   *  The auto-open effect above deliberately does not re-open a tab the user
   *  closed, which is why the bubble placeholder has to be a real control rather
   *  than static text. Note the effect's once-per-tool-call guard holds only
   *  PER CHATPAGE MOUNT: `openedAppTabsRef` is not persisted, so navigating away
   *  and back re-arms it. Closing the find pane is part of the action: `isSidePanelHidden`
   *  keeps the panel hidden while search owns the dock, so without this the click
   *  would open a tab the user cannot see and look broken. */
  const revealAppInPanel = useCallback((toolCallId: string) => {
    if (search.isOpen) search.close()
    dispatch(openActivityPanel())
    tabsCtlRef.current?.openApp(toolCallId, i18nT('pages.chatPage.mcp_app_tab_title'), activeSlot ?? null)
  }, [dispatch, activeSlot, search])
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
    // whatever the user was mid-way through typing. `inputRef` is the live
    // composer value; `mergeIntoDraft` is shared with the error → agent hand-off
    // drain so the two paths cannot drift.
    dispatch(setPendingInput(mergeIntoDraft(inputRef.current, item.prompt)))
    // Clear by the RENDERED card's ts, as the worktree action does: a newer card
    // for this slot can land between render and click, and an unqualified clear
    // would delete suggestions the user never saw.
    dispatch(clearFollowupCard({ slot: activeSlot, ts: followupTsRef.current[activeSlot]?.ts }))
  }, [dispatch, activeSlot])

  // Folder suggestion: accepting reuses the ONE move path every other surface
  // (row menu, drag-to-folder, new-chat-in-folder) already funnels through, so
  // the optimistic update and its guarded rollback are inherited rather than
  // re-implemented here. Both answers clear the card by the ts it rendered with,
  // for the same reason the follow-up actions do.
  const folderSuggestionAccept = useCallback(() => {
    if (!activeSlot || !folderSuggestion) return
    moveSlotToFolder(activeSlot, folderSuggestion.folderId)
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, moveSlotToFolder, dispatch])

  const folderSuggestionDecline = useCallback(() => {
    if (!activeSlot || !folderSuggestion) return
    // Nothing to tell the backend: it already spent its one offer for this slot,
    // so declining is purely "take the card away".
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, dispatch])

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
    if (!repo) throw new Error(i18nT('pages.chatPage.this_session_has_no_project_directory_to_branch'))
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
    if (!path) throw new Error(res?.error || i18nT('pages.chatPage.worktree_creation_returned_no_path'))
    let slotKey = ''
    try {
      // `activate: false` on purpose: the slot must be SCOPED to the worktree
      // before the user can type into it. Activating first (the default) leaves a
      // window where the composer is live but `chatSlotProject` is still pending,
      // so a turn sent in that window would run in the default directory — agent
      // tools writing to the wrong checkout. It also means a scoping failure can
      // render its error on the still-mounted card instead of unmounting it.
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
  // Auto-open the Browser panel when the agent starts browsing. The signal is the
  // agent's own shell call: browsing is `playwright-cli` commands, so a shell
  // tool_call whose preview invokes it is the start of a browse. Open/focus the tab
  // only at the START (new slot, or after a >90s gap), NOT on every command, so it
  // cannot steal focus from a tab the user switched to mid-browse.
  const browseOpenedRef = useRef<{ key: string | null; ts: number }>({ key: null, ts: 0 })
  useEffect(() => {
    const onTool = (e: Event) => {
      const d = (e as CustomEvent<{ slot?: string; is_shell?: boolean; input_preview?: string }>).detail
      if (!d?.is_shell) return
      if (!isBrowseCommand(d.input_preview)) return
      const key = d.slot ?? null
      // Only auto-open when the browsing session IS the one on screen. A background
      // session's commands must not open another session's panel.
      if (!key || key !== activeSlotRef.current) return
      const now = Date.now()
      const prev = browseOpenedRef.current
      if (prev.key !== key || now - prev.ts > 90_000) {
        dispatch(openActivityPanel())
        tabsCtlRef.current.openView('browser')
      }
      browseOpenedRef.current = { key, ts: now }
    }
    window.addEventListener('kirocrew-tool-call', onTool)
    return () => window.removeEventListener('kirocrew-tool-call', onTool)
  }, [dispatch])
  // Reachability: declare open chat slots to the Electron main process so the
  // agent command channel polls for them (see listPanelIds) even before the Browser
  // tab is ever opened — this is what makes the built-in browser the default for a
  // fresh chat. It is NOT a grant: authorization to drive the built-in browser is
  // Browser Mode (the Settings toggle), and the main-process gate is just the view
  // precondition. There is no separate per-session consent registration — the
  // command channel can only deliver an op for a session key it polls for, and it
  // must poll before any URL is known, so gating reachability on a per-session
  // grant would make the whole native path unreachable for a fresh chat.
  //
  // EVERY open chat is declared, not just the active one.
  //
  // The command channel can only deliver an op for a session key it polls for,
  // and it must poll BEFORE any URL is known. Declaring only `activeSlot` made
  // that a moving target, and both consequences were observed live in a diagnostic
  // run:
  //   * a chat created and messaged within seconds RACED the registration — the
  //     navigate reached the gateway first, which answered `no-native-panel` (503)
  //     because no poller held that key yet, so the proxy fell back to the
  //     Playwright mirror for the whole turn (observed: slot created at T+0, the
  //     navigate at T+15s, the key first reported 9 minutes later);
  //   * a BACKGROUND chat was never reachable at all, even when it was the session
  //     the agent was acting for.
  //
  // Declaring a key is NOT authorization — it grants nothing, and every op still
  // runs the same gate — so there is no reason to report one key instead of all of
  // them. Tracking is diffed rather than torn down per change: re-registering the
  // same keys on every slot-list edit would churn IPC for no reason, and dropping
  // them mid-turn is exactly the race above.
  const trackedSlotsRef = useRef<Set<string>>(new Set())
  const trackableSlotKeys = useMemo(
    () => slots.map(s => s.key).filter((k): k is string => !!k),
    [slots],
  )
  useEffect(() => {
    const api = (window as unknown as {
      browserAPI?: { trackSession?: (id: string, tracked: boolean) => Promise<unknown> }
    }).browserAPI
    if (!api?.trackSession) return      // plain browser (no bridge)
    const want = new Set(trackableSlotKeys)
    const tracked = trackedSlotsRef.current
    for (const key of want) {
      if (tracked.has(key)) continue
      tracked.add(key)
      void api.trackSession(key, true)
    }
    for (const key of [...tracked]) {
      if (want.has(key)) continue
      tracked.delete(key)
      void api.trackSession(key, false)
    }
  }, [trackableSlotKeys])
  // Native counterpart of the mirror auto-open above. When the agent opens a page
  // in the BUILT-IN browser, the WebContentsView is created in the Electron main
  // process but the dashboard owns layout — until the Browser panel mounts and
  // reports its rect, the page is composited nowhere and the user sees nothing.
  // So surface the panel on the main process's `browser:agent-opened` signal.
  //
  // Same active-slot guard as the mirror path: a background session's page must
  // not open another session's panel.
  useEffect(() => {
    const api = (window as unknown as {
      browserAPI?: { onAgentOpened?: (cb: (p: { panelId?: string }) => void) => () => void }
    }).browserAPI
    if (!api?.onAgentOpened) return      // plain browser (no preload bridge)
    return api.onAgentOpened(({ panelId }) => {
      if (!panelId || panelId !== activeSlotRef.current) return
      dispatch(openActivityPanel())
      tabsCtlRef.current.openView('browser')
    })
  }, [dispatch])
  // "Run in terminal" (from chat code blocks): open a terminal tab in the
  // app-wide dock panel and run the command in it, starting in the chat's
  // working dir. The dock panel persists across routes (unlike chat-scoped
  // terminal tabs) so the running shell survives navigation.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      const code: string = detail.code
      const reqId: string = detail.reqId
      if (typeof code !== 'string' || !code) return
      const sessionId = addDockTerminal(currentProjectRef.current ?? undefined)
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
  }, [])
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
  const _slotAgentName = (currentSlot && !currentSlot.model) ? (currentSlot.agent || 'default') : ''
  const { data: _slotResolvedModel } = useQuery({
    queryKey: ['resolved-model', _slotAgentName, provider.id],
    queryFn: () => provider.resolveModel(_slotAgentName),
    enabled: !!_slotAgentName,
  })
  // The agent the composer's "set as default" row acts on: the active slot's
  // agent, else whichever agent a new session would open on.
  const _modelPinAgent = currentSlot?.agent || pendingAgent || defaultAgent || 'default'
  const _modelPinCfg = installedAgents.find(a => a.name === _modelPinAgent)
  // Writes agents.<name>.model in config.json. Invalidates the resolved-model
  // queries so a slot showing an inherited value picks the new pin up without a
  // reload; open sessions keep the model they already resolved.
  const pinModelToAgentMut = useMutation({
    mutationFn: ({ agent, model }: { agent: string; model: string }) =>
      api.updateKirocrewAgent(agent, { model }),
    onSuccess: () => {
      dispatch(triggerRefresh())
      queryClient.invalidateQueries({ queryKey: ['resolved-model'] })
    },
    // The dropdown closes as soon as the row is clicked, so without this a
    // failed write left NOTHING on screen and the old default silently stood —
    // discoverable only by reopening the menu. Body is the agent name plus the
    // server's own message, so it carries no untranslated prose of its own.
    onError: (e: Error, vars) => {
      dispatch(addNotification({
        ts: uniqueNotificationTs(),
        kind: 'agent',
        priority: 'critical',
        title: i18nT('pages.chatPage.could_not_set_the_agent_default_model'),
        body: `${vars.agent}: ${e?.message || i18nT('components.errorBoundary.something_went_wrong')}`,
      }))
    },
  })
  // Derived, not mirrored into state via an effect: the effect form cost an extra
  // render pass every time the query settled, for a value that is a pure function
  // of the query result.
  const resolvedModel = _slotResolvedModel || ''
  // The model to DISPLAY for this slot. A slot can stay pinned to a model the
  // account can no longer run (a plan downgrade leaves the pin behind): the
  // backend withholds it at spawn and runs the session on its own default, so
  // showing the pin would name a model no turn will use. The degraded flag is
  // the authority on whether the list can be trusted — a cached list served
  // while /api/models fails is stale, not authoritative — and is subscribed to
  // rather than read, because it can flip without the list changing.
  const _modelsDegraded = useModelsDegraded(provider.id)
  const shownModel = displayModel(
    currentSlot?.model || resolvedModel || '',
    availableModels,
    _modelsDegraded,
  )
  // True when the pin row would be a no-op: the agent already stores exactly
  // the model the composer is showing. 'auto' is the inherit spelling, never a
  // stored pin, so it never counts as pinned. Reads the slot's REAL model, not
  // `shownModel` — this pairs with the write below, and a display fallback must
  // never decide what gets persisted.
  const _modelPinActive = currentSlot?.model || resolvedModel || ''
  const _modelPinPinned =
    !!_modelPinCfg?.model && _modelPinCfg.model === _modelPinActive && _modelPinActive !== 'auto'
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

  // Auto-open the Git panel when the slot has a project dir that is a git repo.
  // OPT-IN (dashboard.auto_open_git_panel, default off) because the marker below
  // cannot make this the once-per-project nudge it reads like: a new slot inherits
  // `dashboard.default_project`, so keying on slot+path re-fires for every new
  // chat in the same repo — forever. The Git TAB is still created unconditionally
  // (same as the folder tab below), so the panel is one click away when off.
  useEffect(() => {
    if (!activeSlot || !_slotProject || projectGitError) return
    if (!projectGit?.repo) return
    // Do not consume the marker before the opt-in's value is known — see
    // `autoOpenGitPanelKnown`.
    if (!autoOpenGitPanelKnown) return
    const key = `mc-git-panel-opened:${activeSlot}:${_slotProject}`
    if (localStorage.getItem(key)) return
    // If the marker cannot be persisted (quota), skip the auto-open entirely:
    // opening changes tabsCtl, which re-runs this effect, and an absent marker
    // would make it open again forever.
    try { localStorage.setItem(key, '1') } catch { return }
    tabsCtl.openView('git')
    if (autoOpenGitPanel) dispatch(openActivityPanel())
  }, [activeSlot, _slotProject, projectGit?.repo, projectGitError, tabsCtl, dispatch, autoOpenGitPanel, autoOpenGitPanelKnown])

  const [sidebarPinned, setSidebarPinned] = useState(() => localStorage.getItem('mc-sidebar-pinned') !== 'false')
  const sidebarPinnedRef = useRef(sidebarPinned)
  sidebarPinnedRef.current = sidebarPinned
  // Pre-focus session-list state while the Web Preview expand mode auto-hides
  // it, so exiting focus mode restores what the user had. null = focus mode is
  // not the reason the list is hidden (the user owns the state).
  const sidebarAutoHidden = useRef<boolean | null>(null)
  const isMobile = useIsMobile()
  const [sidePanelDock] = useSidePanelDock()
  // Recomputed on every dock flip: the wrapper keeps one React key across the
  // flip, so both axes have to stay named or the flipped-away one gets driven
  // back to its base (see sidePanelDockMotion).
  const sidePanelDockAnim = useMemo(() => sidePanelDockMotion(sidePanelDock), [sidePanelDock])
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const v = parseInt(localStorage.getItem('mc-sidebar-width') || '', 10)
    return !isNaN(v) && v >= SIDEBAR_MIN && v <= SIDEBAR_MAX ? v : 260
  })
  const [sidebarDragging, setSidebarDragging] = useState(false)
  // Pinned to the slot the rename opened on: activeSlot moves the instant the user
  // switches sessions, and a live-resolved commit would rename the wrong session.
  const [editingTitleSlot, setEditingTitleSlot] = useState<string | null>(null)
  const editingTitle = editingTitleSlot !== null && editingTitleSlot === activeSlot
  // Leaving abandons the draft. The pin alone closes the editor but keeps it, so a
  // return would revive stale text and a blur could overwrite a newer title.
  useEffect(() => { setEditingTitleSlot(null) }, [activeSlot])
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
  // layout (so a fresh refresh never seeds an orphan pane).
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
  // ---- Refused-press notice ---------------------------------------------------
  // One surface for any press the server refuses. These endpoints re-check under
  // the slot lock and can refuse a press the client believed was available (a
  // turn already running, a stop in progress, a pending approval, a readiness
  // probe that timed out). Left in the console, that refusal reaches the user as
  // the button flicking to disabled and straight back — a control that promises
  // action and then says nothing. The server names the reason; this shows it
  // above the composer with a per-action title. One state slot serves every
  // refusable press (the newest refusal wins), so a press added later inherits
  // the surface by calling `showRefusedPress` instead of re-discovering
  // console.warn. The title map is `as const` so the key gate resolves every
  // member from the single render-site call.
  const [refusedPress, setRefusedPress] = useState<{ action: RefusedPressAction; message: string } | null>(null)
  const showRefusedPress = useCallback((action: RefusedPressAction, e: unknown) => {
    setRefusedPress({ action, message: e instanceof Error && e.message ? e.message : String(e) })
  }, [])
  useEffect(() => { setRefusedPress(null) }, [activeSlot])
  // A turn that actually starts retires the refusal: whatever the slot was busy
  // with is over, so the old reason would now describe a state that passed.
  useEffect(() => { if (slotRunning) setRefusedPress(null) }, [slotRunning])
  const handleRegenerate = useCallback(() => {
    if (!activeSlot || regenerating || slotRunning) return
    const uIdx = messages.slice(0, lastTextIdx).map(mm => mm.role).lastIndexOf('user')
    if (uIdx < 0) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(uIdx + 1))
    setRegenerating(true)
    api.regenerateSlot(activeSlot).catch((e: unknown) => {
      showRefusedPress('regenerate', e)
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, regenerating, slotRunning, messages, lastTextIdx, dispatch, showRefusedPress])

  // ---- Continue the thread ---------------------------------------------------
  // A turn can end without the assistant handing the floor back: the connection
  // dropped, the gateway restarted during an app update, the app was force-quit,
  // or the runner's own recovery ladder gave up. Some of those leave evidence (an
  // unanswered user row, a trailing error card) and some leave none at all — a
  // force-quit runs no cleanup, so its transcript is indistinguishable from a
  // clean finish. Continue is therefore offered on any idle slot with a
  // conversation, and `interrupted` only decides how the button describes itself.
  //
  // The two COMPOSE at the ErrorCard; neither alone is right. `continuable` is the
  // availability half (running, stopping, pending turn, autopilot, subagents,
  // queue) and `interrupted` is the placement half — `i === lastErrorIdx` means
  // "newest error row", never "the transcript ends badly", so on
  // `[user, error, user, assistant]` availability alone would put a Continue
  // button on a superseded failure card that acts on a LATER request. Dropping
  // `continuable` instead is the mirror-image bug: `selectTurnInterrupted` carries
  // none of the busy checks, so a card would offer a Continue that `handleContinue`
  // early-returns on — a dead control in the one place recovery is promised.
  const continuable = useAppSelector(selectContinuable)
  const interrupted = useAppSelector(selectTurnInterrupted)
  const [continuing, setContinuing] = useState(false)
  // Why the refusal is rendered rather than logged: the server re-checks under
  // the slot lock and can refuse a press the client believed was available
  // (`slot_running`, `slot_subagents_running`, an approval still pending). Left
  // in the console, that refusal reached the user as the button flicking to
  // disabled and straight back — a control that promises recovery and then says
  // nothing at all. `showRefusedPress` is the shared surface for exactly that.
  useEffect(() => { setContinuing(false) }, [activeSlot])
  // The turn taking over is the success signal; clear the spinner then.
  useEffect(() => { if (continuing && slotRunning) setContinuing(false) }, [continuing, slotRunning])
  // Backstop: a request that neither starts a turn nor rejects must not strand
  // the button in a disabled state. Mirrors the regenerate safety timeout.
  useEffect(() => {
    if (!continuing) return
    const t = setTimeout(() => { setContinuing(false) }, 30_000)
    return () => clearTimeout(t)
  }, [continuing])
  const handleContinue = useCallback(() => {
    if (!activeSlot || continuing || !continuable) return
    setContinuing(true)
    // No optimistic transcript mutation: the backend appends the continuation as
    // an `inject` row and the WS `slots` update flips `running`, so the UI
    // converges from the server. Nothing to roll back on failure.
    api.continueSlot(activeSlot).catch((e: unknown) => {
      showRefusedPress('continue', e)
      setContinuing(false)
    })
  }, [activeSlot, continuing, continuable, showRefusedPress])
  // Index of the newest error row. Only that one gets the action: an error
  // further up the transcript belongs to a turn that has already been
  // superseded, and offering to "continue" it would resume the wrong thing.
  const lastErrorIdx = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === 'error') return i
    return -1
  }, [messages])

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
    revealComposer()
  }, [])

  // "Ask" (Select-to-Ask): open the isolated /side conversation seeded with the
  // selection, WITHOUT touching the main chat context (unlike handleQuote, which
  // injects into the main composer). Mirrors the /side slash command's
  // openActivityToTab('side') bridge, then hands the selection to SideChat via a
  // `side-seed` CustomEvent (same event-bridge pattern as openActivityToTab —
  // no new prop-drilling, no backend change). No transit
  // animation: the popup routes the selection straight to the Side Chat panel
  // (matches Codex's "Ask in side chat" behavior).
  const handleAsk = useCallback((text: string) => {
    dispatch(openActivityToTab('side'))
    // The Side Chat panel (and its `side-seed` listener) mounts asynchronously
    // once the panel opens. Poll a few frames for its input as a mount signal,
    // then dispatch the seed. Fall back to dispatching after a cap so the
    // feature still works even if the input never resolves.
    const trySeed = (attempt = 0) => {
      const mounted = document.querySelector('textarea[data-side-chat-input]')
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
    (c: string, mt: Record<string, unknown> | undefined) => renderUserContent(c, mt, handleFileOpen, handleFolderOpen, linkPreviewsOn),
    [handleFileOpen, handleFolderOpen, linkPreviewsOn]
  )

  const cancelTitleRef = useRef(false)
  // The session-title field is an Enter-to-commit input; the guard owns both the
  // composition latch and the keypress, so the rename cannot fire on the Enter that
  // commits an IME candidate.
  const titleIme = useImeGuard()
  useEffect(() => {
    const togglePin = () => {
      // Always-available collapse. Only guard is no-sessions (the sidebar is
      // force-open then anyway, so there is nothing to collapse).
      if (filteredSlotsRef.current.length === 0) return
      // Explicit user intent outranks the preview-expand auto-hide, so exiting
      // expand mode leaves this choice alone.
      sidebarAutoHidden.current = null
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
  // Advances with every streamed chunk, so ChatFooter can tell "text is arriving"
  // apart from "the stream went quiet mid-turn" (the model generating a tool call,
  // or a tool group holding the trailing 'streaming' message open). 0 whenever no
  // streaming message is in flight.
  const streamTick = lastRole === 'streaming' ? (messages[messages.length - 1]?.content.length ?? 0) : 0
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
  // Keyed on the REQUEST counter, never on the tab's value. `activityTab` also
  // changes when a chat switch restores the incoming chat's cached tab (Files
  // when it has none), and bridging that would force-focus Files — or whatever
  // view was last requested in that chat — over the tab the tab strip has
  // remembered and the user actually left the chat on. Only openActivityToTab
  // bumps the counter, so only a deliberate request moves focus.
  const activityTabRequest = useAppSelector(s => s.chat.activityTabRequest)
  // Skip the mount invocation: the counter is already non-zero after any earlier
  // request this page load, so firing on mount would re-open that view on top of
  // the now-persisted strip every time ChatPage remounts after a route change.
  const activityTabBridged = useRef(false)
  useEffect(() => {
    if (!activityTabBridged.current) { activityTabBridged.current = true; return }
    if (activityOpen) tabsCtl.openView(activityTab === ('nav' as string) ? 'files' : activityTab)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activityTabRequest])
  // Stable row callbacks. Inline lambdas in the row renderer would hand
  // AssistantMessage a fresh function identity every render, so its memo()
  // could never bail out — the boundary would break at the call site, not in the
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
  // read the latest displayItems. useLayoutEffect (not useEffect): the DOM's
  // `data-display-index` attributes are updated at commit, but a scroll rAF can
  // fire before React flushes a PASSIVE effect — so with useEffect the pin
  // recompute could read fresh DOM indices against a stale list, mis-deriving
  // `pinned.idx` by one row (the row-hide is identity-keyed as a second guard,
  // see below). A layout effect runs in the commit phase, before that rAF, so
  // the ref is caught up by the time the recompute reads it. Still a passive
  // side effect, not render-body mutation, so React's rules of render hold.
  useLayoutEffect(() => { displayItemsRef.current = displayItems }, [displayItems])

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
  // them on the SAME identity means the steer-bubble stability fix protects
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
  //      refetch/replace.)
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

  // Reaching the top of a resumed transcript fetches the history behind the loaded slice.
  const handleTopReached = useCallback(() => {
    const chat = store.getState().chat
    if (!shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })) return
    void dispatch(loadOlderMessages())
  }, [dispatch])
  // The click path needs no gate beyond the in-flight check the thunk already makes.
  const handleLoadEarlier = useCallback(() => {
    if (store.getState().chat.loadingOlder) return
    void dispatch(loadOlderMessages())
  }, [dispatch])

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
    // A first measurement lands in the offset tree immediately instead of
    // waiting out the height-sync debounce. Without this, a fast scroll or a
    // FAR jump mounts a streak of rows whose real heights sit outside the
    // spacer math for up to the debounce window; when they reconcile, content
    // shifts under the viewport. Chrome's native scroll anchoring absorbs
    // that shift, iOS Safari has none — measured 13-25px of post-jump drift
    // with anchoring disabled (the "jump lands off by a bit" report). First
    // measurements happen once per row, so they cannot be the oscillation the
    // debounce exists to smother.
    eagerFirstMeasure: true,
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
    onTopReached: handleTopReached,
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
  // files `[attached_file N]` tokens. kiro-cli's `_session/steer` is a
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
    // Nothing to inject into: the composer is busy purely because background
    // sub-agents are still running for this slot (spawn_run is fire-and-forget,
    // so the parent turn already ended). The intent is the same — act on this
    // text now, don't park it — so start a real turn through the normal send
    // path, which carries `ws=1` and so streams, and flag it to skip the
    // server-side hold that keeps a user message behind running sub-agents.
    // Delegating here, BEFORE the composer is read and cleared below, leaves
    // send() owning the draft, attachment and optimistic-bubble bookkeeping.
    // A multi-stage autopilot plan also reads busy-but-not-running. There the
    // server keeps `_in_stage_execution` set for the WHOLE plan, so the flag
    // finds no live session to inject into and the message queues — the right
    // answer between stages, and unconditional across the plan rather than a
    // race with the gaps.
    if (!slotRunning) { void send(undefined, undefined, true); return }
    const raw = inputRef.current.trim()
    const files = pendingFilesRef.current
    if (!raw && !files.length) return
    // Client-side slash commands (/side, /onboarding) are UI commands, not
    // turn content: they must work identically whether the agent is mid-turn
    // or idle. Without this guard the command text is steered into the
    // running turn as a literal message and the command never runs (#1857).
    // interceptSlashCommand is async, so gate on the sync matcher first and
    // fire-and-forget the handler — same contract as send()'s intercepted
    // branch, which also doesn't await side-open before clearing the composer.
    if (isInterceptedSlashCommand(raw)) {
      // Expand paste tokens first: a large paste after "/side " sits in the
      // composer as a `[ Paste #N ]` token whose backing block is cleared
      // below — without expansion the side chat would receive the literal
      // token instead of the pasted content.
      const pastes = pasteBlocksRef.current
      const cmdTxt = pastes.length ? expandPasteTokens(raw, pastes) : raw
      // Fire-and-forget, but recoverable: on failure (409 side turn in
      // flight, 400 question too long, side-open rejected) the question is
      // merged back so it is never silently lost. The restore is bound to
      // the ORIGINATING slot, captured here — the user may switch slots
      // before the rejection lands. On-screen and settled (same dance as
      // the voice-transcript delivery above): merge into the live composer.
      // Otherwise: merge into the origin slot's persisted draft.
      // mergeIntoDraft appends after a paragraph break instead of replacing,
      // so text the user typed in the meantime survives alongside the
      // recovered question (same contract as the hand-off paths).
      const originSlot = activeSlotRef.current
      void interceptSlashCommand(cmdTxt, originSlot, dispatch).then(res => {
        if (!res.intercepted || !res.failed || !originSlot) return
        const onScreen = originSlot === activeSlotRef.current && composerSlotRef.current === originSlot
        if (onScreen) {
          setInput(mergeIntoDraft(inputRef.current, cmdTxt))
        } else {
          const merged = mergeIntoDraft(drafts.current[originSlot], cmdTxt)
          setDraft(drafts.current, originSlot, merged)
          // Mid-switch guard (same as the voice-transcript delivery): if the
          // composer still belongs to originSlot — activeSlot advanced in
          // render but the outgoing-slot persist effect hasn't run yet — that
          // effect will flush inputRef.current into drafts[originSlot] and
          // overwrite the merge. Carry the merged value into inputRef too so
          // the flush preserves it.
          if (composerSlotRef.current === originSlot) inputRef.current = merged
          saveDrafts()
        }
      })
      setInput(''); setPasteBlocks([])
      return
    }
    const { txt } = prepareSendPayload(raw, files)
    // Folder tokens deliberately stay in their `@rel/` form on steer: the
    // steer transport is TEXT-ONLY (no meta), so a `[attached_dir N] /abs
    // path` marker would have no meta.dirs index to replay against and the
    // whitespace-bounded fallback truncates a path containing spaces — the
    // chip would then open the wrong directory. The raw token is what the
    // agent resolved before serialization existed, and it stays correct
    // under replay. Serialize on steer only if that transport ever carries
    // attachment metadata.
    const activePastes = pasteBlocksRef.current
    const llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
    // Optimistically show the steered text immediately. Steer is the default
    // mid-turn action (split send button), so pressing Enter while a turn is
    // running routes here; without an optimistic bubble the message only appears
    // once the backend echoes it via the 'steer_push' WS event, making it look
    // like nothing happened until the response resumes.
    // Tagged meta.optimistic so the echo reconciles this bubble in place
    // (appendSlotMessage) instead of rendering a duplicate.
    dispatch(appendMessage({ role: 'user', content: llmTxt, cls: 'msg msg-u', ts: new Date().toISOString(), meta: { steer: true, optimistic: true } }))
    steerMutation.mutate(llmTxt)
    // Staged session references are deliberately NOT part of steering: neither
    // carried into the payload nor cleared. `steerMutation`'s onError only logs,
    // so anything cleared here is gone for good — text, attachments and pastes
    // have always been discarded on a failed steer, and adding refs to that set
    // would lose a reference the user cannot recover except by dragging again.
    // Leaving them staged is lossless and predictable: the chip stays in the
    // composer and rides the next real send, which does have a restore path.
    setInput(''); setPendingFiles([]); pickedFileTokens.current = {}; setPasteBlocks([])
    delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]
    saveDrafts()
  }, [activeSlot, slotRunning, send, steerMutation, saveDrafts, dispatch])

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

  const handleReorderQueued = useCallback((queueId: string, direction: 'next' | 'later') => {
    if (!activeSlot) return
    const slot = activeSlot
    // Build the order from ALL queued messages (allQueuedMessages includes
    // hidden system deliveries and recovery continuations), not just the
    // interactive cards: submitting only visible ids would let the backend
    // append the omitted ones at the tail, silently demoting automation. The
    // swap is between adjacent VISIBLE cards, expressed inside the full order.
    const fullIds = allQueuedMessages.map(m => m.meta?.queueId as string).filter(Boolean)
    const visibleIds = queuedMessages.map(m => m.meta?.queueId as string).filter(Boolean)
    const vFrom = visibleIds.indexOf(queueId)
    const vTo = direction === 'next' ? vFrom - 1 : vFrom + 1
    if (vFrom < 0 || vTo < 0 || vTo >= visibleIds.length) return
    const a = fullIds.indexOf(visibleIds[vFrom])
    const b = fullIds.indexOf(visibleIds[vTo])
    if (a < 0 || b < 0) return
    const next = [...fullIds]
    ;[next[a], next[b]] = [next[b], next[a]]
    // No optimistic dispatch: the server commits and broadcasts queue_reorder
    // to every client including this one, and that WS event is the
    // authoritative store update. A local dispatch with rollback-on-failure
    // could restore a stale order when the server committed but the HTTP
    // response was lost, leaving this client in conflict with execution order.
    api.reorderQueuedMessages(slot, next).catch(() => undefined)
  }, [activeSlot, allQueuedMessages, queuedMessages])


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

  // ── Chat Pins ──────────────────────────────────────────────────────────────
  const {
    pins: chatPins,
    loading: chatPinsLoading,
    error: chatPinsError,
    clearError: clearChatPinsError,
    isPinned,
    pinMessage,
    unpinMessage,
    unpinById,
  } = useChatPins(activeSlot ?? undefined)
  const [pinNotice, setPinNotice] = useState<string | null>(null)
  const [pendingPinnedJump, setPendingPinnedJump] = useState<{
    slotKey: string
    messageTs: string
    mid?: string
  } | null>(null)
  const pinnedJumpPageLoadsRef = useRef(0)
  const jumpToLoadedPinnedMessage = useCallback((messageTs: string, mid?: string): boolean => {
    // Prefer mid-based resolution (unique identity); fall back to ts for legacy pins.
    let msgIdx = -1
    if (mid) {
      msgIdx = messages.findIndex(m => (m.meta as Record<string, unknown> | undefined)?.mid === mid)
    }
    if (msgIdx < 0) {
      msgIdx = messages.findIndex(m => m.ts === messageTs)
    }
    if (msgIdx < 0) return false
    const di = messageToDisplayIdxRef.current.get(msgIdx)
    if (di === undefined) return false
    setPinNotice(null)
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
    setHighlightTs(messageTs)
    setTimeout(() => setHighlightTs(null), 3000)
    return true
  }, [messages, navToDisplayIndex])
  const handleJumpToPinnedMessage = useCallback((messageTs: string, mid?: string) => {
    if (jumpToLoadedPinnedMessage(messageTs, mid)) return
    if (activeSlot && (!cursorIsForActiveSlot || (slotHasMore && slotOldestIndex > 0))) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(null)
      setPendingPinnedJump({ slotKey: activeSlot, messageTs, mid })
      return
    }
    setPinNotice(i18nT('pages.chat.pins.message_unavailable'))
  }, [activeSlot, cursorIsForActiveSlot, jumpToLoadedPinnedMessage, slotHasMore, slotOldestIndex])
  useEffect(() => {
    if (!pendingPinnedJump) return
    if (pendingPinnedJump.slotKey !== activeSlot) {
      pinnedJumpPageLoadsRef.current = 0
      setPendingPinnedJump(null)
      return
    }
    if (jumpToLoadedPinnedMessage(pendingPinnedJump.messageTs, pendingPinnedJump.mid)) {
      pinnedJumpPageLoadsRef.current = 0
      setPendingPinnedJump(null)
      return
    }
    // The cursor still describes the chat we left; wait for the switch to settle
    // rather than read its has-more as this chat's.
    if (!cursorIsForActiveSlot) return
    if (!slotHasMore || slotOldestIndex <= 0) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(i18nT('pages.chat.pins.message_unavailable'))
      setPendingPinnedJump(null)
      return
    }
    if (loadingOlder) return

    pinnedJumpPageLoadsRef.current += 1
    let cancelled = false
    void dispatch(loadOlderMessages()).unwrap().then(result => {
      if (!cancelled && result === null) {
        pinnedJumpPageLoadsRef.current = 0
        setPinNotice(i18nT('pages.chat.pins.message_unavailable'))
        setPendingPinnedJump(null)
      }
    }).catch(err => {
      // Cancelled or refused means the user switched chat, not that the pin is
      // unreachable.
      if (isSupersededPagingRejection(err)) return
      if (!cancelled) {
        pinnedJumpPageLoadsRef.current = 0
        setPinNotice(i18nT('pages.chat.pins.message_unavailable'))
        setPendingPinnedJump(null)
      }
    })
    return () => { cancelled = true }
  }, [
    activeSlot,
    cursorIsForActiveSlot,
    dispatch,
    jumpToLoadedPinnedMessage,
    loadingOlder,
    pendingPinnedJump,
    slotHasMore,
    slotOldestIndex,
  ])
  const handleTogglePinForMessage = useCallback((mid: string, messageTs: string, role: 'user' | 'assistant', content: string) => {
    if (isPinned(mid)) {
      void unpinMessage(mid).catch(() => {}) // useChatPins exposes the localized error state.
      return
    }
    // A session's FIRST pin opens the Pins tab, so the pin has a visible
    // destination -- the same shape as the Issues reveal, and for the same
    // reason: Pins is an on-demand view, so nothing would surface it otherwise.
    // A session pinned earlier reaches it through the + menu (Issues' zero
    // option for pre-existing links), which is what keeps this free of a
    // persisted reveal claim.
    // Read before the mutation so the optimistic insert has not landed yet.
    const isFirstPin = chatPins.length === 0
    void pinMessage({ mid, message_ts: messageTs, role, preview: content }).catch(() => {})
    if (isFirstPin && activeSlot) {
      // Addressed by slot, not through tabsCtl, for the same reason as the
      // source-reveal path: that binding can be a chat being left.
      openPanelView(activeSlot, 'pins')
      // Pinning is NOT a navigation request, so it must not cost the user state
      // they are mid-way through. Unlike the source-reveal path this does not
      // close the find pane: someone who searched the transcript to FIND the
      // message they are pinning would lose the pane and its results on the very
      // click that acts on a result. Below the mobile breakpoint the panel opens
      // full width, so opening it would navigate them off the chat entirely.
      // The tab is still created above -- it is revealed quietly instead.
      if (!search.isOpen && !isMobile) dispatch(openActivityPanel())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- search re-identifies on every keystroke; only its isOpen flag is read
  }, [activeSlot, chatPins.length, dispatch, isMobile, isPinned, pinMessage, search.isOpen, unpinMessage])
  const handleUnpinById = useCallback((id: string) => {
    void unpinById(id).catch(() => {})
  }, [unpinById])
  const pinStatus = pinNotice ?? (chatPinsError
    ? i18nT(chatPinsError === 'pin' ? 'pages.chat.pins.pin_failed' : 'pages.chat.pins.unpin_failed')
    : null)
  const dismissPinStatus = useCallback(() => {
    setPinNotice(null)
    clearChatPinsError()
  }, [clearChatPinsError])
  useEffect(() => {
    if (!pinStatus) return
    const timeout = window.setTimeout(dismissPinStatus, 8000)
    return () => window.clearTimeout(timeout)
  }, [pinStatus, dismissPinStatus])

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
  // used by the fork button. Avoids a per-row O(i) filter that would make the
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
    // Key identity rules (clientTs preference + streaming→assistant role
    // normalization) live in messageRowKey — see its doc comment.
    const key = messageRowKey(m, i)
    if (m.role === 'thinking') return m.content ? <ThinkingBlock key={key} content={m.content} disclosureKey={key} /> : null
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
      return <ToolCallLine key={key} message={m} running={isInTrailingGroup} onFileOpen={handleFileOpen} disclosure={toolDisclosure[key]} disclosureKey={key} onDisclosureChange={setToolDisclosureFor} appInPanel={mcpAppPanel} onOpenApp={revealAppInPanel} />
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
      return <NudgeCard key={key} message={m} disclosureKey={key} onOpenLoop={ownLoop ? () => setAutoNudgeOpen(true) : undefined} />
    }
    if (m.kind === 'stop_event' || m.meta?.kind === 'stop_event') return <StopEventCard key={m.meta?.id as string ?? key} message={m} />
    // A synthetic turn-recovery continuation (tool refusal / stalled turn /
    // stalled tool) is machine-facing instruction text. It stays in the
    // transcript for auditability, but as a one-line card that names the event
    // and the deny pattern rather than a full-width bubble of prompt prose.
    if (m.role === 'inject') {
      // One shared decision (resolveInjectCard) so this surface and the
      // transcript-renderer registry cannot disagree about the same row. It
      // returns null for a cron row, for a replay of the user's own words, and
      // for a row with no provenance stamp — each of which keeps the renderer
      // below. Anything positively marked gateway-authored folds into a note
      // instead of falling through to a full-width bubble, which is the defect
      // this replaces.
      const card = resolveInjectCard(m)
      if (card) return <RecoveryCard key={key} parsed={card} disclosureKey={key} />
    }
    if (m.role === 'error') return (
      <ErrorCard
        key={key}
        content={m.content}
        onContinue={continuable && interrupted && i === lastErrorIdx ? handleContinue : undefined}
        continuing={continuing}
      />
    )
    if (m.role === 'notice') return <div key={key} className="bg-card text-muted text-[13px] leading-5 px-3 py-2 rounded-md ring-1 ring-inset forced-colors:border ring-border self-center animate-scale-in">{m.content}</div>
    if (m.role === 'permission') return null
    if (m.role === 'mcp_oauth') {
      const banner = renderMcpOAuthMessage(m, connectionsUiOn)
      return banner ? <div key={key}>{banner}</div> : null
    }
    // An injected workflow completion event renders as a compact status card
    // (with the full result folded away) instead of a wall of raw JSON.
    if (isWorkflowCompletionMessage(m)) return <WorkflowCompletionCard key={key} message={m} onFileOpen={handleFileOpen} onFolderOpen={handleFolderOpen} disclosureKey={key} />
    // An injected sub-agent completion event is machine-facing prompt text (the
    // spawn-discipline instructions are addressed to the model). It renders as a
    // compact outcome row with the payload folded away, not as a chat bubble.
    if (isSubagentCompletionMessage(m)) return <SubagentCompletionCard key={key} message={m} onFileOpen={handleFileOpen} onFolderOpen={handleFolderOpen} disclosureKey={key} onOpenPanel={handleSubagentPanelOpen} />
    const isUser = m.role === 'user'
    const isStreaming = m.role === 'streaming'
    const isInject = m.role === 'inject'
    // Pass a stable handleFork (useCallback) + primitive index so memo()
    // on AssistantMessage can short-circuit when only unrelated state changes.
    // visibleIndexMap is O(1) per row.
    const canFork = !isStreaming && !isInject && !slotHasMore
    const forkIndex = canFork ? visibleIndexMap.get(i) : undefined
    const msgTime = fmtMessageTime(m.ts)
    const msgTimeFull = fmtMessageTimeFull(m.ts)
    return (
      <MessageSearchScope key={key} messageIdx={i}>
      <div className={`group flex flex-col min-w-0 ${isUser ? 'items-end' : ''} ${m.ts && m.ts === highlightTs ? 'animate-msg-highlight rounded-lg' : ''}`}>
        <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${isUser ? 'items-end' : ''}`}>
          {isUser ? (
            <UserMessage
              content={m.content}
              meta={m.meta}
              timestamp={chatConfig.showTimestamps ? msgTime : undefined}
              timestampTitle={msgTimeFull}
              renderContent={renderUserContentCb}
              canEdit={!slotRunning && !regenerating && !!activeSlot}
              messageIndex={i}
              messageTs={m.ts || ''}
              onEditResend={handleEditResend}
              slotKey={activeSlot || undefined}
              slotTitle={activeSlotTitle}
              mode={mode}
              pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false}
              onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? () => handleTogglePinForMessage((m.meta as Record<string, unknown>).mid as string, m.ts!, 'user', m.content) : undefined}
            />
          ) : isInject ? (
            (() => {
              const cronLabel = (m.meta?.cronLabel as string) || ''
              // Strip wrapper tags — LLM needs them for context but user sees clean content
              const cleanContent = cronLabel
                ? m.content.replace(/^\[Cron notification from ".*"\]\n/, '').replace(/\n\[End of cron notification\]$/, '')
                : m.content
              return <>
                {cronLabel && <span className="text-muted text-[11px] leading-4 font-medium px-1 mb-1"><Clock className="lucide-inline" /> {cronLabel}</span>}
                <div className="msg-content px-4 py-3 text-sm leading-6 whitespace-pre-wrap rounded-lg bg-warning-subtle text-fg ring-1 ring-inset forced-colors:border ring-warning/30 rounded-bl-[4px] overflow-hidden min-w-0" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}><MessageErrorBoundary rawContent={cleanContent}><MarkdownRenderer content={cleanContent} /></MessageErrorBoundary></div>
                {/* No `font-mono`: a formatted date is prose, and Tailwind's
                    `font-mono` pins `var(--mono)` — a token the Font Family
                    setting never writes, so it overrode the user's choice and
                    put JetBrains Mono (no CJK coverage) under a date that a
                    zh/ja dashboard renders WITH CJK characters. `tabular-nums`
                    keeps the digits fixed-width, which is the alignment the
                    mono was actually there for. */}
                {chatConfig.showTimestamps && msgTime && <span className="text-muted text-[12px] leading-4 tabular-nums px-1" title={msgTimeFull}>{msgTime}</span>}
              </>
            })()
          ) : (
            <div className="flex flex-col gap-0">
              <AssistantMessage linkPreviews={linkPreviewsOn} content={m.content} isStreaming={isStreaming} isRegenerating={regenerating && i === lastTextIdx} onFileOpen={handleFileOpen} onFolderOpen={handleFolderOpen} onArtifactOpen={handleArtifactOpen} onQuote={handleQuote} onAsk={handleAsk} slotRunning={slotRunning} planTaskId={planTaskId} timestamp={chatConfig.showTimestamps ? msgTime : undefined} timestampTitle={msgTimeFull} messageTs={m.ts} slotKey={activeSlot || undefined} slotTitle={activeSlotTitle} mode={mode} fileChanges={(m.meta as Record<string, unknown> | undefined)?.file_changes as FileChangeEntry[] | undefined} turnStats={chatConfig.showTurnStats ? (m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined : undefined} onOpenDiff={handleOpenDiff} fileChipStyle={chatConfig.fileChipStyle} artifactPaths={artifactPaths} pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false} onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? () => handleTogglePinForMessage((m.meta as Record<string, unknown>).mid as string, m.ts!, 'assistant', m.content) : undefined} showFooter={(() => {
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
                showRefusedPress('switch_variant', e)
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
  }, [messages, visibleIndexMap, slotRunning, slotState, lastTextIdx, handleFileOpen, handleArtifactOpen, handleFork, handleQuote, handleAsk, chatConfig, activeSlot, regenerating, handleRegenerate, handleEditResend, slotHasMore, renderUserContentCb, highlightTs, activeSlotTitle, mode, dispatch, handleOpenDiff, handlePlanFromHere, navigate, planTaskId, artifactPaths, autoNudgeLoop, toolDisclosure, setToolDisclosureFor, linkPreviewsOn, handleSubagentPanelOpen, isPinned, handleTogglePinForMessage, connectionsUiOn, showRefusedPress])

  const [mobileSessions, setMobileSessions] = useState(false)
  // Close mobile sessions panel when a session is selected
  useEffect(() => { if (isMobile) setMobileSessions(false) }, [activeSlot]) // eslint-disable-line react-hooks/exhaustive-deps
  // Reset mobile sessions state when leaving mobile viewport
  useEffect(() => { if (!isMobile) setMobileSessions(false) }, [isMobile])
  // Swipe from left edge to open sidebar, swipe left on backdrop to close
  const chatContainerRef = useRef<HTMLDivElement>(null)
  // Measured container height — sizes the sidebar border-box morph (the panel
  // rect the box shrinks from on collapse and grows back to on expand).
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
  /** True while the INLINE side panel (mobile / embed, no actbar column) is
   *  mounted AND visible.
   *
   *  Mobile has no actbar grid column, so the panel renders as a flex sibling of
   *  the chat pane at the full window width — it covers the content area
   *  outright. Anything the chat pane floats over that area (the sessions FAB
   *  below) would land on top of the panel's own controls, so it is gated on
   *  this. Reuses the panel's own mount/visibility predicates rather than
   *  re-deriving them from `activityOpen`, which is only one of their inputs (a
   *  live app or browser tab keeps the panel mounted through a close, and the
   *  find pane hides it while owning the dock). */
  const inlineSidePanelShowing = !activitySlot
    && shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })
    && !isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })
  const openSidebar = useCallback(() => setMobileSessions(true), [])
  const closeSidebar = useCallback(() => setMobileSessions(false), [])
  useSwipeEdge(chatContainerRef, { enabled: isMobile && !mobileSessions, edge: 'left', edgeZone: 0.35, onSwipe: openSidebar })
  useSwipeEdge(chatContainerRef, { enabled: isMobile && mobileSessions, edge: 'right', threshold: 50, edgeZone: 9999, onSwipe: closeSidebar })
  /** Reveal a session's pull request / issue in that session's side panel.
   *
   *  Fires from a sidebar chip AFTER ChatSidebar has dispatched the slot switch,
   *  so `switchSlot.pending` has already published the target slot to the store —
   *  but activeSlotRef is assigned during RENDER and still names the chat being
   *  left, so `slot` is threaded explicitly through every write below.
   *
   *  The url is re-parsed rather than trusted: the chip payload comes from the
   *  BACKEND's scan, and running it through the panel's own parser is what
   *  guarantees the injected link matches the shape (and the host allowlist) the
   *  panels already work with.
   *
   *  Returns whether the panel took the link. FALSE hands the click back to the
   *  chip's own anchor, so a url this parser rejects opens the provider instead
   *  of doing nothing at all. That is reachable rather than theoretical: the two
   *  parsers read the self-managed GitLab allowlist from different places, and
   *  `sourceHosts` is empty until the dashboard-config query resolves (and stays
   *  empty if it fails), so every self-hosted chip parses to null in that window
   *  even though the backend scan accepted it. */
  const revealSourceLink = useCallback((slot: string, chip: { url: string; kind: SourceLinkKind }): boolean => {
    const link = parseSourceLinkUrl(chip.url, sourceHostsRef.current, jiraSourceHostsRef.current)
    if (!link) return false
    const view = link.kind === 'issue' ? 'issues' : 'changes'
    // Durable BEFORE the state update, and one key at a time. Writing inside the
    // updater would both make it impure (React may invoke an updater more than
    // once) and publish this window's whole map, deleting a sibling window's
    // reveals — see `commitRevealedSource`.
    commitRevealedSource(slot, link.kind, link.url)
    setRevealedSources(previous => ({
      ...previous,
      [slot]: { ...previous[slot], [link.kind]: link },
    }))
    selectSource(link.kind, link.url, slot)
    // Addressed by slot, not through tabsCtl: that binding is still the chat
    // being left, so the tab would open on the wrong strip.
    openPanelView(slot, view)
    // The find pane owns the right-hand dock exclusively (shouldMountSidePanel
    // returns false while it is open), so revealing into a session with search
    // open would suppress the chip's navigation and then mount nothing at all.
    // Same reason handleFileOpen / handleOpenDiff close it before opening a dock
    // panel.
    search.close()
    dispatch(openActivityToTab(view))
    // The mobile session drawer covers the panel it would reveal into. The
    // activeSlot effect closes it on a real switch, but a chip on the session
    // already open does not change activeSlot.
    if (isMobile) setMobileSessions(false)
    return true
  }, [dispatch, isMobile, selectSource])
  // Web Preview expand mode — broadcast by the Web Preview tab's
  // expand toggle. When on, hide the session list and maximize the side panel
  // (passed to SidePanel), so the preview gets max room and chat shrinks to its
  // minimum. App collapses the left nav off the same event.
  //
  // Hiding the list drives `sidebarPinned` directly instead of overriding
  // `sidebarOpen`: an override leaves the sessions toggle visibly present but
  // inert. Driving the real state keeps that toggle working normally inside
  // expand mode. `sidebarAutoHidden` holds the pre-expand state to restore on
  // exit, and is cleared once the user toggles the list themselves. Neither
  // transition persists `mc-sidebar-pinned` — only a user toggle does.
  //
  // The ref is read and cleared HERE, in the handler, and only plain values
  // reach the setter: a state updater must be pure, and React invokes one twice
  // under StrictMode, which would make the second pass read an already-cleared
  // ref and lose the restore value.
  //
  // The mobile drawer is a separate state, so it is closed outright rather than
  // suppressed — a swipe or a tap still reopens it, which an override would not
  // allow.
  const [previewExpanded, setPreviewExpanded] = useState(false)
  useEffect(() => {
    const onPreviewExpand = (e: Event) => {
      const expanded = !!(e as CustomEvent<{ expanded?: boolean }>).detail?.expanded
      setPreviewExpanded(expanded)
      if (expanded) {
        setMobileSessions(false)
        if (sidebarAutoHidden.current === null) sidebarAutoHidden.current = sidebarPinnedRef.current
        setSidebarPinned(false)
        return
      }
      const prior = sidebarAutoHidden.current
      sidebarAutoHidden.current = null
      if (prior !== null) setSidebarPinned(prior)
    }
    window.addEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
    return () => window.removeEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
  }, [])
  // The no-sessions force-open yields to expand mode: with an empty list no
  // sessions toggle is rendered, so suppressing it makes nothing inert, and the
  // preview would otherwise stay covered by a list that cannot be dismissed.
  const sidebarOpen = isMobile
    ? mobileSessions
    : (sidebarPinned || (filteredSlots.length === 0 && !previewExpanded))

  // ── Collapsed-sidebar hover flyout ──────────────────────────────────────
  // Hovering the toggle while collapsed opens a recents list over the chat, so
  // switching sessions stops being expand → switch → collapse. It is purely an
  // overlay: it never touches `sidebarPinned`, because `panelReserve` and
  // `panelFillWidth` below both read `sidebarOpen`, and flipping it to show a
  // transient popover would re-run the side panel's width maths and visibly
  // resize the chat every time the pointer rested on a 28px button.
  const flyoutTriggerRef = useRef<HTMLButtonElement>(null)
  const flyoutSurfaceRef = useRef<HTMLDivElement>(null)
  // Touch is a second gate beyond isMobile: a desktop-width touch device has no
  // hover, so the flyout would only ever appear as a tap artefact.
  const flyoutEligible = !isMobile && !isTouchDevice() && !splitMode
    && embedMode !== 'chat' && embedMode !== 'sessions'
    && !sidebarOpen && filteredSlots.length > 0
  const flyout = useHoverIntent({
    enabled: flyoutEligible,
    triggerRef: flyoutTriggerRef,
    surfaceRef: flyoutSurfaceRef,
  })
  // Rect the sidebar's clip window should expand FROM, captured at click time
  // from the live flyout element. Null when the expand came from the button
  // alone, which keeps the stock button-rect morph for that path.
  const [expandFrom, setExpandFrom] = useState<{ x: number; y: number; w: number; h: number } | null>(null)
  const expandSidebar = useCallback((fromFlyout: boolean) => {
    const surface = flyoutSurfaceRef.current
    const container = chatContainerRef.current
    if (fromFlyout && surface && container) {
      const s = surface.getBoundingClientRect()
      const c = container.getBoundingClientRect()
      setExpandFrom({ x: s.left - c.left, y: s.top - c.top, w: s.width, h: s.height })
    } else {
      setExpandFrom(null)
    }
    flyout.close()
    window.dispatchEvent(new CustomEvent('toggle-pin-chat-sidebar'))
  }, [flyout])
  // The rect is only valid for the mount it was captured for. Clearing it on
  // collapse means a later button-only expand cannot inherit a stale flyout
  // rect and appear to grow out of nothing.
  useEffect(() => { if (!sidebarOpen) setExpandFrom(null) }, [sidebarOpen])
  const flyoutSwitch = useCallback((key: string) => {
    dispatch(switchSlot(key))
    setSplitMode(false)
    flyout.close()
  }, [dispatch, flyout])
  const flyoutNew = useCallback(() => {
    const effectiveMode = loadChatConfig().defaultAutopilot ? 'orchestrator' : (mode || '')
    flyout.close()
    // `focusComposerAfter`, not a bare dispatch + rAF: there is one composer and
    // it is bound to the ACTIVE slot, so focusing before creation fulfils puts
    // the caret on the old session and loses whatever is typed. See the module.
    focusComposerAfter(dispatch(createSlot({ agent: defaultAgent || undefined, mode: effectiveMode })).unwrap())
  }, [dispatch, defaultAgent, mode, flyout])

  // Force the list open when there is nothing in it, so a user with no sessions
  // still has the surface that creates one. Skipped while expand mode owns the
  // hidden state: re-pinning there would fight the auto-hide and, worse, persist
  // 'true' over the user's stored preference, which the restore on exit then
  // contradicts in the live state.
  useEffect(() => {
    if (filteredSlots.length === 0 && !sidebarPinned && !previewExpanded) {
      setSidebarPinned(true)
      safeSetItem('mc-sidebar-pinned', 'true')
    }
  }, [filteredSlots.length, sidebarPinned, previewExpanded])

  // Horizontal space (px) the detail panel must keep clear so it never grows
  // past its flex row and collapses the chat pane: the open sidebar's width
  // plus a usable chat-pane minimum. On mobile the panel is full-screen (no
  // shared row), so no reserve applies.
  const CHAT_PANE_MIN = CHAT_PANE_MIN_W
  const panelReserve = isMobile ? undefined : (sidebarOpen ? sidebarWidth : 0) + CHAT_PANE_MIN
  // The panel takes its maximum only while the session list is actually hidden.
  // That maximum is measured against the header's reserve, which knows nothing
  // about the session list's width — so keeping it while the user reopens the
  // list inside expand mode pushes the chat pane below CHAT_PANE_MIN and clips
  // its content. Reverting to the normal width maths there costs the preview a
  // few hundred px in a state the user asked for by reopening the list.
  const panelMaximized = previewExpanded && !sidebarOpen

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
    <RowDisclosureProvider resetKey={activeSlot}>
    <TagPopoverProvider>
    {/* Self-hosted Jira allowlist for every markdown anchor in the page --
        message bodies, previews, and panels alike -- so a pasted Jira URL
        chips identically wherever it renders. Cloud URLs need no provider. */}
    <JiraHostsCtx.Provider value={jiraSourceHosts}>
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
      {/* Sidebar toggle — absolute in the stable container in BOTH states
          (only the icon flips), so collapsing cannot drag it sideways with
          the reflowing content pane. The collapse/expand motion itself is the
          panel deforming into/out of this button's rect (OverlayDrawer morph
          mode, morphTarget below). Desktop, non-embed, with sessions only.
          While collapsed, hovering it opens the recents flyout below; clicking
          hands that flyout's rect to the drawer so the panel grows out of it. */}
      {!isMobile && embedMode !== 'chat' && embedMode !== 'sessions' && filteredSlots.length > 0 && (
        <button
          ref={flyoutTriggerRef}
          type="button"
          onClick={() => expandSidebar(flyout.open)}
          {...flyout.triggerProps}
          aria-haspopup={flyoutEligible ? 'menu' : undefined}
          aria-expanded={flyoutEligible ? flyout.open : undefined}
          // Geometry mirrored by TOGGLE_RECT (chat/SessionFlyout) — every
          // surface in this interaction grows out of and back into this rect.
          className="pi-morph absolute top-[9px] left-2 z-[61] w-7 h-7 rounded-md flex items-center justify-center cursor-pointer text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none"
          title={sidebarOpen ? i18nT('pages.chatPage.hide_sessions') : i18nT('pages.chatPage.show_sessions')}
          aria-label={sidebarOpen ? i18nT('pages.chatPage.hide_sessions_sidebar') : i18nT('pages.chatPage.show_sessions_sidebar')}
        >
          {sidebarOpen ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
        </button>
      )}
      <AnimatePresence>
        {flyoutEligible && flyout.open && (
          <SessionFlyout
            key="session-flyout"
            ref={flyoutSurfaceRef}
            slots={filteredSlots}
            activeSlot={activeSlot}
            unreadSlots={surfaceUnreadSlots}
            panelWidth={sidebarWidth}
            // The panel's own height (OverlayDrawer carries pb-2), so the
            // flyout can never be taller than the thing it grows into.
            maxHeight={Math.max(0, containerH - 8)}
            connected={connected}
            creating={creatingSlot}
            autoFocus={flyout.openedBy === 'keyboard'}
            onSwitch={flyoutSwitch}
            onNew={flyoutNew}
            onExpand={() => expandSidebar(true)}
            onDismiss={() => { flyout.close(); flyoutTriggerRef.current?.focus() }}
            onMouseEnter={flyout.surfaceProps.onMouseEnter}
            onMouseLeave={flyout.surfaceProps.onMouseLeave}
            onBlur={flyout.surfaceProps.onBlur}
          />
        )}
      </AnimatePresence>
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
      <OverlayDrawer open={sidebarOpen} width={isMobile ? window.innerWidth : sidebarWidth} dragging={sidebarDragging} morph={!isMobile} morphTarget={TOGGLE_RECT} expandFrom={expandFrom} contentH={Math.max(0, containerH - 8)} className={isMobile ? 'mobile-sessions-overlay fixed top-[42px] bottom-0 left-0 z-50 bg-bg-elevated !py-0 rounded-r-xl shadow-lg max-w-[calc(100vw-2.5rem)] [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0' : ''}>
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
          onSelectSlot={() => setSplitMode(false)}
          onOpenSource={revealSourceLink}
          // Only offer the pane as a drop target when a composer exists to show
          // the chip — see canStageSessionRef for why this is a named predicate.
          chatDropTarget={canStageSessionRef ? chatPaneEl : null}
          onDropSessionRef={stageSessionRef}
        />
      </OverlayDrawer>
      )}

      {/* Per-slot tag picker — a single connected popover, opened from any session
          menu (sidebar row or header) via the ChatPage-scoped TagPopover context. */}
      <SlotTagPopover />

      {/* Chat pane */}
      {embedMode !== 'sessions' && (
      <div ref={setChatPaneEl} className={`relative flex flex-col bg-bg min-w-0 min-h-0 h-full overflow-hidden ${(activityOpen && !activitySlot) || search.isOpen ? 'flex-[1_1_60%]' : 'flex-1'}`} style={{ transition: 'flex 0.2s', ...(!sidebarOpen && !isMobile ? { marginLeft: '-0.5rem' } : {}), '--mc-content-width': CONTENT_WIDTH[chatConfig.contentWidth].messages, '--mc-input-width': CONTENT_WIDTH[chatConfig.contentWidth].input } as React.CSSProperties}>
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
        {pinStatus && (
          <div role="status" className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
            <span className="text-sm text-text flex-1">{pinStatus}</span>
            <button onClick={dismissPinStatus} aria-label={i18nT('app.dismiss')} className="text-muted hover:text-text leading-none p-0.5"><X className="w-4 h-4" /></button>
          </div>
        )}
        {/* Floating sessions opener — mobile only, and only on a chat with
            nothing in it yet (a conversation gets the in-header control
            instead). Suppressed while the inline side panel is showing: it is
            `fixed` at the same top-left corner as the panel's own collapse
            button and, carrying z-10 against that button's auto z-index, paints
            OVER it — leaving no way to close a panel that covers the whole
            screen. It would also be pointing at a chat pane the panel has
            squeezed to zero width. Sessions stay reachable meanwhile via the
            left-edge swipe (useSwipeEdge above).

            Suppressed when EMBEDDED for the same reason it is suppressed
            behind the side panel: `fixed` anchors it to the VIEWPORT, not to
            the host's pane, so it lands on whatever the host put in that
            corner -- in Papyrus, on the toolbar's back button, giving two
            overlapping tap targets on the app's primary exit. A host that
            embeds one scoped conversation has no sessions list to open. */}
        {isMobile && !embedded && !sidebarOpen && !inlineSidePanelShowing && !(activeSlot && (messages.length > 0 || slotRunning)) && (
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
              {/* The row's left padding GLIDES between its open (20px) and
                  collapsed (60px, clearing the stationary toggle + divider)
                  values on the same 320ms curve as the panel — an instant
                  class flip here reads as the title jumping sideways at the
                  start of the slide. */}
              <div className={`relative pr-1.5 pt-[9px] pb-2 flex items-center gap-2 bg-bg pointer-events-none transition-[padding-left] duration-[240ms] [transition-timing-function:cubic-bezier(.32,.72,0,1)] ${!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && !sidebarOpen ? 'pl-[60px]' : isMobile ? (embedMode === 'chat' ? 'pl-4' : 'pl-3') : 'pl-5'}`}>
                {/* Divider between toggle and title — ALWAYS mounted and
                    absolute (zero width, no flex-gap participation) so it can
                    never change the row's layout; it rides the row (title
                    side) and only fades. left-[52px] = the collapsed pane's
                    view of container x 44 (button 8+28 + 8px gap). */}
                {!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && (
                  <span aria-hidden="true" className={`absolute left-[52px] top-[13px] w-px h-5 bg-border transition-opacity ${sidebarOpen ? 'opacity-0 duration-100' : 'opacity-100 duration-150 delay-[90ms]'}`} />
                )}
                {embedMode !== 'chat' && isMobile && (
                  <button className="p-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none pointer-events-auto" onClick={() => setMobileSessions(p => !p)} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
                    {effectiveMode === 'orchestrator' ? <MessageSquareDot size={16} /> : <MessageSquare size={16} />}
                  </button>
                )}
                <div className="group/header flex min-w-0 items-stretch gap-0.5 pointer-events-auto">
                <div className="flex items-center rounded-l-md rounded-r-[2px] px-1.5 py-0.5 group-hover/header:bg-bg-hover transition-colors">
                <ChatHeaderMenu
                  activeSlot={activeSlot}
                  agent={currentSlot?.agent}
                  onReveal={activeSlot && embedMode !== 'chat' ? () => {
                    // The request rides the store, not a window event: with the
                    // drawer collapsed ChatSidebar is unmounted, so an event
                    // dispatched here (before the mount that setSidebarPinned
                    // schedules commits) had no listener and was dropped —
                    // the store entry survives until the sidebar consumes it
                    // (#912). Mobile drives its own drawer state. Embed-chat
                    // never mounts a sidebar, so the item is not offered there:
                    // a stored request would outlive the view and fire on
                    // whichever sidebar mounts next.
                    sidebarAutoHidden.current = null
                    if (isMobile) setMobileSessions(true)
                    else if (!sidebarPinned) setSidebarPinned(true)
                    dispatch(requestSlotReveal(activeSlot))
                  } : undefined}
                  onRename={activeSlot ? () => { setEditingTitleSlot(activeSlot); setTitleDraft(title) } : undefined}
                  mode={effectiveMode}
                />
                </div>
              {editingTitle ? (
                <div className="flex min-w-0 flex-1 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md bg-bg-hover">
                  {currentSlot?.memory_mode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
                  {currentSlot?.memory_mode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
                  <Input className="session-header-title text-sm font-semibold text-muted font-body bg-transparent border-0 rounded-none p-0 m-0 min-w-0 flex-1 outline-none md:max-w-[50vw] focus:!shadow-none" size={Math.min(Math.max(titleDraft.length + 2, 6), 80)} autoFocus value={titleDraft} onChange={e => setTitleDraft(e.target.value)} {...titleIme.bindComposition<HTMLInputElement>({ onBlur: () => { if (!cancelTitleRef.current && titleDraft.trim() && activeSlot && titleDraft !== title) { dispatch(sseSlotTitle({ key: activeSlot, title: titleDraft.trim() })); api.renameSlot(activeSlot, titleDraft.trim()).catch(() => {}) } cancelTitleRef.current = false; setEditingTitleSlot(null) } })} onKeyDown={e => { if (e.key === 'Enter' && titleIme.claimEnter(e)) (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') { titleIme.reset(); cancelTitleRef.current = true; setEditingTitleSlot(null) } }} />
                </div>
              ) : (
                <div className="cursor-text flex min-w-0 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md group-hover/header:bg-bg-hover transition-colors">
                  <Clickable className="flex min-w-0 items-center gap-1" onClick={() => { if (activeSlot && generatingTitleSlots.has(activeSlot)) return; setEditingTitleSlot(activeSlot); setTitleDraft(title) }}>
                    {currentSlot?.memory_mode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
                    {currentSlot?.memory_mode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
                    <TypewriterText text={title} className="session-header-title text-sm font-semibold text-muted font-body truncate min-w-0 md:max-w-[50vw]" />
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
              <div className="ml-auto flex shrink-0 items-center gap-1.5 pointer-events-none">
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
                  className="pi-morph flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 pointer-events-auto text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                  onClick={toggleAct}
                  title={i18nT('pages.chatPage.open_activity_panel')}
                  aria-label={i18nT('pages.chatPage.open_activity_panel')}
                >
                  <PanelRightSolid size={15} />
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
                  images={pinned.images}
                  bodyBeyondPreview={pinned.bodyBeyondPreview}
                  pushUp={pinned.push}
                  bannerH={pinned.bannerH}
                  expanded={pinExpanded}
                  onToggleExpanded={() => setPinExpanded(p => !p)}
                  onJump={() => scrollToPinnedPrompt(pinned.idx)}
                  cardRef={pinCardRef}
                  onCollapsedHeight={onPinCollapsedHeight}
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
                      color_hex: old?.color_hex ?? null,
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
                      color_hex: old?.color_hex ?? null,
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
              // -1 so the bar can hand focus here on unmount without adding a tab stop.
              tabIndex={-1}
              // stable theming hook 'chat-container' — see website/docs/theming-contract.md
              className="chat-container"
              style={{
                flex: 1,
                paddingBottom: 8,
                overflowY: 'auto',
                // overflow-x must be pinned, not left to default `visible`: with
                // overflowY `auto`, CSS forces the `visible` axis to compute to
                // `auto`, so one over-wide child (a long path, a wide code block,
                // a widget) gives the whole list a draggable horizontal scrollbar
                // above the composer. The conversation never pans sideways —
                // wide children scroll within themselves.
                overflowX: 'hidden',
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
              {/* Mid-switch `slotHasMore` still describes the outgoing chat, so the cursor
                  key gates the bar to match the paging thunk's own precondition. */}
              {slotHasMore && cursorIsForActiveSlot && (
                <EarlierMessagesBar loading={loadingOlder} failed={olderFailed} onLoad={handleLoadEarlier} onFocusRelease={() => scrollerRef.current?.focus()} />
              )}
              {/* Top sentinel: drives upward window expansion via virtualizer's IO. */}
              <div ref={virt.topSentinelRef} aria-hidden style={{ height: 1 }} />
              {/* top-16 matches the h-16 header spacer above, so the pinned spinner
                  clears the overlay header instead of sitting under it.
                  overflow-anchor:none so appearing/vanishing here cannot become the
                  browser's scroll anchor and jump the list mid-fetch. */}
              {loadingOlder && (
                <div className="sticky top-16 z-[1] flex justify-center py-2" data-testid="older-messages-loading" role="status" aria-label={i18nT('pages.chatPage.loading_earlier_messages')} style={{ overflowAnchor: 'none', background: 'var(--bg)' }}>
                  <Loader size={16} className="animate-spin text-muted" />
                </div>
              )}
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
                    return <div key={turnLeadKey(it, stableMsgKey)} className={`px-4 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                      {it.kind === 'group' ? (() => {
                        const unresolvedPerms = it.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                        // Skip group entirely if it only contains unresolved permissions (handled by ApprovalBar)
                        if (it.msgs.every(m => m.role === 'permission')) return null
                        return (
                        <CollapsibleToolGroup
                          count={it.msgs.filter(m => m.role !== 'permission').length}
                          disclosureKey={`ctg-${turnLeadKey(it, stableMsgKey)}`}
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
                        >{it.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(it.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
                      })() : renderMessage(it.idx, it.msg)}
                    </div>
                  }
                  return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx}><TurnBlock turn={item} renderItem={renderTurnItem} collapseAll={chatConfig.collapseAllSteps} appToolCallIds={appToolCallIds} disclosure={turnDisclosure[vi.key]} onDisclosureChange={(next: boolean) => setTurnDisclosureFor(vi.key, next)} /></div>
                }
                return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx} className={`px-4 mx-auto w-full py-1`} style={{
                  maxWidth: 'var(--mc-content-width, 900px)',
                  // The pinned banner is styled as this row's own bubble and sits
                  // at the exact position and width the bubble had when its bottom
                  // edge reached the band's bottom, so leaving both visible is what
                  // betrays them as two containers. Hide the real one (visibility,
                  // NOT display — the virtualizer must keep measuring its height or
                  // the transcript would reflow under the reader) and the bubble
                  // appears to simply stop travelling and stick. A row is only ever
                  // hidden once it is entirely behind the band, so a tall prompt
                  // never leaves a visible hole above the response.
                  //
                  // Match by message IDENTITY (ts), not display index. `pinned.idx`
                  // is computed in a scroll rAF against `displayItemsRef`, which is
                  // refreshed in a layout effect — but a streaming append or a turn
                  // regroup can still shift the list between that read and this
                  // render, leaving `pinned.idx` pointing one row off. When it did,
                  // the WRONG row was hidden and the real pinned bubble painted
                  // alongside the banner — the "two stacked boxes" bug. The ts is
                  // stable across any index shift, so it hides the right row every
                  // frame; fall back to the index only for a message with no ts.
                  visibility: (pinned && (pinned.ts != null
                    ? (item.kind === 'single' && item.msg.ts === pinned.ts)
                    : pinned.idx === displayIdx)) ? 'hidden' : undefined,
                }}>{item.kind === 'group' ? (() => {
                const unresolvedGroupPerms = item.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                if (item.msgs.every(m => m.role === 'permission')) return null
                return (
                <CollapsibleToolGroup
                  count={item.msgs.filter(m => m.role !== 'permission').length}
                  disclosureKey={`ctg-${vi.key}`}
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
                >{item.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(item.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
              })() : renderMessage(item.idx, item.msg)}</div>
              })}
              {/* Bottom spacer — reserves the height of all items below the
                  mounted window. overflow-anchor:none (see top spacer). */}
              <div aria-hidden style={{ height: virt.offsetAfter, overflowAnchor: 'none' }} />
              {/* Bottom sentinel: drives downward window expansion when in jump mode. */}
              <div ref={virt.bottomSentinelRef} aria-hidden style={{ height: 1 }} />
              {/* Footer */}
              <ChatFooter running={slotRunning} stopping={slotStopping} state={slotState} lastRole={lastRole} streamTick={streamTick} regenerating={regenerating} stopState={currentSlot?.stop_state} />
              {activeSlot && !slotLoading && !embedded && !popout && slotSwitchTarget !== activeSlot && (
                <div className="px-4 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                  <SessionPulseSurveyCard
                    // Remount on session switch: without this, React reuses
                    // the same component instance across sessions, so an
                    // in-progress rating/feedback/email from session A would
                    // still be sitting in state when the user switches to
                    // session B and hits Submit — attributing A's answers to
                    // B's sessionId prop, which had already updated.
                    //
                    // Gated on !slotLoading: the card captures its baseline
                    // turn count on FIRST MOUNT (see the component's own
                    // comment), so mounting before history finishes loading
                    // would baseline at 0 and then count every loaded
                    // historical turn as "live" once the fetch resolves —
                    // reintroducing the exact reopened-session bug the
                    // baseline exists to prevent, just via a race instead of
                    // a missing check.
                    key={activeSlot}
                    sessionId={activeSlot}
                    kiroCrewVersion={kiroCrewVersion}
                    turnCount={assistantTurnCount}
                    slotOrigin={currentSlot?.origin}
                    onLayoutChange={handleSurveyLayoutChange}
                  />
                </div>
              )}
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
              {/* De-duplicate ONLY against the matching sidebar tab (#728): each
                  bar is redundant when the activity sidebar is actually SHOWING
                  its own view (Subagents / Workflows), but on any OTHER tab
                  (Files, Changes, Logs, Artifacts) hiding it would lose the live
                  roster entirely. The condition mirrors the SidePanel's own
                  render guard (`activityOpen && !search.isOpen`) — so opening the
                  find pane, which UNMOUNTS the panel, re-shows the bar — and
                  reads the live panel tab (`tabsCtl`), NOT the Redux
                  `activityTab`, which only tracks programmatic openActivityToTab
                  calls and goes stale when the user clicks a tab in the panel. */}
              {!(activityOpen && !search.isOpen && tabsCtl.tabs.find(t => t.id === tabsCtl.activeId)?.kind === 'subagents') && <SubagentProgressBar slot={activeSlot} />}
              {!(activityOpen && !search.isOpen && tabsCtl.tabs.find(t => t.id === tabsCtl.activeId)?.kind === 'workflows') && <WorkflowProgressBar slot={activeSlot} />}
              <SubagentDeliveryProgress count={systemDeliveryCount} />
              <QueueStack messages={queuedMessages} onCancel={handleCancelQueued} onInterrupt={handleInterruptQueued} onEdit={handleEditQueued} onReorder={handleReorderQueued} fuseBelow={followUpOptions.length === 0 && !knowledgeFetch.pendingKnowledge} />
              {flyingQuote && <FlyingQuote text={flyingQuote.text} from={flyingQuote.from} targetRef={inputAreaRef} onComplete={() => setFlyingQuote(null)} />}
              <div ref={inputAreaRef} className="relative z-10">
              {/* The refused-press answer sits directly above the composer,
                  adjacent to the message-footer controls that raised it, so the
                  press cannot fail silently. Shares the chat column's own
                  container recipe (the page gutter + the theme content width)
                  rather than capping itself: a narrower centred box reads as
                  belonging to neither the transcript above nor the input below.

                  The title names the refused action. Without it the notice
                  reads as a generic error rather than "this is the answer to
                  the button you just pressed" — a first-time reader then
                  concludes the click did nothing and presses again. */}
              {refusedPress && (
                <div
                  className="px-4 mb-1.5 mx-auto w-full"
                  style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
                  data-testid="refused-press-error"
                >
                  <ErrorNotice
                    title={i18nT(REFUSED_PRESS_TITLE_KEYS[refusedPress.action])}
                    message={refusedPress.message}
                    onDismiss={() => setRefusedPress(null)}
                  />
                </div>
              )}
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
                          {s.created && <div className="text-[11px] text-muted font-mono mt-0.5">{fmtDateFields(s.created, { year: 'numeric', month: 'short', day: 'numeric' })}</div>}
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
                <div className="px-4 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
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
                      // and send in one click.
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
                <div className="px-4 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
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
                   tipSuppressed). ChatInput renders this slot LAST in the
                   above-composer stack, so the card stays flush against the
                   input box and an options row sits above it. */
                <AnimatePresence>
                  {folderSuggestion && activeSlot ? (
                    <div className="pt-1.5" key="folder-suggestion">
                      <FolderSuggestionCard
                        folderName={folderSuggestion.folderName}
                        breadcrumb={folderSuggestion.breadcrumb}
                        onAccept={folderSuggestionAccept}
                        onDecline={folderSuggestionDecline}
                      />
                    </div>
                  ) : activeTip && (
                    <div className="pt-1.5" key="tip">
                      <TipCard tip={activeTip} onDismiss={dismissTip} />
                    </div>
                  )}
                </AnimatePresence>
              }
              value={input}
              onChange={setInput}
              onSend={() => send()}
              canSteer={composerBusy}
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
              pendingDirs={pendingDirs}
              resizedInfo={resizedInfo}
              onRemoveFile={p => {
                setPendingFiles(prev => prev.filter(x => x !== p))
                // A picker-picked file also inserted an `@rel` token into the
                // composer, so its remove strips that token too — the same
                // contract folder chips have, so the two chip kinds cannot
                // disagree about what "remove" means. The exact token is
                // recorded at pick time, but the ref is in-memory only: a
                // restored draft or a failed-send restore re-stages the file
                // without it. Fall back to deriving the token from the path —
                // the shortest boundary-checked `@suffix` present in the text
                // (the same walk buildRelMap uses), which is exactly the form
                // the picker inserts. Uploaded/dropped files have no token in
                // the text, so the derivation finds nothing and their remove
                // stays state-only. On no match the text is left alone —
                // visible and editable is the safe fallback.
                const token = pickedFileTokens.current[p] ?? [...buildRelMap([p], inputRef.current).keys()].map(s => `@${s}`)[0]
                delete pickedFileTokens.current[p]
                if (!token) return
                const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                setInput(prev => prev.replace(new RegExp(`(^|\\s)${esc}(?: |(?=\\s)|$)`, 'g'), '$1'))
              }}
              onRemoveDir={rel => {
                // The chip derives from the `@rel/` token, so removing the
                // reference IS removing the token. Boundary-checked so
                // "@src/pages/" never eats a longer "@src/pages/sub/" token.
                const esc = `@${rel}`.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                setInput(prev => prev.replace(new RegExp(`(^|\\s)${esc}(?: |(?=\\s)|$)`, 'g'), '$1'))
              }}
              pendingSessions={pendingSessions}
              onRemoveSessionRef={unstageSessionRef}
              // A folder pick is complete once ChatInput inserts its `@rel/`
              // token — the chip derives from the text, so there is no state
              // to stage here. Files stay list-backed (uploads have no token)
              // and additionally record their inserted token for remove.
              onFileSelect={(path, kind, token) => {
                if (kind === 'dir') return
                if (token) pickedFileTokens.current[path] = token
                setPendingFiles(prev => prev.includes(path) ? prev : [...prev, path])
              }}
              onFileOpen={handleFileOpen}
              project={currentSlot?.project || ''}
              projectBranch={projectBranch}
              projectDetached={!projectGitError && !!projectGit?.detached}
              isMac={isMac}
              onDrop={handleDrop}
              dragOver={dragOver}
              onDragOver={e => { e.preventDefault(); e.stopPropagation(); setDragOver(true) }}
              onDragLeave={e => { if (e.currentTarget === e.target) setDragOver(false) }}
              voiceRecording={voiceOwned && voice.recording}
              voiceTranscribing={voiceOwned && voice.transcribing}
              voiceError={voice.error}
              voiceLevel={voiceOwned ? voice.level : 0}
              voiceDeviceLabel={voiceOwned ? voice.deviceLabel : ''}
              voiceDeviceId={voiceOwned ? voice.deviceId : ''}
              onSelectVoiceDevice={voice.switchDevice}
              voiceDeviceSwitchIsLive={voiceOwned && voice.deviceSwitchIsLive}
              onClearVoiceError={voice.clearError}
              voiceDictationPanel={sttDictationPanel}
              voiceStreaming={voice.streamEnabled}
              voiceSampleRef={voice.sampleRef}
              voicePartial={voiceOwned ? voice.partial : ''}
              voiceCaretRef={voiceCaretRef}
              voicePendingCaretRef={voicePendingCaretRef}
              onVoiceToggle={voiceInputSupported ? toggleVoice : undefined}
              onVoiceCancel={voiceInputSupported ? cancelVoice : undefined}
              onVoicePrewarm={voiceInputSupported ? voice.prewarm : undefined}
              agentName={currentSlot?.agent || 'default'}
              agentSource={installedAgents.find(a => a.name === (currentSlot?.agent || 'default'))?.source}
              modelName={shownModel}
              onAgentClick={provider.capabilities.agentTemplates ? (rect) => { setAgentBtnRect(rect); setAgentDropdown(!agentDropdown) } : undefined}
              onModelClick={(rect) => { setModelBtnRect(rect); setModelDropdown(!modelDropdown) }}
              onProjectClick={(rect) => {
                setProjectBtnRect(rect)
                setProjectPickerOpen(o => !o)
              }}
              contextPct={contextPct}
              contextUsedTokens={contextTokens?.used}
              contextWindowTokens={contextTokens?.window || provider.getContextWindow(shownModel)}
              showContextPct={chatConfig.showContextPct}
              showContextTokens={chatConfig.showContextTokens}
              isRunning={composerBusy}
              /* Composed with `interrupted`, matching the ErrorCard gate above.
                 Availability alone would put a filled primary button on the
                 composer of every idle chat that holds a conversation — an
                 accent-filled control reads as "this is your next move", so on
                 a slot that finished cleanly it advertises pending work that
                 does not exist and the only thing distinguishing it from Send
                 is a hover tooltip. `interrupted` is not merely the wording
                 now: it is the reason the control exists at all. When nothing
                 proves an interruption the composer falls back to the ordinary
                 Send button, disabled while empty, like every other chat.

                 The cost is a turn that died leaving no evidence — a hard kill
                 after a mid-turn assistant segment already flushed, which is
                 the one shape `_is_interrupted` cannot see. That slot loses its
                 one-click nudge; typing anything still resumes it. Closing that
                 hole needs a persisted turn-in-flight marker (backend), not a
                 louder button here. */
              continuable={continuable && interrupted}
              continueIsRecovery={interrupted}
              onContinue={handleContinue}
              continuing={continuing}
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
              onReasoningEffortClick={provider.capabilities.reasoningEffort && modelSupportsEffort(shownModel === 'auto' ? '' : shownModel) ? (rect) => { setReasoningEffortBtnRect(rect); setReasoningEffortDropdown(!reasoningEffortDropdown) } : undefined}
              onAutoNudgeClick={setAutoNudgeOpen}
              autoNudgeLoop={autoNudgeLoop}
              autoNudgeOpen={autoNudgeOpen}
              onAutoNudgeChange={setAutoNudgeLoop}
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
                if (tryQuickSend(o, dashCfg?.quick_send, e.shiftKey, slotRunning, followUpPickedRef.current.size, send)) return
                // Regular options: toggle. Click unpicked → append + mark; click
                // picked → try to remove text + unmark (if the user edited the
                // text so it no longer matches, leave text alone — the chip
                // still un-highlights for consistency).
                if (followUpPickedRef.current.has(o)) {
                  const next = new Set(followUpPickedRef.current); next.delete(o)
                  followUpPickedRef.current = next
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
                  setFollowUpPicked(next)
                } else {
                  const next = new Set(followUpPickedRef.current); next.add(o)
                  followUpPickedRef.current = next
                  setInput(prev => prev.trim() ? prev.trimEnd() + ', ' + o : o)
                  setFollowUpPicked(next)
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
              reason={voiceDisabledReason}
              provider={sttProvider}
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
                  <Input ref={agentInputRef} type="text" aria-label={i18nT('pages.chatPage.filter_agents')} placeholder={i18nT('pages.chatPage.type_to_filter')} value={agentFilter} onChange={e => setAgentFilter(e.target.value)} className="w-full px-2 py-1 text-[13px]" />
                </div>
                <div role="listbox" aria-label={i18nT('pages.chatPage.agent_list')} className="overflow-y-auto max-h-[280px]">
                <AgentDropdownList agents={filteredAgents} activeAgent={activeAgentName} defaultAgent={defaultAgent} onSelect={(name) => { switchAgent(name); setAgentDropdown(false) }} filter={agentFilter} />
                </div>
                {/* Embedded chat gets neither half of the default-agent affordance: it has
                    no /capabilities route for the footer, and the footer is what carries the
                    failed-write alert — offering the write without its error path would make
                    a rejected request indistinguishable from a successful one. */}
                {!embedded && <DefaultAgentRow agentName={activeAgentName} isDefault={activeAgentName === defaultAgent} onSetDefault={() => toggleDefaultAgent(activeAgentName)} />}
                {!embedded && <ManageAgentsFooter error={defaultAgentFailed} onManage={() => { setAgentDropdown(false); navigate('/capabilities?tab=templates') }} />}
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
                activeModel={shownModel}
                onSelectModel={name => switchModel(name)}
                filter={modelFilter}
                setFilter={setModelFilter}
                onClose={() => setModelDropdown(false)}
                hasEffort={!!(activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(shownModel === 'auto' ? '' : shownModel))}
                slot={activeSlot}
                currentEffort={currentSlot?.reasoning_effort || ''}
                defaultEffort={defaultEffort}
                onSetDefault={() => {
                  setModelDropdown(false)
                  navigate(`/settings?tab=chat&highlight=${SETTINGS_DEFAULT_MODEL_ID}`)
                }}
                agentName={_modelPinAgent}
                pinModelName={_modelPinActive || 'auto'}
                pinModelUnavailable={pinIsWithheld(_modelPinActive, shownModel)}
                pinnedToAgent={_modelPinPinned}
                onPinToAgent={() => {
                  setModelDropdown(false)
                  pinModelToAgentMut.mutate({
                    agent: _modelPinAgent,
                    // The slot's REAL model, never the display fallback: a
                    // stale/degraded list must not be able to persist 'auto'
                    // over a pin the account actually has.
                    model: _modelPinActive === 'auto' ? '' : _modelPinActive,
                  })
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
            {reasoningEffortDropdown && reasoningEffortBtnRect && activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(shownModel === 'auto' ? '' : shownModel) && createPortal(
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
        {shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) && !activitySlot && (
          <motion.div
            key="side-panel-inline"
            initial={{ width: 0 }}
            animate={{ width: 'auto' }}
            exit={{ width: 0 }}
            transition={{ duration: 0.4, ease: [0.32, 0.72, 0, 1] }}
            className="h-full overflow-hidden flex justify-end shrink-0"
            // Kept mounted for a live app tab: hide instead of unmounting so the
            // iframe (and the drawing inside it) survives a panel close.
            style={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) ? { display: 'none' } : undefined}
          >
            <SidePanel
              tabsCtl={tabsCtl}
              slot={activeSlot || ''}
              onFileOpen={handleFileOpen}
              onArtifactOpen={handleArtifactOpen}
              projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
              sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
              onSubmitComments={submitComments} onFileSave={handleFileSave} onClose={toggleAct}
              pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPinnedMessage} onUnpin={handleUnpinById}
              slotTitle={activeSlotTitle} chatMode={mode}
              expanded={panelMaximized}
              fillWidth={panelFillWidth}
              canDockBottom={false}
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
          {shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) && (
            <motion.div
              key="side-panel"
              initial={sidePanelDockAnim.initial}
              animate={sidePanelDockAnim.animate}
              exit={sidePanelDockAnim.exit}
              transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
              className={sidePanelDock === 'bottom' ? 'w-full overflow-visible flex flex-col justify-end' : 'h-full overflow-visible flex justify-end'}
              style={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) ? { display: 'none' } : undefined}
            >
              <SidePanel
                tabsCtl={tabsCtl}
                slot={activeSlot || ''}
                onFileOpen={handleFileOpen}
                onArtifactOpen={handleArtifactOpen}
                projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
                sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
                onSubmitComments={submitComments} onFileSave={handleFileSave} onClose={toggleAct}
                pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPinnedMessage} onUnpin={handleUnpinById}
                slotTitle={activeSlotTitle} chatMode={mode}
                expanded={panelMaximized}
                fillWidth={panelFillWidth}
              />
            </motion.div>
          )}
        </AnimatePresence>,
        activitySlot
      )}
    </div>
    </JiraHostsCtx.Provider>
    </TagPopoverProvider>
    </RowDisclosureProvider>
  )
}

