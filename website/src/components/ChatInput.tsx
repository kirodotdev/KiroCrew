import { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo, memo } from 'react'
import { ArrowUpFromLine, ArrowUp, Loader2, RotateCw, Plus, Crop, Bot, Mic, Square, BookOpen, X, ClipboardList, CheckCircle, Ban, Sparkles, Target, Lock, FolderOpen, FileText, ChevronDown, Check } from 'lucide-react'
import CopyBranchButton from './CopyBranchButton'
import { usePointerDrag } from '../hooks/usePointerDrag'
import VoiceStatusBar from './VoiceStatusBar'
import VoiceDictationPanel, { useDictationPanelUsable } from './VoiceDictationPanel'
import type { AudioSample } from '../hooks/mic'
import { createPortal } from 'react-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useBranding } from '../hooks/useBranding'
import { useAppSelector, useAppDispatch } from '../store'
import { resolveByApprovalId, openActivityToTool, openActivityToTab, selectSlotPendingApproval, selectSlotPendingSpawnApprovals, markSubagentApproving, sseSubagentDone } from '../store/chatSlice'
import { useSlotId } from '../providers/SlotContext'
import { useToolPillVisible } from '../store/toolPillRegistry'
import { ToolDetails } from '../pages/chat/ToolDetails'
import { api, ApiError } from '../api/client'
import { safeSetItem } from '../utils/safeStorage'
import { offlineProps } from '../utils/offline'
import { shallowEqual } from 'react-redux'
import { motion, AnimatePresence } from 'framer-motion'
import { sanitizeLlmOutput } from '../utils/sanitize'
import { useSimplifiedToolNames } from '../hooks/useSimplifiedToolNames'
import { useLanguage } from '../i18n/LanguageProvider'
import { pickToolLabel } from '../utils/toolLabel'
import TrustDropdown from './TrustDropdown'
import AutoNudgePopover, { type AutoNudgeLoop } from './AutoNudgePopover'
import { useIsMobile } from '../hooks/useIsMobile'
import { isTouchDevice } from '../utils/isTouchDevice'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { isScreenSnipSupported } from '../hooks/useScreenSnip'
import { useImeGuard } from '../hooks/useImeGuard'
import ContextBar, { contextTip, contextPctClamped, contextColor } from './ContextBar'
import PasteHighlightLayer, { INPUT_TYPO } from './PasteHighlightLayer'
import FollowUpBar from './FollowUpBar'
import { dispatchLightbox } from './MarkdownRenderer'
import { IMG_EXT } from '../utils/fileTokens'
import type { ResizeInfo } from '../utils/resizeImage'
import type { SubagentActivity } from '../types'
import { platformShortcut } from '../utils/platform'
import {
  type PasteBlock,
  shouldCollapse as shouldCollapsePaste,
  countLines,
  makePasteId,
  formatToken,
  tokenRangeAt,
  pruneBlocks,
  nextSeq,
  findTokenRanges,
} from '../utils/pasteTokens'
import type { SendMode } from '../pages/chat/ChatSettings'

// Upload picker accept hints. Client-side ONLY (UX) — the server validates type
// (magic bytes), size, and runs malware scanning per input-validation guidance.
const IMAGE_ACCEPT = 'image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml'
const FILE_ACCEPT = IMAGE_ACCEPT + ',.txt,.md,.json,.yaml,.yml,.xml,.csv,.log,.py,.js,.ts,.tsx,.jsx,.html,.css,.sh,.bash,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf,.zip,.tar,.gz'

import ApprovalModePicker from './ApprovalModePicker'
// Effort vocabulary lives in lib/effort.ts (mirrors backend effort.py).
// Re-exported here for back-compat with existing `from './ChatInput'` imports.
export {
  EFFORT_LABEL_KEY,
  EFFORT_LEVELS,
  REASONING_EFFORT_PROVIDERS,
  modelSupportsEffort,
  effortLabel,
} from '../lib/effort'
// Re-export above does not create a local binding — import effortLabel for use
// in this component's own render below.
import { effortLabel } from '../lib/effort'
import SlashCommandMenu from './SlashCommandMenu'
import FilePickerMenu from './FilePickerMenu'
import SkillPickerMenu from './SkillPickerMenu'
import { matchFileToken, matchSkillToken, replaceTokenAtCaret } from './composerTokens'
import { useStopEscapeHatch } from '../hooks/useStopEscapeHatch'

import { i18nT } from '../i18n/t'
import { fmtDateFields } from '../i18n/format'
const INPUT_MIN_H = 44
const INPUT_DEFAULT_MAX_H = 140
const INPUT_PREFILL_MAX_H = 320
const INPUT_DRAG_MIN_H = 93
const FILE_PREVIEW_H = 81 // h-16 (64px) + py-2 (16px) + border-t (1px)
const INPUT_DRAG_MAX_RATIO = 0.5
const INPUT_HEIGHT_LS_KEY = 'mc-input-height'

// Send behavior while a turn is RUNNING. 'steer' (default) injects the
// composer into the running turn; 'queue' defers it to the next turn. The
// user picks via the split send button's dropdown; choice persists.
const BUSY_SEND_MODE_LS_KEY = 'mc-busy-send-mode'
type BusySendMode = 'steer' | 'queue'
/**
 * Catalog KEYS for the two busy-send modes' menu copy.
 *
 * Keys, not strings: `BUSY_SEND_MODES` is built at module load, so an `i18nT()`
 * call in it would freeze whatever language was active at boot and never
 * re-resolve on a language switch. The lookups happen in the menu's render.
 *
 * Held apart from `BUSY_SEND_MODES` and shaped as flat `Record`s of full literal
 * keys, indexed inline at the `i18nT()` call, because that is the only form
 * `scripts/check-i18n-keys.mjs` can resolve statically — nested in the array and
 * read as `i18nT(m.labelKey)` the gate cannot see the key at all.
 *
 * `steer` reuses the label the split button's `aria-label` already ships rather
 * than sending a duplicate English string to ten locales.
 */
const BUSY_SEND_MODE_LABEL_KEY: Record<BusySendMode, string> = {
  steer: 'components.chatInput.steer',
  queue: 'components.chatInput.queue',
}
const BUSY_SEND_MODE_DESC_KEY: Record<BusySendMode, string> = {
  steer: 'components.chatInput.steer_desc',
  queue: 'components.chatInput.queue_desc',
}
const BUSY_SEND_MODES: Array<{ mode: BusySendMode; icon: React.ReactNode }> = [
  { mode: 'steer', icon: <Target size={15} /> },
  { mode: 'queue', icon: <ArrowUpFromLine size={15} /> },
]

// Prompt undo/redo tuning. The chat textarea is a controlled component, so any
// programmatic value reset (send-clear, ↑/↓ history recall, prompt optimize)
// wipes the browser's native undo stack. We keep an explicit snapshot history
// so Ctrl/Cmd+Z can always restore prior text — including after an accidental
// full erase.
const UNDO_COALESCE_MS = 400 // merge keystrokes within this window into one undo step
const UNDO_BULK_DELTA = 8 // an insert/delete of >= this many chars is its own boundary
const UNDO_MAX_HISTORY = 200 // cap snapshots to bound memory

// `blocks` rides with each snapshot so undo/redo restores the paste content
// backing any `[ Paste #N ]` token in `value` — deleting or expanding a token
// drops its PasteBlock, and without this an undo would resurrect the token text
// as a dead literal with no recoverable content.
type UndoSnap = { value: string; selStart: number; selEnd: number; blocks: PasteBlock[] }

/** True when two block lists hold the same blocks by id (order-independent).
 *  Lets undo/redo skip a redundant onPasteBlocksChange when the paste set is
 *  unchanged (e.g. plain-text undo, where both sides are empty). */
function sameBlocks(a: PasteBlock[], b: PasteBlock[]): boolean {
  if (a === b) return true
  if (a.length !== b.length) return false
  const ids = new Set(a.map(x => x.id))
  return b.every(x => ids.has(x.id))
}

function toApiDecision(d: string): 'approve' | 'reject' {
  return (d === 'approved' || d === 'trust' || d === 'trust_reads') ? 'approve' : 'reject'
}

/** Approval sources that run unattended, with no human bound to the chat the
 *  card renders in. Session-scoped Trust is meaningless for these (see
 *  `approvalIsUnattended`), so the Trust controls are withheld and only
 *  Allow once / Reject are offered. Kept in sync with the backend's
 *  `_BACKGROUND_APPROVAL_SOURCES` minus `autonudge`, which does run in-session. */
export const UNATTENDED_APPROVAL_SOURCES = new Set(['cron', 'heartbeat', 'taskrunner'])

// Pending-approval selection is slot-aware — see selectSlotPendingApproval
// in chatSlice: each grid pane's approval bar reflects ITS slot.

/** Usable viewport height. Native window zoom already reports zoomed CSS
 *  pixels through innerHeight, so no compensation var is needed. */
function effectiveVh(): number {
  return window.innerHeight
}

/** Remove a trailing run of blank lines from pasted text: strips trailing
 *  spaces/tabs/newlines, but ONLY when that run contains at least one newline
 *  (so a paste ending in plain spaces is left untouched); interior content is
 *  never modified. A single linear backward scan over the trailing whitespace
 *  run — no regex backtracking, so it stays linear even on adversarial input
 *  (e.g. a huge run of spaces followed by a non-whitespace character). */
function stripTrailingBlankLines(s: string): string {
  let i = s.length - 1
  let sawNewline = false
  while (i >= 0) {
    const c = s.charCodeAt(i)
    if (c === 10 /* \n */ || c === 13 /* \r */) { sawNewline = true; i--; continue }
    if (c === 32 /* space */ || c === 9 /* \t */) { i--; continue }
    break
  }
  return sawNewline ? s.slice(0, i + 1) : s
}

/** Auto-size textarea to fit content (only when not manually sized).
 *  Sets overflow:hidden during measurement so the parent flex container
 *  never sees the collapsed (height:0) intermediate state — prevents the
 *  Virtuoso message list above from reflowing and causing visible vibration. */
function applyHeight(
  el: HTMLTextAreaElement,
  manualHeight: number | null,
  prefillHint?: boolean,
) {
  if (manualHeight !== null) return // manual height — wrapper controls size
  const cap = prefillHint ? INPUT_PREFILL_MAX_H : INPUT_DEFAULT_MAX_H
  const prev = el.style.height
  const prevOverflow = el.style.overflow
  const prevScrollTop = el.scrollTop // height:0 below resets scroll; preserve for non-typing callers
  el.style.overflow = 'hidden'
  el.style.height = '0'
  const next = Math.max(INPUT_MIN_H, Math.min(el.scrollHeight, cap)) + 'px'
  el.style.height = next === prev ? prev : next
  el.style.overflow = prevOverflow
  el.scrollTop = prevScrollTop
  // When typing at the end of overflowing content, snap to the bottom so the caret
  // stays visible — restoring prevScrollTop loses it (the value-commit re-resets
  // scrollTop after this runs).
  const caretAtEnd = el.selectionStart === el.value.length && el.selectionEnd === el.value.length
  if (document.activeElement === el && el.scrollHeight > el.clientHeight && caretAtEnd) {
    el.scrollTop = el.scrollHeight
  }
}

interface ChatInputProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  /** Rendered inside the composer's own width wrapper, directly above the
   * bordered input box. Children here share the EXACT box geometry of the
   * composer (same padding container, same resolved max-width), so band
   * surfaces like the feature tip can never drift out of alignment the way
   * parallel sibling containers with percentage widths do. */
  aboveComposer?: React.ReactNode
  /** When true (turn is running), show the split Steer/Queue send button.
   * v1 gates on turn-running only; if the slot's backend is not steer-capable
   * (e.g. claude), the POST safely falls through to the queue server-side.
   * Plumbing a per-slot capability flag is a follow-up. */
  canSteer?: boolean
  /** Inject a mid-turn steer into the running turn. Reads the composer text
   * and pending files itself (ChatPage) and clears them atomically — ChatInput
   * must NOT clear the value around this call. */
  onSteer?: () => void
  disabled?: boolean
  placeholder?: string
  prefillHint?: boolean
  onDismissHint?: () => void
  /** macOS-only screenshot */
  onScreenshot?: () => void
  /** Browser-native file upload (cross-platform) */
  onUploadFiles?: (files: File[]) => void
  /** Whether file actions are in progress */
  uploading?: boolean
  /** Pending file paths (images + non-images) for preview strip */
  pendingFiles?: string[]
  /** Resize details keyed by pending-file path; renders a badge on the chip */
  resizedInfo?: Record<string, ResizeInfo>
  /** Remove a pending file by path */
  onRemoveFile?: (path: string) => void
  /** Show macOS-only buttons (screenshot) */
  isMac?: boolean
  /** Drag-and-drop handler for the entire input bar */
  onDrop?: (e: React.DragEvent) => void
  /** Whether drag-over styling is active */
  dragOver?: boolean
  /** Drag-over event handler */
  onDragOver?: (e: React.DragEvent) => void
  /** Drag-leave event handler */
  onDragLeave?: (e: React.DragEvent) => void
  /** Voice input state */
  voiceRecording?: boolean
  /** Change the voice capture device from the in-chat picker. */
  onSelectVoiceDevice?: (deviceId: string) => void
  /** True when a device switch applies to the live capture, not the next one. */
  voiceDeviceSwitchIsLive?: boolean
  voiceTranscribing?: boolean
  onVoiceToggle?: () => void
  /** Cancel (discard) an in-progress dictation without transcribing — Esc. */
  onVoiceCancel?: () => void
  /** Pre-warm the mic on pointer-down so recording starts instantly on click. */
  onVoicePrewarm?: () => void
  /** Mic error (null = none), live input level [0,1], active device label, and error-dismiss. */
  voiceError?: string | null
  voiceLevel?: number
  voiceDeviceLabel?: string
  onClearVoiceError?: () => void
  /** Show the animated dictation panel while recording (stt.dictation_panel). */
  voiceDictationPanel?: boolean
  /** True for streaming STT — the dictation panel's hint says "Enter to send"
   *  (live transcript in composer); batch says "click the mic to finish". */
  voiceStreaming?: boolean
  /** Per-frame audio features driving the dictation panel's shader. */
  voiceSampleRef?: { current: AudioSample }
  /** Latest partial hypothesis, rendered muted in the dictation panel. */
  voicePartial?: string
  /** Live composer caret, updated by ChatInput so ChatPage's dictation handler
   *  can splice the transcript in at the cursor instead of appending. */
  voiceCaretRef?: React.MutableRefObject<{ start: number; end: number } | null>
  /** Caret offset to restore after a dictation-driven value update lands. */
  voicePendingCaretRef?: React.MutableRefObject<number | null>
  /** Chat-level controls in input bar */
  agentName?: string
  agentSource?: string
  modelName?: string
  onAgentClick?: (rect: DOMRect) => void
  onModelClick?: (rect: DOMRect) => void
  onProjectClick?: (rect: DOMRect) => void
  contextPct?: number
  contextUsedTokens?: number
  contextWindowTokens?: number
  showContextPct?: boolean
  isRunning?: boolean
  onStop?: () => void
  /**
   * True when an EMPTY composer can hand the thread back to the agent, so the
   * dead send button becomes a Continue control instead. Offered on any idle
   * slot with a conversation — a force-quit leaves no trace of the turn it
   * killed, so restricting this to visibly-broken transcripts would miss exactly
   * the case that needs it most.
   */
  continuable?: boolean
  /**
   * True when the transcript SHOWS the last turn ending badly (unanswered user
   * row, or a trailing error). Picks between "the last turn was interrupted" and
   * the neutral "keep going" wording, so the button never asserts a breakage it
   * cannot see. NOT copy-only any more: `ChatPage` composes this into the
   * `continuable` it passes, so on the dashboard it also decides whether the
   * control appears at all. A caller may still pass `continuable` alone — the
   * component keeps working, it just gets the neutral wording.
   */
  continueIsRecovery?: boolean
  onContinue?: () => void
  /** True while a continue request is in flight. */
  continuing?: boolean
  isQueued?: boolean
  stopState?: 'idle' | 'soft_pending' | 'killing'
  approvalMode?: string
  reasoningEffort?: string
  onReasoningEffortClick?: (rect: DOMRect) => void
  providerId?: string
  onFileSelect?: (path: string) => void
  onFileOpen?: (path: string) => void
  project?: string
  /** Checked-out branch of the active project (or short SHA when detached). */
  projectBranch?: string
  /** True when the project's HEAD is detached, so the label is a commit. */
  projectDetached?: boolean
  memoryMode?: string
  cleanMode?: boolean
  /** User-sent messages for ↑/↓ history navigation (oldest → newest). */
  sentMessages?: string[]
  /** Auto-nudge loop state for this slot (if any) */
  onAutoNudgeClick?: (open: boolean) => void
  autoNudgeLoop?: AutoNudgeLoop | null
  autoNudgeOpen?: boolean
  onAutoNudgeChange?: (loop: AutoNudgeLoop | null) => void
  /** Send-key mode. Default 'enter'. */
  sendOnEnter?: SendMode
  /** Follow-up options from assistant message */
  followUpOptions?: string[]
  /** Options the user has picked (visual highlight in FollowUpBar) */
  followUpPicked?: Set<string>
  /** Select a follow-up option — handler toggles text in input (see ChatPage wiring) */
  onFollowUpSelect?: (option: string, event: React.MouseEvent) => void
  /** Double-click a follow-up option — send with option text directly (bypasses setInput race) */
  onFollowUpSend?: (text?: string) => void
  /** Quick Send enabled — clicking sends immediately */
  quickSend?: boolean
  /** Layout mode for the follow-up bar: 'multiline' (default) or 'scroll' (original single-line). */
  followUpLayout?: 'multiline' | 'scroll'
  /** Collapsed paste blocks backing `⌜🗒 Pasted …⌟` tokens in `value`. */
  pasteBlocks?: PasteBlock[]
  /** Replace the current list of paste blocks (add/remove). */
  onPasteBlocksChange?: (next: PasteBlock[]) => void
  /** Optional knowledge chip rendered above the input */
  knowledgeChip?: React.ReactNode
  /** When this key changes, focus the textarea (e.g. on chat session switch). */
  autoFocusKey?: string | null
  /** Gateway WebSocket connection state. When false, send is blocked and a
   *  warning banner appears above the input. Defaults to true so callers that
   *  don't track connectivity (e.g. tests, embedded previews) keep working. */
  connected?: boolean
  /** Deliver an optimize result to the session that initiated it when that
   *  session is no longer the one displayed in this ChatInput (the user
   *  navigated away mid-optimize). The parent routes `optimized` into
   *  `slotId`'s draft so the result is never written to the wrong session and
   *  never silently lost. When the originating session is still on screen,
   *  ChatInput writes the result itself (undoable) and does NOT call this. */
  onOptimizeResult?: (slotId: string | null, optimized: string) => void
}

/** Accent pill on a downscaled attachment chip. Hover (or focus) shows a
 *  styled tooltip with the resize details, portal-rendered above the chip so
 *  the strip's overflow-x-auto can't clip it. */
function ResizeBadge({ resize }: { resize: ResizeInfo }) {
  const [tip, setTip] = useState<{ top: number; left: number } | null>(null)
  const ref = useRef<HTMLSpanElement>(null)
  const show = () => {
    const r = ref.current?.getBoundingClientRect()
    if (r) setTip({ top: r.top - 8, left: r.left })
  }
  const hide = () => setTip(null)
  return (
    <>
      <span
        ref={ref}
        tabIndex={0}
        aria-label={i18nT('components.chatInput.resized_to_fit_model_limits_2', { fromW: resize.fromW, fromH: resize.fromH, toW: resize.toW, toH: resize.toH })}
        className="absolute bottom-1 left-1 z-10 px-1.5 py-[1px] rounded-full text-[10px] font-bold bg-accent text-accent-fg shadow-sm cursor-default"
        onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}
      >{i18nT('components.chatInput.resized')}</span>
      {tip && createPortal(
        <div
          role="tooltip"
          className="fixed z-[9999] -translate-y-full rounded-lg border border-border-strong bg-bg-elevated px-2.5 py-1.5 text-[11px] leading-snug shadow-lg pointer-events-none whitespace-nowrap"
          style={{ top: tip.top, left: tip.left }}
        >
          <div className="text-text">{i18nT('components.chatInput.resized_to_fit_model_limits')}</div>
          <div className="text-muted">{resize.fromW}×{resize.fromH} → {resize.toW}×{resize.toH}</div>
        </div>,
        document.body,
      )}
    </>
  )
}

function FilePreviewStrip({ files, resizedInfo, onRemove }: { files: string[]; resizedInfo?: Record<string, ResizeInfo>; onRemove?: (path: string) => void }) {
  const imgs = files.filter(p => IMG_EXT.test(p))
  const nonImgs = files.filter(p => !IMG_EXT.test(p))
  if (!imgs.length && !nonImgs.length) return null
  return (
    // NOTE: rendered height must match FILE_PREVIEW_H constant, update both together
    <div className="flex gap-2 px-5 py-2 border-t border-border bg-chrome/50 overflow-x-auto items-end" data-image-scope="">
      {imgs.map((path, i) => {
        const src = `/api/file-raw?path=${encodeURIComponent(path)}`
        const resize = resizedInfo?.[path]
        return (
          <div key={path} className="relative group/preview shrink-0" title={path}>
            <span className="absolute -top-1.5 -left-1.5 w-5 h-5 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center z-10">{i + 1}</span>
            <button
              type="button"
              aria-label={i18nT('components.chatInput.open_preview_of', { name: path.split('/').pop() })}
              className="block cursor-pointer"
              onClick={(e) => { const img = e.currentTarget.querySelector('img'); if (img) dispatchLightbox(img) }}
            >
              <img src={src} alt={path} className="h-16 rounded border border-border object-contain hover:opacity-80 transition-opacity"
                data-lightbox-image="" />
            </button>
            {resize && <ResizeBadge resize={resize} />}
            {onRemove && (
              <button
                aria-label={i18nT('components.chatInput.remove')}
                className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-danger text-white text-[12px] flex items-center justify-center opacity-0 group-hover/preview:opacity-100 transition-opacity cursor-pointer"
                onClick={() => onRemove(path)} title={i18nT('components.chatInput.remove')}
              ><X className="lucide-inline" /></button>
            )}
          </div>
        )
      })}
      {nonImgs.map(path => (
        <div key={path} className="relative group/preview shrink-0 flex items-center gap-1.5 px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text">
          <span>{path.split('/').pop()}</span>
          {onRemove && (
            <button className="text-muted hover:text-danger cursor-pointer bg-transparent border-none p-0" onClick={() => onRemove(path)} title={i18nT('components.chatInput.remove')} aria-label={i18nT('components.chatInput.remove')}><X size={12} /></button>
          )}
        </div>
      ))}
    </div>
  )
}


/** Stable no-op so an unwired embedder does not remount the picker each render. */
const noopSelectDevice = () => {}

function ChatInput({
  aboveComposer,
  value,
  onChange,
  onSend,
  canSteer,
  onSteer,
  disabled: disabledProp = false,
  placeholder = '',
  prefillHint,
  onScreenshot,
  onUploadFiles,
  uploading = false,
  pendingFiles = [],
  resizedInfo,
  onRemoveFile,
  isMac = false,
  onDrop,
  dragOver = false,
  onDragOver,
  onDragLeave,
  voiceRecording = false,
  onSelectVoiceDevice,
  voiceDeviceSwitchIsLive = false,
  voiceTranscribing = false,
  onVoiceToggle,
  onVoiceCancel,
  onVoicePrewarm,
  voiceError = null,
  voiceLevel = 0,
  voiceDeviceLabel = '',
  voiceDictationPanel = false,
  voiceStreaming = false,
  voiceSampleRef,
  voicePartial = '',
  voiceCaretRef,
  voicePendingCaretRef,
  onClearVoiceError,
  agentName,
  agentSource,
  modelName,
  onAgentClick,
  onModelClick,
  onProjectClick,
  contextPct,
  contextUsedTokens,
  contextWindowTokens,
  showContextPct,
  isRunning = false,
  onStop,
  continuable = false,
  continueIsRecovery = false,
  onContinue,
  continuing = false,
  isQueued = false,
  stopState,
  approvalMode,
  reasoningEffort,
  onReasoningEffortClick,
  providerId: _providerId,
  onFileSelect,
  onFileOpen,
  project,
  projectBranch,
  projectDetached,
  memoryMode,
  cleanMode,
  sentMessages,
  onAutoNudgeClick,
  autoNudgeLoop,
  autoNudgeOpen,
  onAutoNudgeChange,
  sendOnEnter = 'enter',
  followUpOptions,
  followUpPicked,
  onFollowUpSelect,
  onFollowUpSend,
  quickSend,
  followUpLayout,
  pasteBlocks = [],
  onPasteBlocksChange,
  knowledgeChip,
  autoFocusKey,
  connected = true,
  onOptimizeResult,
}: ChatInputProps) {
  const disabled = disabledProp
  const dispatch = useAppDispatch()
  const slotId = useSlotId()
  const pendingApproval = useAppSelector(s => selectSlotPendingApproval(s, slotId), shallowEqual)
  const hasApproval = !!pendingApproval
  const [approvalSubmitting, setApprovalSubmitting] = useState(false)
  // Non-null while the last approval decision failed. Rendered as a one-line
  // strip under the composer; auto-clears so it cannot become permanent chrome.
  const [approvalNotice, setApprovalNotice] = useState<string | null>(null)

  const activeSlot = slotId
  const approvalMeta = pendingApproval?.meta as Record<string, unknown> | undefined
  const approvalId = approvalMeta?.approval_id as string | undefined
  const approvalToolInput = (approvalMeta?.tool_input as string) || ''
  const approvalIsReadOnly = !!(approvalMeta?.is_read_only)
  const approvalFullCommand = (approvalMeta?.full_command as string) || ''
  const approvalBaseCommand = (approvalMeta?.base_command as string) || ''
  const approvalToolTitle = (approvalMeta?.tool_title as string) || ''
  const approvalIsShell = approvalToolTitle.startsWith('Running: ')
  /** Sources that run with no human attached to THIS conversation. Session
   *  trust means "auto-approve tools for this chat session", which is
   *  incoherent for an unattended job: the job is not this session, so the
   *  grant would widen this slot's own auto-approval surface while doing
   *  nothing for the job. `autonudge` is deliberately absent — a monitor loop
   *  runs *in* this session, so trusting it is meaningful. */
  const approvalSource = (approvalMeta?.source as string)
    // Persisted permission rows are rehydrated from content alone (chatSlice's
    // reconstruct path carries no `source`), so fall back to the `[source]`
    // prefix the card was written with rather than silently treating a
    // reloaded cron card as an ordinary in-session one.
    || (pendingApproval?.content || '').match(/^(?:🔧\s*)?\[([a-z_]+)\]/)?.[1]
    || ''
  const approvalIsUnattended = UNATTENDED_APPROVAL_SOURCES.has(approvalSource)
  const simplified = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved
  const approvalLabelRaw = sanitizeLlmOutput(pendingApproval?.content || '').replace(/^🔧\s*/, '')

  const approvalToolCallId = (approvalMeta?.tool_call_id as string) || null

  const approvalToolEntry = useAppSelector(s => {
    if (!approvalToolCallId) return null
    const log = slotId && slotId !== s.chat.activeSlot ? (s.chat.slotActivity[slotId]?.toolLog ?? []) : s.chat.toolLog
    const entry = log.findLast(e => e.type === 'tool' && e.tool_call_id === approvalToolCallId)
    return entry ? { purpose: entry.purpose || '', ts: entry.ts || 0 } : null
  }, shallowEqual)
  const approvalPurpose = approvalToolEntry?.purpose || ''
  const approvalTs = approvalToolEntry?.ts || 0

  const approvalLabel = pickToolLabel({ simplified, purpose: approvalPurpose, rawLabel: approvalLabelRaw, uiLang })

  // Subscribe to the inline pill's viewport visibility. While the pill is in
  // view, the bar collapses to just the always-visible button row; the moment
  // the pill scrolls past the top, a "ghost pill" mirror slides into the bar
  // so the user keeps full context (timestamp, purpose, input preview)
  // alongside the action buttons. See src/store/toolPillRegistry.ts.
  const pillVisible = useToolPillVisible(approvalToolCallId)

  // Settle guard: when a new approval arrives, suppress the ghost for a brief
  // window so the Virtuoso list has time to mount the ToolCallLine and register
  // the pill. Without this, the ghost flashes for 1-2 frames then collapses
  // once the in-chat pill reports itself visible.
  const [ghostSettled, setGhostSettled] = useState(false)
  useEffect(() => {
    if (!approvalToolCallId) { setGhostSettled(false); return }
    setGhostSettled(false)
    const t = setTimeout(() => setGhostSettled(true), 150)
    return () => clearTimeout(t)
  }, [approvalToolCallId])

  const showGhost = !!pendingApproval && !pillVisible && ghostSettled

  // Auto-dismiss the failure notice. Bounded lifetime keeps a transient
  // backend hiccup from leaving a permanent banner over the composer.
  useEffect(() => {
    if (!approvalNotice) return
    const t = setTimeout(() => setApprovalNotice(null), 8000)
    return () => clearTimeout(t)
  }, [approvalNotice])
  const showInChat = useCallback(() => {
    if (approvalToolCallId) dispatch(openActivityToTool(approvalToolCallId))
  }, [approvalToolCallId, dispatch])

  // Stop button: killing-state escape hatch (re-enable after 15s)
  const { escaped: killingEscaped } = useStopEscapeHatch(stopState)

  const handleApprovalAction = useCallback((decision: string, pattern?: string) => {
    if (!approvalId) return
    setApprovalSubmitting(true)
    setApprovalNotice(null)
    const finish = () => {
      dispatch(resolveByApprovalId({ id: approvalId, decision }))
      setApprovalSubmitting(false)
    }
    const fail = (err: unknown) => {
      setApprovalSubmitting(false)
      // 404 means the backend no longer holds a future for this id — the turn
      // was stopped, timed out, or the process was replaced. The card is an
      // orphan: leaving it up makes every button look broken, so clear it and
      // say why instead of only logging to the console.
      if (err instanceof ApiError && err.status === 404) {
        dispatch(resolveByApprovalId({ id: approvalId, decision: 'stale' }))
        // Say WHOSE turn expired. Unattended sources deny-fast on a short
        // window (minutes), so by the time a human reads the card the job has
        // usually already been denied and moved on — "expired" alone reads as
        // a dashboard bug rather than the job's documented timeout.
        setApprovalNotice(
          approvalIsUnattended
            ? i18nT('components.chatInput.that_request_already_timed_out_and_was_denied', { source: approvalSource })
            : i18nT('components.chatInput.that_approval_expired_the_turn_it_belonged_to_is')
        )
        return
      }
      // eslint-disable-next-line no-console -- surface real approval-resolution failures to the dev console
      console.error('Approval failed:', err)
      setApprovalNotice(i18nT('components.chatInput.could_not_submit_that_decision_see_the_console_f'))
    }
    if (['trust_command', 'trust_base', 'trust', 'trust_reads'].includes(decision) && activeSlot) {
      // Defence in depth: the Trust controls are not rendered for unattended
      // sources, but never let a trust grant be applied on their behalf. The
      // grant would land on THIS slot (api.approveChatSlot is slot-scoped),
      // widening its auto-approval surface for a job that is not this session.
      // Downgrade to a one-shot allow instead of silently over-granting.
      if (approvalIsUnattended) {
        api.resolveApproval(approvalId, 'approve').then(finish).catch(fail)
        return
      }
      const extra: Record<string, string> = { request_id: approvalId }
      if (pattern) extra.pattern = pattern
      api.approveChatSlot(activeSlot, decision, extra).then(finish).catch(fail)
    } else {
      api.resolveApproval(approvalId, toApiDecision(decision)).then(finish).catch(fail)
    }
  }, [approvalId, activeSlot, approvalIsUnattended, approvalSource, dispatch])

  // Pending sub-agent SPAWN approvals for this slot (blocked on user approval).
  // Surfaced as a top-level banner with inline Approve/Reject so the user can
  // resolve pending spawns without leaving the composer. A single pending spawn
  // gets a compact one-line row; with several, the header carries Approve all /
  // Reject all and each sub-agent gets its own row with per-agent Approve/Reject
  // (so one can be run and another rejected). "Review in panel" opens the
  // Subagents tab for the fuller per-agent view (task + streaming output).
  // Resolution goes through the same api.resolveApproval + markSubagentApproving
  // path the panel uses, so the two surfaces stay consistent for a given id.
  const pendingSpawnApprovals = useAppSelector(s => selectSlotPendingSpawnApprovals(s, slotId), shallowEqual)
  const reviewSpawnApprovals = useCallback(() => { dispatch(openActivityToTab('subagents')) }, [dispatch])
  // True once every pending spawn is mid-resolution — swaps the header buttons
  // for a "Resolving…" note. Cards stay in the pending list (status is still
  // 'pending') until the backend confirms, so the banner remains mounted.
  const spawnApprovalsResolving = pendingSpawnApprovals.length > 0 && pendingSpawnApprovals.every(a => a.approving)
  const resolveOneSpawn = useCallback((a: SubagentActivity, action: 'approve' | 'reject') => {
    if (!a.approval_id || a.approving) return
    dispatch(markSubagentApproving({ id: a.id, approving: true }))
    api.resolveApproval(a.approval_id, action).then(() => {
      // Terminate a rejected card here, because nothing else will. The backend's
      // `approval_resolved` frame carries only {id, approved} — no slot — so the
      // useWebSocket handler that would dispatch sseSubagentDone is skipped
      // (it requires data.slot to avoid misattributing cards across sessions).
      // An APPROVED spawn still converges: it runs and emits its own
      // spawn/chunk/done stream, each frame carrying a slot. A REJECTED spawn
      // never runs and emits nothing further, so without this the card stays
      // pending+approving and the banner sticks on "Resolving…" indefinitely.
      if (action === 'reject' && slotId) {
        dispatch(sseSubagentDone({ slot: slotId, id: a.id, elapsed: 0, error: 'rejected' }))
      }
    }).catch(() => dispatch(markSubagentApproving({ id: a.id, approving: false })))
  }, [dispatch, slotId])
  const resolveSpawnApprovals = useCallback((action: 'approve' | 'reject') => {
    for (const a of pendingSpawnApprovals) resolveOneSpawn(a, action)
  }, [pendingSpawnApprovals, resolveOneSpawn])

  const approvalBtnClass = 'inline-flex items-center gap-1 px-2 py-1 rounded-md bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] border border-border text-text text-[12px] cursor-pointer font-body hover:bg-[color-mix(in_srgb,var(--warn)_25%,transparent)] hover:text-text hover:border-border-strong transition-colors disabled:opacity-50'

  const inputRef = useRef<HTMLTextAreaElement>(null)
  // Publish the live caret so ChatPage's dictation handler can splice a
  // transcript in at the cursor instead of appending. Written on every caret
  // move (typing, click, selection); the value persists through blur (clicking
  // the mic button), which is exactly when a batch transcript needs it.
  const recordCaret = useCallback(() => {
    const ta = inputRef.current
    if (ta && voiceCaretRef) voiceCaretRef.current = { start: ta.selectionStart ?? 0, end: ta.selectionEnd ?? 0 }
  }, [voiceCaretRef])
  // Restore the caret after a dictation transcript lands in `value`. The update
  // arrives via the parent (onChange → ChatPage setInput → value prop), so the
  // parent can't set the DOM selection itself. rAF mirrors applyPickedToken:
  // wait for the controlled value to commit before moving the caret. Cheap on
  // ordinary edits — it no-ops unless a dictation splice armed a pending caret.
  useLayoutEffect(() => {
    const pendingRef = voicePendingCaretRef
    const pos = pendingRef?.current
    if (!pendingRef || pos == null) {
      // No dictation restore pending: keep voiceCaretRef in sync with the live
      // selection, but ONLY once it has been established by a real interaction.
      // Guard on an already-non-null ref so an untouched textarea holding an
      // existing draft doesn't publish offset 0 here (which would make the next
      // batch transcript prepend at 0 instead of using the append fallback that
      // a null ref provides).
      const el = inputRef.current
      if (el && voiceCaretRef && voiceCaretRef.current) voiceCaretRef.current = { start: el.selectionStart ?? 0, end: el.selectionEnd ?? 0 }
      return
    }
    pendingRef.current = null
    const raf = requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      const p = Math.min(pos, el.value.length)
      // Restore the caret WITHOUT taking focus: a batch transcript can land while
      // the user is focused in another field/session, and stealing focus would
      // corrupt their typing there. setSelectionRange works on an unfocused
      // element, so the caret is correct the moment the composer is (re)focused.
      el.setSelectionRange(p, p)
      if (voiceCaretRef) voiceCaretRef.current = { start: p, end: p }
    })
    // Cancel the frame if the slot switches (autoFocusKey) or value changes
    // again before it fires — otherwise the callback would stamp this slot's
    // caret onto whatever composer is mounted next.
    return () => cancelAnimationFrame(raf)
  }, [value, voicePendingCaretRef, voiceCaretRef, autoFocusKey])
  // Dictation-panel gate. Three independent conditions must hold: the setting
  // is on, the browser has WebGL2, and the OS is not asking for reduced motion
  // (the hook covers the latter two). A mic error always falls through to
  // VoiceStatusBar, which owns the dismissible error affordance — the panel
  // has no way to surface it. Resolves to the sample ref (not a boolean) so
  // the non-optional prop narrows without a cast.
  const dictationUsable = useDictationPanelUsable(voiceDictationPanel)
  const showDictation =
    dictationUsable && voiceRecording && !voiceError && voiceSampleRef ? voiceSampleRef : null
  const wrapperRef = useRef<HTMLDivElement>(null)
  // Backdrop mirror that paints chip backgrounds behind paste tokens; its scroll
  // is kept in lockstep with the textarea (see syncMirrorScroll on the textarea).
  const mirrorRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // "+" drop-up menu (upload file / image + browse toggle).
  const [plusOpen, setPlusOpen] = useState(false)
  const [ctxPopoverOpen, setCtxPopoverOpen] = useState(false)
  // Shelf responsiveness: measure the shelf row width and collapse chips to
  // icon-only (agent/project) + drop the model effort label when space is tight.
  // Truncation handles the in-between cases.
  const [shelfWidth, setShelfWidth] = useState(9999)
  const shelfRoRef = useRef<ResizeObserver | null>(null)
  const shelfRef = useCallback((el: HTMLDivElement | null) => {
    shelfRoRef.current?.disconnect()
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width
      if (typeof w === 'number') setShelfWidth(w)
    })
    ro.observe(el)
    shelfRoRef.current = ro
  }, [])
  // Below ~340px the labels no longer fit comfortably alongside the context bar
  // + model chip, so collapse the chips (agent/project) to icon-only.
  const shelfCompact = shelfWidth < 340
  // Tooltip for the project chip. The chip itself shows the basename (plus the
  // branch when known); the tooltip carries the full path so nothing that was
  // previously discoverable is lost, and names the branch even when the label
  // is truncated or the shelf has collapsed to icon-only.
  const projectChipTitle = useMemo(() => {
    if (!project) return i18nT('components.chatInput.select_project')
    const base = i18nT('components.chatInput.project_2', { path: project })
    if (!projectBranch) return base
    return projectDetached
      ? `${base}\n${i18nT('components.chatInput.detached_head_at', { branch: projectBranch })}`
      : `${base}\n${i18nT('components.chatInput.branch', { branch: projectBranch })}`
  }, [project, projectBranch, projectDetached])
  // Focus the composer when the dictation panel is up (as before) OR while a
  // batch transcript is landing (voiceTranscribing), so Enter sends and typing
  // edits the result. Deliberately NOT keyed on bare voiceRecording: focusing
  // during a STREAMING recording would invite mid-dictation typing that the
  // next partial rebuilds away — the panel (showDictation) already handles the
  // visible streaming case, where the user watches rather than types.
  useEffect(() => {
    if (showDictation || voiceTranscribing) inputRef.current?.focus()
  }, [showDictation, voiceTranscribing])

  // Escape CANCELS dictation (discards the audio), from ANYWHERE. Deliberately a
  // document-level listener rather than the textarea's onKeyDown: starting a
  // recording means clicking the mic button, so focus sits on that button and a
  // textarea-scoped handler never fires — the panel would advertise "Esc to
  // cancel" and do nothing. This DISCARDS: nothing is transcribed or inserted,
  // so an abandoned dictation is thrown away. Clicking the mic remains the
  // commit path (stop + transcribe).
  //
  // BUBBLE phase, not capture, and it yields three ways. Capture phase runs
  // before every descendant, so an open menu/popover/selector (this composer
  // has many) would lose its own Escape to this handler — recording would stop
  // and the menu would stay open. Bubbling lets the innermost control consume
  // Escape first; Radix and friends call preventDefault() when they do, which
  // is what `defaultPrevented` detects. The three explicit refs cover the
  // hand-rolled pickers that close on Escape WITHOUT preventing default, so
  // they cannot be detected that way.
  //
  // The `[role="dialog"]` probe is the precedence rule: Escape belongs to the
  // TOPMOST dismissible surface, and the composer is not it while a dialog is
  // up. Modal, CommandPalette and SnipOverlay all bind Escape on `window` and
  // all carry role="dialog", so one presence check defers to every one of them
  // rather than enumerating them. Without it this handler would steal Escape
  // from each — those surfaces own Escape, so intercepting it here would be a
  // regression, not a trade.
  //
  // stopPropagation() only once we have decided the key is OURS. document
  // bubbles on to `window`, and those window handlers do not check
  // defaultPrevented, so a snip started during recording would otherwise be
  // cancelled by the same keypress that stopped the recording.
  useEffect(() => {
    const cancel = onVoiceCancel || onVoiceToggle
    if (!voiceRecording || !cancel) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing || e.defaultPrevented) return
      if (slashMenuOpenRef.current || filePickerOpenRef.current || skillPickerOpenRef.current) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      e.stopPropagation()
      cancel()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [voiceRecording, onVoiceCancel, onVoiceToggle])

  const ctxWrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ctxPopoverOpen) return
    const handler = (e: MouseEvent) => {
      if (ctxWrapRef.current && !ctxWrapRef.current.contains(e.target as Node)) setCtxPopoverOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ctxPopoverOpen])
  const plusWrapRef = useRef<HTMLDivElement>(null)
  const plusBtnRef = useRef<HTMLButtonElement>(null)
  const plusMenuRef = useRef<HTMLDivElement>(null)
  const [plusRect, setPlusRect] = useState<DOMRect | null>(null)
  useEffect(() => {
    if (!plusOpen) return
    // Menu is portaled to <body> (escapes the input's overflow-hidden), so the
    // outside-click guard must also exclude the portaled menu, not just the button.
    const h = (e: MouseEvent) => {
      const t = e.target as Node
      if (!plusWrapRef.current?.contains(t) && !plusMenuRef.current?.contains(t)) setPlusOpen(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [plusOpen])
  const togglePlus = () => {
    if (!plusOpen && plusBtnRef.current) setPlusRect(plusBtnRef.current.getBoundingClientRect())
    setPlusOpen(o => !o)
  }
  // Client-side `accept` is a UX hint only (input-validation guidance: server enforces type via
  // magic bytes, size, and malware scanning — never trust the extension/MIME here).
  const openPicker = (imageOnly: boolean) => {
    const el = fileInputRef.current
    if (!el) return
    el.accept = imageOnly ? IMAGE_ACCEPT : FILE_ACCEPT
    el.click()
    setPlusOpen(false)
  }
  // Split send button (running turn): 'steer' (default) vs 'queue', chosen via
  // the chevron dropdown and persisted so the preference sticks across sessions.
  const [busySendMode, setBusySendMode] = useState<BusySendMode>(() => {
    try { return localStorage.getItem(BUSY_SEND_MODE_LS_KEY) === 'queue' ? 'queue' : 'steer' } catch { return 'steer' }
  })
  const [busyMenuOpen, setBusyMenuOpen] = useState(false)
  const [busyMenuRect, setBusyMenuRect] = useState<DOMRect | null>(null)
  const busySplitRef = useRef<HTMLDivElement>(null)
  const busyMenuRef = useRef<HTMLDivElement>(null)
  const busyCaretRef = useRef<HTMLButtonElement>(null)
  // This menu has no filter input; the ref stays null so useListboxKeyboard
  // treats ArrowUp from the first option as a no-op instead of a focus jump.
  const busyNoInputRef = useRef<HTMLElement | null>(null)
  const closeBusyMenuToTrigger = useCallback(() => {
    setBusyMenuOpen(false)
    busyCaretRef.current?.focus()
  }, [])
  // Keyboard operability for the portaled menu (WAI-ARIA menu pattern):
  // focus moves into the first option on open, ArrowUp/Down + Home/End roam,
  // Escape/Tab close and return focus to the caret trigger.
  const { onListKeyDown: onBusyMenuKeyDown } = useListboxKeyboard({
    open: busyMenuOpen,
    dropdownRef: busyMenuRef,
    inputRef: busyNoInputRef,
    hasFilterInput: false,
    filteredCount: BUSY_SEND_MODES.length,
    onEnterSingleMatch: () => {},
    closeToTrigger: closeBusyMenuToTrigger,
  })
  useEffect(() => {
    if (!busyMenuOpen) return
    // Menu is portaled to <body> (escapes the input's overflow-hidden), so the
    // outside-click guard must exclude both the split button and the menu.
    const h = (e: MouseEvent) => {
      const t = e.target as Node
      if (!busySplitRef.current?.contains(t) && !busyMenuRef.current?.contains(t)) setBusyMenuOpen(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [busyMenuOpen])
  const toggleBusyMenu = () => {
    if (!busyMenuOpen && busySplitRef.current) setBusyMenuRect(busySplitRef.current.getBoundingClientRect())
    setBusyMenuOpen(o => !o)
  }
  const selectBusyMode = (m: BusySendMode) => {
    setBusySendMode(m)
    safeSetItem(BUSY_SEND_MODE_LS_KEY, m)
    closeBusyMenuToTrigger()
  }
  // Steer is the active Enter/send action only while a live (not stopping)
  // turn is running on a steer-capable slot and the user hasn't switched the
  // split button to Queue. Everywhere else the composer falls back to onSend
  // (normal send, or server-side queue while running).
  const steerActive = isRunning && (!stopState || stopState === 'idle') && !!canSteer && !!onSteer && busySendMode === 'steer'
  const fireComposer = useCallback(() => {
    if (disabled) return
    // A batch dictation is still transcribing: block the send so the pending
    // transcript isn't left behind. Otherwise Enter/Send fires the current draft
    // BEFORE the transcript lands, orphaning the dictation into the emptied
    // composer. The transcript appends within ~1-2s, after which a normal Enter
    // sends the complete text. Covers both Enter (handleKeyDown) and the Send
    // button, since both route through here.
    if (voiceTranscribing) return
    if (steerActive && onSteer) onSteer()
    else onSend()
  }, [disabled, voiceTranscribing, steerActive, onSteer, onSend])
  const sendFollowUp = useCallback((text?: string) => {
    if (!disabled) onFollowUpSend?.(text)
  }, [disabled, onFollowUpSend])
  const { botName } = useBranding()
  const isMobile = useIsMobile()
  const ime = useImeGuard()
  const resolvedPlaceholder = placeholder || i18nT('components.chatInput.message_placeholder', { bot: botName })
  // An icon swap alone announces nothing, so the empty-state placeholder carries
  // the explanation — and it names typing as the other way out, so the morph
  // never feels like a trap.
  //
  // But ONLY when the transcript actually shows a broken turn. The default
  // placeholder is not dead space: it is the only surface that teaches the three
  // sigils (`/command · @file · $skill`), so overriding it unconditionally would
  // delete that hint for every returning chat and leave it visible only in a
  // brand-new one. On the dashboard the two conditions now coincide — ChatPage
  // gates the control on the interruption itself — but this component is still
  // callable with `continuable` alone, and in that case the hint survives and
  // the labeled Resume button carries the affordance on its own.
  const continuePlaceholder = continuable && onContinue && continueIsRecovery
    ? i18nT('components.chatInput.turn_interrupted_press_continue')
    : ''
  const continueLabel = i18nT(continueIsRecovery
    ? 'components.chatInput.continue_interrupted_turn'
    : 'components.chatInput.continue_thread')
  const [slashMenuOpen, setSlashMenuOpen] = useState(false)
  const [filePickerOpen, setFilePickerOpen] = useState(false)
  const [fileQuery, setFileQuery] = useState('')
  const [skillPickerOpen, setSkillPickerOpen] = useState(false)
  const [skillQuery, setSkillQuery] = useState('')
  // Open an in-input trigger picker from the + menu (mirrors typing the sigil):
  //  '/' slash commands (whole-input), '@' file mention, '$' skill. Inserts the
  //  sigil at a word boundary, opens the matching picker, then refocuses the box.
  const openTrigger = (sigil: '/' | '@' | '$') => {
    setPlusOpen(false)
    if (sigil === '/') {
      onChange('/')
      setSlashMenuOpen(true); setFilePickerOpen(false); setSkillPickerOpen(false)
    } else {
      const sep = value === '' || /\s$/.test(value) ? '' : ' '
      onChange(value + sep + sigil)
      setSlashMenuOpen(false)
      if (sigil === '@') { setFilePickerOpen(true); setFileQuery(''); setSkillPickerOpen(false) }
      else { setSkillPickerOpen(true); setSkillQuery(''); setFilePickerOpen(false) }
    }
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (el) { el.focus(); const n = el.value.length; el.setSelectionRange(n, n) }
    })
  }
  // Warm the shared ['skills'] cache when the input gains focus so the first
  // `$` trigger renders the picker instantly (the fetch is the only latency).
  // prefetchQuery is a no-op if the cache is already fresh (staleTime), so it's
  // cheap to call on every focus. Shares the key with SkillPickerMenu + SkillsTab.
  const queryClient = useQueryClient()
  const prefetchSkills = useCallback(() => {
    queryClient.prefetchQuery({
      queryKey: ['skills'],
      queryFn: () => api.skills(),
      staleTime: 5 * 60 * 1000,
    })
  }, [queryClient])
  // Shared caret-relative token insertion for the @/$ pickers: replace the
  // sigil-token ending at the caret with `token`, commit, and restore the caret
  // just after it. One copy keeps the two onSelect handlers duplication-free.
  const applyPickedToken = useCallback((tokenRe: RegExp, token: string) => {
    const el = inputRef.current
    const next = replaceTokenAtCaret(value, el?.selectionStart ?? value.length, tokenRe, token)
    onChange(next.value)
    requestAnimationFrame(() => { const e2 = inputRef.current; if (e2) { e2.focus(); e2.setSelectionRange(next.caret, next.caret) } })
  }, [value, onChange])
  const chatMessages = useAppSelector(s => s.chat.messages)
  const [manualHeight, setManualHeight] = useState<number | null>(() => {
    const saved = localStorage.getItem(INPUT_HEIGHT_LS_KEY)
    const n = saved ? parseInt(saved, 10) : NaN
    return !isNaN(n) && n >= INPUT_MIN_H ? n : null
  })

  // Drag-to-resize refs — resize wrapper div via direct DOM writes, commit on mouseup.
  // Resizing the wrapper (not the textarea) avoids layout thrashing: the textarea
  // fills the wrapper with height:100% so the browser only reflows the wrapper's
  // subtree, not the entire flex column + Virtuoso list above.
  const dragging = useRef(false)
  const dragStartY = useRef(0)
  const dragStartH = useRef(0)

  // Prompt history navigation: -1 = draft (not in history), else index into sentMessages.
  // Refs keep the handler stable across re-renders while preserving state between keystrokes.
  const historyIdxRef = useRef(-1)
  const draftRef = useRef('')
  // Refs mirror frequently-changing props/state read from inside the keydown handler
  // so it doesn't re-create on every keystroke.
  const valueRef = useRef(value)
  valueRef.current = value
  // Mirror the paste blocks so the undo-recording effect (keyed on
  // [value, autoFocusKey], not pasteBlocks) always snapshots the freshest set.
  const pasteBlocksRef = useRef(pasteBlocks)
  pasteBlocksRef.current = pasteBlocks
  // --- Prompt undo/redo history (per slot) ---
  // Explicit snapshot stack: undoHistoryRef[undoPointerRef] always mirrors the
  // live value. Rapid keystrokes coalesce into one entry; bulk deletes and
  // programmatic resets become their own restorable boundary. applyingUndoRef
  // suppresses re-recording the value we set during an undo/redo.
  const undoHistoryRef = useRef<UndoSnap[]>([{ value, selStart: value.length, selEnd: value.length, blocks: pasteBlocks }])
  const undoPointerRef = useRef(0)
  const undoLastEditRef = useRef(0)
  const applyingUndoRef = useRef(false)
  // True for the next paste only when the user pressed Cmd/Ctrl+Shift+V, so
  // handlePaste inserts the full text inline instead of collapsing it to a
  // `[ Paste #N ]` chip. Set on that keydown, cleared on any other keydown.
  const rawPasteRef = useRef(false)
  const prevUndoAfkRef = useRef(autoFocusKey)
  const slotSettlingRef = useRef(false)
  // True when the latest `value` change came from a real DOM edit (user typing,
  // IME, execCommand) rather than a parent-driven prop change (slot draft
  // restore). Lets the slot-settling logic tell a keystroke apart from the
  // draft restore regardless of whether ChatPage restores sync or async.
  const valueFromUserRef = useRef(false)
  // Tracks the prior render's raw pending state so the completion effect can
  // record a single undo boundary when an optimize actually finishes (as
  // opposed to the scoped `optimizing` flipping off because the user switched
  // sessions mid-flight).
  const wasOptimizingRef = useRef(false)
  // Hoisted here (assigned below, where `optimizing` is defined) so the
  // recording effect above the optimizer block can read it.
  const optimizingRef = useRef(false)
  // The slot that initiated the in-flight optimize. Overlay / readOnly / pending
  // state is scoped to this slot so navigating to another session mid-optimize
  // dismisses the overlay here and only reveals it again when we return to the
  // originating session. Null when no optimize is in flight.
  const optimizeSlotRef = useRef<string | null>(null)
  const slashMenuOpenRef = useRef(false)
  slashMenuOpenRef.current = slashMenuOpen
  const filePickerOpenRef = useRef(false)
  filePickerOpenRef.current = filePickerOpen
  const skillPickerOpenRef = useRef(false)
  skillPickerOpenRef.current = skillPickerOpen

  // Auto-focus textarea when the active session changes (autoFocusKey).
  // Track the previous key in a ref so the effect only acts on real key
  // transitions — `disabled` and `isMobile` are in the dep array to keep the
  // closure fresh, but a flip in either (e.g. AI finishes responding -> disabled
  // goes true -> false) MUST NOT steal focus while the user reads or scrolls.
  //
  // Also bail on touch devices: programmatic .focus() there pops the on-screen
  // keyboard, so merely tapping a session would cover half the screen before the
  // user has decided to type. `isMobile` (viewport width < 768px) already covers
  // portrait phones, but it's a LAYOUT signal — it misses tablets and phones in
  // landscape (≥768px), which are still touch. `isTouchDevice()` (coarse pointer
  // / no hover) is the precise keyboard-popping predicate. It's called inline,
  // not in the dep array, because a device's touch capability is effectively
  // static for the session (unlike `disabled`/`isMobile`, which flip at runtime).
  //
  // IMPORTANT: bail on `disabled || isMobile` BEFORE advancing the ref. If a
  // session switch lands while disabled=true (e.g. the user picks a session that
  // is currently stopping), advancing the ref here would consume the focus
  // opportunity — when disabled later flips false the effect re-runs but the
  // key check matches and bails. Holding the ref preserves the pending focus
  // until the gate clears.
  //
  // The active-element check IS placed after the ref update — that's a "decline
  // and don't retry" condition (if the user is typing in the agent picker, we
  // shouldn't come back later and steal focus once they switch back).
  const prevAutoFocusKeyRef = useRef<typeof autoFocusKey>(undefined)
  useEffect(() => {
    if (autoFocusKey == null || autoFocusKey === prevAutoFocusKeyRef.current) {
      prevAutoFocusKeyRef.current = autoFocusKey
      return
    }
    if (disabled || isMobile || isTouchDevice()) return
    prevAutoFocusKeyRef.current = autoFocusKey
    const ae = document.activeElement as HTMLElement | null
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return
    inputRef.current?.focus()
  }, [autoFocusKey, disabled, isMobile])

  // Global "/" shortcut to focus chat input (like GitHub, YouTube, Slack)
  useEffect(() => {
    const onSlashFocus = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable) return
      e.preventDefault()
      inputRef.current?.focus()
    }
    document.addEventListener('keydown', onSlashFocus)
    return () => document.removeEventListener('keydown', onSlashFocus)
  }, [])

  const inputResize = usePointerDrag({
    threshold: 0,
    onStart: (e) => {
      if (!wrapperRef.current) return
      const h = wrapperRef.current.offsetHeight
      dragging.current = true
      dragStartY.current = e.clientY
      dragStartH.current = h
      // Use current natural height as floor so drag never snaps up
      dragMinHRef.current = Math.min(dragMinHRef.current, h)
      // Lock in current height so auto-resize stops interfering
      setManualHeight(h)
      document.body.style.cursor = 'row-resize'
      document.body.style.userSelect = 'none'
      // Isolate reflow to this subtree during drag
      wrapperRef.current.style.contain = 'strict'
    },
    onMove: ({ y }) => {
      if (!dragging.current || !wrapperRef.current) return
      // Account for CSS zoom/scale on #root
      const scale = parseInt(localStorage.getItem('mc-zoom') || '100', 10) / 100
      const maxH = effectiveVh() * INPUT_DRAG_MAX_RATIO
      const delta = (dragStartY.current - y) / scale
      const h = Math.min(maxH, Math.max(dragMinHRef.current, dragStartH.current + delta))
      // Direct DOM write on wrapper — no React state, no textarea auto-size
      wrapperRef.current.style.height = h + 'px'
    },
    onEnd: () => {
      if (!dragging.current || !wrapperRef.current) return
      dragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      wrapperRef.current.style.contain = ''
      // Commit final height to React state
      const finalH = wrapperRef.current.offsetHeight
      setManualHeight(finalH)
      safeSetItem(INPUT_HEIGHT_LS_KEY, String(Math.round(finalH)))
    },
  })
  // Unmount guard: onEnd can't fire if the composer unmounts mid-drag
  // (setPointerCapture dies with the element), so restore the global body styles
  // here to avoid leaving the resize cursor / text-selection lock stuck.
  useEffect(() => () => {
    if (dragging.current) {
      dragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [])



  const resetHeight = useCallback(() => {
    setManualHeight(null)
    localStorage.removeItem(INPUT_HEIGHT_LS_KEY)
    if (wrapperRef.current) { wrapperRef.current.style.height = ''; wrapperRef.current.style.maxHeight = '' }
  }, [])

  // Sync persisted manual height to DOM (same path as drag writes)
  useEffect(() => {
    if (!wrapperRef.current) return
    if (manualHeight !== null) {
      wrapperRef.current.style.height = Math.max(manualHeight, INPUT_MIN_H) + 'px'
      wrapperRef.current.style.maxHeight = `${INPUT_DRAG_MAX_RATIO * 100}vh`
    } else {
      wrapperRef.current.style.height = ''
      wrapperRef.current.style.maxHeight = ''
    }
  }, [manualHeight, pendingFiles.length])

  // Auto-resize textarea to fit content
  useEffect(() => {
    if (inputRef.current && !dragging.current) applyHeight(inputRef.current, manualHeight, prefillHint)
  }, [value, prefillHint, manualHeight])

  // Keep the paste-highlight mirror's scroll aligned with the textarea after
  // value/height changes (applyHeight mutates scrollTop programmatically, which
  // doesn't fire the textarea's onScroll). rAF lets layout settle first.
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      if (mirrorRef.current && inputRef.current) mirrorRef.current.scrollTop = inputRef.current.scrollTop
    })
    return () => cancelAnimationFrame(id)
  }, [value, prefillHint, manualHeight])

  // Reset manual height when input is cleared (new message sent)
  const prevValueRef = useRef(value)
  useEffect(() => {
    if (prevValueRef.current && !value) resetHeight()
    // Exit history mode when value diverges from the recalled message
    // (user edited it, or the send pipeline cleared it).
    if (historyIdxRef.current !== -1 && value !== sentMessages?.[historyIdxRef.current]) {
      historyIdxRef.current = -1
      draftRef.current = ''
    }
    prevValueRef.current = value
  }, [value, resetHeight, sentMessages])

  // Record undo snapshots as the controlled value changes.
  useEffect(() => {
    const el = inputRef.current
    // Consume the "this change came from a DOM edit" flag exactly once per run.
    const fromUser = valueFromUserRef.current
    valueFromUserRef.current = false
    const seed = () => {
      undoHistoryRef.current = [{
        value,
        selStart: el?.selectionStart ?? value.length,
        selEnd: el?.selectionEnd ?? value.length,
        blocks: pasteBlocksRef.current,
      }]
      undoPointerRef.current = 0
      undoLastEditRef.current = 0
    }
    // Skip the change we just made via undo/redo — the pointer is already
    // correct. Keep slot tracking in sync so a coincident switch can't trigger
    // a spurious reset on a later pass.
    if (applyingUndoRef.current) {
      applyingUndoRef.current = false
      prevUndoAfkRef.current = autoFocusKey
      return
    }
    // Slot/session switch. ChatPage restores a slot's draft via the
    // `[activeSlot]` effect in ChatPage.tsx, which calls `setInput` in a
    // *separate* commit after `activeSlot` (`autoFocusKey`) changes — so on this
    // pass `value` may still be the previous slot's text. Reseed now and mark
    // the next value change as "settling" so the draft restore reseeds the base
    // rather than being recorded as an undoable transition from the prior slot's
    // stale text — otherwise Ctrl+Z in the new slot would restore the old draft.
    if (autoFocusKey !== prevUndoAfkRef.current) {
      prevUndoAfkRef.current = autoFocusKey
      seed()
      slotSettlingRef.current = true
      return
    }
    if (slotSettlingRef.current) {
      slotSettlingRef.current = false
      // The first value change after a switch. A parent-driven prop change is
      // the draft restore (reseed the base at it). A real DOM edit means the
      // user typed before/without a separate restore commit — i.e. ChatPage
      // restored synchronously, the base was already seeded at the switch — so
      // fall through and record the keystroke as a normal edit instead of
      // folding it into the base. Keeps undo correct for sync and async restore.
      if (!fromUser) {
        if (undoHistoryRef.current[undoPointerRef.current]?.value !== value) seed()
        return
      }
    }
    // While the optimizer owns the textarea, skip per-keystroke recording. A
    // single-shot optimize (one execCommand) lands after `optimizing` clears and
    // records normally; a streaming optimize is captured as one boundary by the
    // completion effect below. Either way one Ctrl+Z reverses a whole optimize.
    if (optimizingRef.current) return
    const hist = undoHistoryRef.current
    const ptr = undoPointerRef.current
    const prev = hist[ptr]?.value
    if (prev === value) return // selection-only re-render, no text change
    const snap: UndoSnap = {
      value,
      selStart: el?.selectionStart ?? value.length,
      selEnd: el?.selectionEnd ?? value.length,
      blocks: pasteBlocksRef.current,
    }
    const now = Date.now()
    // Coalesce only small, incremental, recent edits at the tip of the history.
    // A bulk change (clear, recall, optimize, select-all-delete) or a pause
    // starts a new boundary so it can be undone on its own. The `prev !== ''`
    // guard also makes the first char typed from empty its own boundary.
    const incremental =
      prev !== undefined && prev !== '' && value !== '' &&
      Math.abs(value.length - prev.length) < UNDO_BULK_DELTA
    const recent = now - undoLastEditRef.current < UNDO_COALESCE_MS
    const atTip = ptr === hist.length - 1
    if (atTip && incremental && recent) {
      hist[ptr] = snap // merge typing burst into the current entry
    } else {
      hist.splice(ptr + 1) // editing discards any redo branch
      hist.push(snap)
      if (hist.length > UNDO_MAX_HISTORY) hist.shift()
      undoPointerRef.current = hist.length - 1
    }
    undoLastEditRef.current = now
  }, [value, autoFocusKey])

  const handleInput = useCallback((e: React.FormEvent<HTMLTextAreaElement>) => {
    if (!dragging.current) applyHeight(e.target as HTMLTextAreaElement, manualHeight, prefillHint)
  }, [manualHeight, prefillHint])

  const setTextUndoable = useCallback((text: string) => {
    const el = inputRef.current
    if (!el) { onChange(text); return }
    el.readOnly = false
    el.focus()
    el.select()
    document.execCommand('insertText', false, text)
  }, [onChange])

  const optimizeMutation = useMutation({
    mutationFn: async (
      { prompt, context, pastes }: {
        prompt: string
        context: string
        pastes?: Array<{ seq: number; content: string }>
        slotId: string | null
      },
    ) => {
      const resp = await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-session-key': 'dashboard:ui' },
        credentials: 'same-origin',
        body: JSON.stringify({ prompt, context, pastes }),
      })
      if (!resp.ok) throw new Error('optimizer failed')
      return resp.json()
    },
    onSuccess: (data, variables) => {
      // Originating session is still the one on screen: write the result here,
      // undoable. The textarea stayed readOnly for the whole optimize on this
      // session so the value can't have diverged from what we sent; the
      // trim-guard defends against a stray whitespace-only mismatch or any
      // unforeseen divergence (drop rather than clobber).
      if (variables.slotId === slotId) {
        if (valueRef.current.trim() !== variables.prompt.trim()) return
        setTextUndoable(data.changed && data.optimized ? data.optimized : valueRef.current.trim())
        return
      }
      // The user navigated to a different session mid-optimize. Route the
      // result back to the session that started it instead of writing into the
      // session now on screen (wrong session) or dropping it (lost work). Fall
      // back to the original prompt when the optimizer returned no change.
      onOptimizeResult?.(variables.slotId, data.changed && data.optimized ? data.optimized : variables.prompt)
    },
    onError: (err, variables) => {
      // eslint-disable-next-line no-console -- surface prompt-optimizer failures to the dev console
      console.warn('optimizer failed', err)
      // Same slot-routing split as onSuccess. On the originating session,
      // restore the original prompt in place; otherwise hand it back to that
      // session's draft so a failed optimize on a backgrounded session doesn't
      // leave stale readOnly text or vanish.
      if (variables.slotId === slotId) {
        if (valueRef.current.trim() !== variables.prompt.trim()) return
        setTextUndoable(valueRef.current.trim())
        return
      }
      onOptimizeResult?.(variables.slotId, variables.prompt)
    },
  })
  // Raw request lifecycle — true whenever a request is in flight, regardless of
  // which session is currently displayed.
  const optimizePending = optimizeMutation.isPending
  // Scoped view of that state: only "optimizing" while we're still showing the
  // slot that initiated it. Navigating to a different session dismisses the
  // overlay / readOnly / disabled state here; returning restores it. In grid
  // mode each pane has its own ChatInput + mutation, so slotId always matches
  // and this reduces to the raw pending flag.
  const optimizing = optimizePending && optimizeSlotRef.current === slotId
  optimizingRef.current = optimizing
  // Re-entrancy guard reads the RAW lifecycle: only one optimize may be in
  // flight per ChatInput instance. Without this, the button on a *different*
  // session (where scoped `optimizing` is false) could fire a second request
  // that clobbers the single mutation's in-flight state.
  const optimizePendingRef = useRef(false)
  optimizePendingRef.current = optimizePending

  // When an optimize completes, ensure its result is a single undo boundary.
  // The recording effect skips writes while `optimizing` is true; a single-shot
  // optimize lands after `optimizing` clears and is already recorded, but a
  // streaming optimize would otherwise leave the final value unrecorded — so
  // push one boundary here if the tip doesn't already hold it. Idempotent: if
  // the recording effect already captured it, the value-equality guard no-ops.
  //
  // Keyed on the RAW pending lifecycle (not the slot-scoped `optimizing`) and
  // fenced to the originating slot: switching sessions mid-flight flips scoped
  // `optimizing` off without the request finishing, and we must NOT record a
  // boundary against the session we navigated to. We only record when the
  // request truly settles while the originating slot is still displayed; the
  // request-diverged case is dropped by onSuccess/onError anyway.
  useEffect(() => {
    if (wasOptimizingRef.current && !optimizePending) {
      const originating = optimizeSlotRef.current
      optimizeSlotRef.current = null
      if (originating === slotId) {
        const v = valueRef.current
        const hist = undoHistoryRef.current
        const ptr = undoPointerRef.current
        if (hist[ptr]?.value !== v) {
          const el = inputRef.current
          hist.splice(ptr + 1)
          hist.push({ value: v, selStart: el?.selectionStart ?? v.length, selEnd: el?.selectionEnd ?? v.length, blocks: pasteBlocksRef.current })
          if (hist.length > UNDO_MAX_HISTORY) hist.shift()
          undoPointerRef.current = hist.length - 1
          undoLastEditRef.current = Date.now()
        }
      }
    }
    wasOptimizingRef.current = optimizePending
  }, [optimizePending, slotId])
  const { mutate: runOptimize } = optimizeMutation

  const optimizePrompt = useCallback(() => {
    const txt = valueRef.current.trim()
    // Guard on the RAW lifecycle so a second optimize can't start while one is
    // in flight — even from a different session where scoped `optimizing` reads
    // false (a single mutation backs this instance).
    if (!txt || optimizePendingRef.current) return
    // Pin the slot that owns this optimize so the overlay and the completion
    // handler stay bound to it across session switches.
    optimizeSlotRef.current = slotId
    const context = chatMessages
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .slice(-10)
      .map(m => (m.content || '').slice(0, 200))
      .join('\n')
    // Forward the full content behind each paste placeholder still present in
    // the draft, so the optimizer understands the paste without us expanding
    // the "[ Paste #N · M lines ]" token inline. The optimizer preserves the
    // tokens verbatim in its output, so pasteBlocks keeps mapping them back on
    // send. Only referenced blocks are sent (pruneBlocks drops stale ones).
    const referenced = pruneBlocks(txt, pasteBlocks)
    const pastes = referenced.map(b => ({ seq: b.seq, content: b.content }))
    runOptimize({ prompt: txt, context, pastes, slotId })
  }, [runOptimize, chatMessages, pasteBlocks, slotId])

  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Cmd/Ctrl+Shift+V → next paste inserts full text inline (no chip collapse).
    // Self-clearing: any other keydown resets the flag so it only ever affects
    // the paste that immediately follows this exact shortcut. We do NOT
    // preventDefault — the browser still fires the paste event we hook below.
    rawPasteRef.current = (e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === 'v'
    // Undo / redo — drive the explicit per-slot history so Ctrl/Cmd+Z restores
    // text even after a programmatic reset (send-clear, ↑/↓ recall, optimize)
    // wiped the browser's native undo stack. We own the gesture and
    // preventDefault native undo so behaviour is deterministic regardless of how
    // `value` changed. Cmd/Ctrl+Z = undo, Cmd/Ctrl+Shift+Z or Ctrl+Y = redo.
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !ime.isComposing(e) && !optimizingRef.current) {
      const k = e.key.toLowerCase()
      const isUndo = k === 'z' && !e.shiftKey
      const isRedo = (k === 'z' && e.shiftKey) || k === 'y'
      if (isUndo || isRedo) {
        e.preventDefault()
        const hist = undoHistoryRef.current
        let ptr = undoPointerRef.current
        if (isUndo && ptr > 0) ptr -= 1
        else if (isRedo && ptr < hist.length - 1) ptr += 1
        else return // nothing to undo/redo
        undoPointerRef.current = ptr
        const snap = hist[ptr]
        applyingUndoRef.current = true
        onChange(snap.value)
        // Restore the paste blocks captured in this snapshot so a `[ Paste #N ]`
        // token brought back by the undo has its backing content again. Only
        // emit when the set actually differs (identity or membership) to avoid a
        // redundant parent render on plain-text undo. The pruneBlocks effect
        // would otherwise strip a block whose token the undo just restored.
        if (onPasteBlocksChange && !sameBlocks(pasteBlocksRef.current, snap.blocks)) {
          onPasteBlocksChange(snap.blocks)
        }
        requestAnimationFrame(() => {
          const el = inputRef.current
          if (!el) return
          el.focus()
          el.setSelectionRange(snap.selStart, snap.selEnd)
        })
        return
      }
    }
    // Atomic paste-token handling — keep caret out of token interior and
    // treat tokens as single deletable units. Runs before Enter/history so
    // edits on or around a token never reach the default textarea handling.
    if (pasteBlocks.length && !ime.isComposing(e)) {
      const ta = e.currentTarget
      const v = valueRef.current
      const ss = ta.selectionStart ?? 0
      const se = ta.selectionEnd ?? 0
      const isCollapsed = ss === se
      const ranges = findTokenRanges(v, pasteBlocks)

      const removeBlockAtom = (r: { start: number; end: number; block: PasteBlock }) => {
        e.preventDefault()
        const next = v.slice(0, r.start) + v.slice(r.end)
        onChange(next)
        onPasteBlocksChange?.(pasteBlocks.filter(b => b.id !== r.block.id))
        requestAnimationFrame(() => {
          const el = inputRef.current
          if (el) el.setSelectionRange(r.start, r.start)
        })
      }

      // Backspace with caret just past a token → delete whole token
      if (e.key === 'Backspace' && isCollapsed && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.end === ss)
        if (adj) { removeBlockAtom(adj); return }
      }
      // Cmd+Backspace (line-back delete on Mac) — extend deletion to cover
      // any token that intersects the caret-to-line-start range, so we never
      // slice a token mid-text. Also drops the associated PasteBlock(s).
      if (e.key === 'Backspace' && isCollapsed && e.metaKey) {
        const lineStart = v.lastIndexOf('\n', ss - 1) + 1
        const intersecting = ranges.filter(r => r.start < ss && r.end > lineStart)
        if (intersecting.length) {
          e.preventDefault()
          const deleteStart = Math.min(lineStart, ...intersecting.map(r => r.start))
          const removedIds = new Set(
            ranges.filter(r => r.start >= deleteStart && r.end <= ss).map(r => r.block.id),
          )
          const next = v.slice(0, deleteStart) + v.slice(ss)
          onChange(next)
          onPasteBlocksChange?.(pasteBlocks.filter(b => !removedIds.has(b.id)))
          requestAnimationFrame(() => {
            const el = inputRef.current
            if (el) el.setSelectionRange(deleteStart, deleteStart)
          })
          return
        }
      }
      // Alt/Ctrl+Backspace (word-back delete) — if caret is adjacent to a
      // token, treat as full-token delete (same as plain Backspace). Beyond
      // that, we leave native behavior alone; word boundaries are fuzzy and
      // tokens are on their own line, so the common case is the adjacent one.
      if (e.key === 'Backspace' && isCollapsed && (e.altKey || e.ctrlKey) && !e.metaKey) {
        const adj = ranges.find(r => r.end === ss)
        if (adj) { removeBlockAtom(adj); return }
      }
      // Delete with caret just before a token → delete whole token
      if (e.key === 'Delete' && isCollapsed && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.start === ss)
        if (adj) { removeBlockAtom(adj); return }
      }
      // Cmd+Delete (forward line-delete on Mac) — mirror Cmd+Backspace in
      // the forward direction: extend deletion to cover intersecting tokens.
      if (e.key === 'Delete' && isCollapsed && e.metaKey) {
        const nextNl = v.indexOf('\n', ss)
        const lineEnd = nextNl === -1 ? v.length : nextNl
        const intersecting = ranges.filter(r => r.end > ss && r.start < lineEnd)
        if (intersecting.length) {
          e.preventDefault()
          const deleteEnd = Math.max(lineEnd, ...intersecting.map(r => r.end))
          const removedIds = new Set(
            ranges.filter(r => r.start >= ss && r.end <= deleteEnd).map(r => r.block.id),
          )
          const next = v.slice(0, ss) + v.slice(deleteEnd)
          onChange(next)
          onPasteBlocksChange?.(pasteBlocks.filter(b => !removedIds.has(b.id)))
          requestAnimationFrame(() => {
            const el = inputRef.current
            if (el) el.setSelectionRange(ss, ss)
          })
          return
        }
      }
      // Alt/Ctrl+Delete (word-forward delete) — adjacent-token atomic delete.
      if (e.key === 'Delete' && isCollapsed && (e.altKey || e.ctrlKey) && !e.metaKey) {
        const adj = ranges.find(r => r.start === ss)
        if (adj) { removeBlockAtom(adj); return }
      }
      // Arrow left/right — skip over token as if it were a single character
      if (e.key === 'ArrowLeft' && isCollapsed && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.end === ss)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => inputRef.current?.setSelectionRange(adj.start, adj.start))
          return
        }
      }
      if (e.key === 'ArrowRight' && isCollapsed && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const adj = ranges.find(r => r.start === ss)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => inputRef.current?.setSelectionRange(adj.end, adj.end))
          return
        }
      }
      // Shift+Arrow — extend selection past the whole token in one step
      if (e.key === 'ArrowLeft' && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const dir = ta.selectionDirection || 'forward'
        const active = dir === 'backward' ? ss : se
        const adj = ranges.find(r => r.end === active)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => {
            const el = inputRef.current; if (!el) return
            if (dir === 'backward') el.setSelectionRange(adj.start, se, 'backward')
            else el.setSelectionRange(ss, adj.start, ss <= adj.start ? 'forward' : 'backward')
          })
          return
        }
      }
      if (e.key === 'ArrowRight' && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const dir = ta.selectionDirection || 'forward'
        const active = dir === 'backward' ? ss : se
        const adj = ranges.find(r => r.start === active)
        if (adj) {
          e.preventDefault()
          requestAnimationFrame(() => {
            const el = inputRef.current; if (!el) return
            if (dir === 'backward') el.setSelectionRange(adj.end, se, adj.end <= se ? 'backward' : 'forward')
            else el.setSelectionRange(ss, adj.end, 'forward')
          })
          return
        }
      }

      // Post-keydown snap for word/line/document-jump shortcuts
      // (Alt+Arrow on Mac, Ctrl+Arrow on Win/Linux, Cmd+Arrow line jump, Home/End).
      // The browser performs the native jump; we check afterwards if caret or
      // selection endpoint landed strictly inside a token and snap it out in
      // the direction of motion.
      const isNavKey = e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'Home' || e.key === 'End'
      const hasNavModifier = e.altKey || e.ctrlKey || e.metaKey || e.key === 'Home' || e.key === 'End'
      if (isNavKey && hasNavModifier) {
        const leftward = e.key === 'ArrowLeft' || e.key === 'Home'
        requestAnimationFrame(() => {
          const el = inputRef.current; if (!el) return
          const freshRanges = findTokenRanges(el.value, pasteBlocks)
          if (!freshRanges.length) return
          const nss = el.selectionStart ?? 0
          const nse = el.selectionEnd ?? 0
          const snapPos = (p: number) => {
            for (const r of freshRanges) {
              if (p > r.start && p < r.end) return leftward ? r.start : r.end
            }
            return p
          }
          const a = snapPos(nss)
          const b = snapPos(nse)
          if (a === nss && b === nse) return
          const dir = el.selectionDirection || 'forward'
          el.setSelectionRange(Math.min(a, b), Math.max(a, b), dir as 'forward' | 'backward' | 'none')
        })
      }
    }

    // Cmd+Shift+Enter (or Ctrl+Shift+Enter) → optimize prompt.
    // preventDefault always fires when the combo is detected so the browser's
    // default Enter behavior (newline insert) doesn't leak through when the
    // gateway is offline. The action itself is gated on `connected` to match
    // the disabled-state on the Optimize button (line ~1734).
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && e.shiftKey) {
      e.preventDefault()
      if (connected) optimizePrompt()
      return
    }
    // Mode: enter-ctrl-newline — Ctrl/Cmd+Enter inserts newline, Enter sends
    if (sendOnEnter === 'enter-ctrl-newline' && e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      const ta = e.currentTarget
      const start = ta.selectionStart
      const end = ta.selectionEnd
      const val = ta.value
      onChange(val.slice(0, start) + '\n' + val.slice(end))
      requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = start + 1 })
      return
    }
    const sendKey = sendOnEnter === 'ctrl-enter'
      ? (e.key === 'Enter' && (e.metaKey || e.ctrlKey))
      : (e.key === 'Enter' && !e.shiftKey)
    if (sendKey && !e.defaultPrevented && !ime.isComposing(e) && !optimizingRef.current) {
      // preventDefault always fires when sendKey matches so the textarea's
      // default Enter behavior (newline insert into draft) doesn't leak
      // through when the gateway is offline. The action itself is gated on
      // `connected` to match the disabled-state on the Send button.
      // While a turn is running, Enter follows the split-button mode:
      // steer (default) injects into the running turn; queue defers.
      e.preventDefault()
      if (connected) fireComposer()
      return
    }
    // Prompt history: ↑/↓ cycles through prior user messages.
    // Ignore when IME composing, no history, modifier keys, or when
    // slash-command / file-picker / skill-picker menus are open (they own ↑/↓).
    if (
      !sentMessages?.length ||
      slashMenuOpenRef.current || filePickerOpenRef.current || skillPickerOpenRef.current ||
      ime.isComposing(e) ||
      e.metaKey || e.ctrlKey || e.altKey || e.shiftKey
    ) return
    const ta = e.currentTarget
    const len = sentMessages.length
    const cur = valueRef.current
    // After recall, place the caret where the next arrow press will re-engage
    // history immediately (↑ → start, ↓ → end). Deferred to next frame so the
    // controlled textarea has re-rendered with the new value first.
    const moveCaretAfterRecall = (pos: 'start' | 'end') => {
      requestAnimationFrame(() => {
        const el = inputRef.current
        if (!el) return
        const p = pos === 'start' ? 0 : el.value.length
        el.setSelectionRange(p, p)
      })
    }
    if (e.key === 'ArrowUp') {
      // Only intercept when input is empty OR caret is collapsed at position 0.
      const atStart = ta.selectionStart === 0 && ta.selectionEnd === 0
      if (!atStart && cur !== '') return
      const idx = historyIdxRef.current
      if (idx === -1) {
        // Entering history mode — save current draft (may be empty).
        draftRef.current = cur
        historyIdxRef.current = len - 1
        onChange(sentMessages[len - 1])
        moveCaretAfterRecall('start')
      } else if (idx > 0) {
        historyIdxRef.current = idx - 1
        onChange(sentMessages[idx - 1])
        moveCaretAfterRecall('start')
      } else {
        // Already at oldest — consume to avoid caret jumping in textarea.
      }
      e.preventDefault()
    } else if (e.key === 'ArrowDown') {
      const idx = historyIdxRef.current
      if (idx === -1) return // not in history mode — let textarea handle
      // Only intercept when caret is at end (so multi-line edits still navigate within).
      const atEnd = ta.selectionStart === cur.length && ta.selectionEnd === cur.length
      if (!atEnd) return
      if (idx < len - 1) {
        historyIdxRef.current = idx + 1
        onChange(sentMessages[idx + 1])
        moveCaretAfterRecall('end')
      } else {
        // Past newest — restore draft and exit history mode.
        historyIdxRef.current = -1
        onChange(draftRef.current)
        draftRef.current = ''
        moveCaretAfterRecall('end')
      }
      e.preventDefault()
    }
  }, [fireComposer, onChange, sentMessages, sendOnEnter, pasteBlocks, onPasteBlocksChange, connected, ime, optimizePrompt])

  /** Intercept clipboard paste — files go to upload path, big text gets collapsed into a token. */
  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    // Cmd/Ctrl+Shift+V bypass: consume the one-shot flag up front, before any
    // early return below, so it can never leak into a later paste (e.g. a
    // context-menu paste with no intervening keydown to clear it).
    const forceRaw = rawPasteRef.current
    rawPasteRef.current = false
    // File paste takes precedence — but not when text is also available (macOS Office
    // apps include an image rendering alongside the copied text in the clipboard).
    const clipTypes = e.clipboardData.types || []
    const hasText = clipTypes.includes('text/plain') || clipTypes.includes('text/html')
    const files = Array.from(e.clipboardData.items)
      .filter(i => i.kind === 'file')
      .map(i => i.getAsFile())
      .filter((f): f is File => f !== null)
    if (files.length && onUploadFiles && !hasText) {
      e.preventDefault()
      onUploadFiles(files)
      return
    }
    // Text paste. Sources that serialize rendered HTML (web pages, PDFs, chat
    // bubbles, table cells) routinely tack trailing blank lines onto a copied
    // "single line", and a <textarea> inserts them verbatim — so the paste shows
    // the line followed by several empty rows. Strip a trailing run of blank
    // lines up front (only whitespace runs that include a newline; a paste
    // ending in plain spaces and interior blank lines are untouched). Raw paste
    // (Cmd/Ctrl+Shift+V) opts out entirely.
    const pasted = e.clipboardData.getData('text')
    const cleaned = forceRaw ? pasted : stripTrailingBlankLines(pasted)

    const ta = e.currentTarget
    const start = ta.selectionStart ?? value.length
    const end = ta.selectionEnd ?? start
    const before = value.slice(0, start)
    const after = value.slice(end)

    // Big paste → collapse into a `[ Paste #N ]` chip. Uses the cleaned text so
    // the chip's line count and stored content exclude the stripped blanks.
    if (onPasteBlocksChange && !forceRaw && shouldCollapsePaste(cleaned)) {
      e.preventDefault()
      const block: PasteBlock = { id: makePasteId(), seq: nextSeq(pasteBlocks), lines: countLines(cleaned), content: cleaned }
      const token = formatToken(block)
      // Surround the token with newlines so the chip lives on its own line —
      // long-form pasted content rarely flows with typed text around it.
      // Skip the leading newline when the caret is at the start of a line,
      // and the trailing one when the caret is at the end of a line.
      const leadingNewline = before && !before.endsWith('\n') ? '\n' : ''
      const trailingNewline = after && !after.startsWith('\n') ? '\n' : ''
      const insert = leadingNewline + token + trailingNewline
      valueFromUserRef.current = true // a paste is a real user edit, not a draft restore
      onChange(before + insert + after)
      onPasteBlocksChange([...pasteBlocks, block])
      // Restore caret right after the inserted token + trailing newline.
      requestAnimationFrame(() => {
        if (ta && document.activeElement === ta) {
          const pos = before.length + insert.length
          ta.setSelectionRange(pos, pos)
        }
      })
      return
    }

    // Small paste. Only intercept when trailing blanks were actually stripped
    // AND something remains — an all-blank clipboard (cleaned === '') is left to
    // the browser so the paste is never a silent no-op.
    if (cleaned !== pasted && cleaned !== '') {
      e.preventDefault()
      // Insert through the native input path so the textarea's own onChange runs:
      // that fires the /, @, $ picker detection, marks the edit user-driven, and
      // keeps native undo. Fall back to a controlled-value splice where
      // execCommand is unavailable (jsdom/tests) or reports failure.
      let inserted = false
      try {
        inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, cleaned)
      } catch { inserted = false }
      if (inserted) return
      valueFromUserRef.current = true
      onChange(before + cleaned + after)
      requestAnimationFrame(() => {
        if (ta && document.activeElement === ta) {
          const pos = before.length + cleaned.length
          ta.setSelectionRange(pos, pos)
        }
      })
    }
  }, [onUploadFiles, onPasteBlocksChange, pasteBlocks, value, onChange])

  /** Two-step click on a collapsed-paste token:
   *    1st click (detail=1) → select the token as a range (visual highlight)
   *    2nd click (detail=2, i.e. a quick second click = native "double click"
   *       semantics) → expand to the original full content in the textarea
   *  Uses `event.detail` (the click count) which the browser computes with
   *  its own double-click timing — fully cross-browser (Chrome, Electron,
   *  Safari, Firefox all agree) and no ref/selection tracking required. */
  const handleTextareaClick = useCallback((e: React.MouseEvent<HTMLTextAreaElement>) => {
    if (!onPasteBlocksChange || !pasteBlocks.length) return
    const ta = e.currentTarget
    const caret = ta.selectionStart ?? 0
    const range = tokenRangeAt(value, pasteBlocks, caret)
    if (!range) return

    if (e.detail < 2) {
      // First click in a (potential) sequence — highlight the token as an
      // atomic range. If the user doesn't click again within the browser's
      // double-click window, nothing else happens.
      requestAnimationFrame(() => {
        const el = inputRef.current
        if (el) el.setSelectionRange(range.start, range.end)
      })
      return
    }

    // e.detail >= 2 — second (or more) click in a rapid sequence on the
    // same region — expand.
    const expanded = value.slice(0, range.start) + range.block.content + value.slice(range.end)
    onChange(expanded)
    onPasteBlocksChange(pasteBlocks.filter(b => b.id !== range.block.id))
    requestAnimationFrame(() => {
      if (ta) {
        const pos = range.start + range.block.content.length
        ta.setSelectionRange(pos, pos)
        ta.focus()
      }
    })
  }, [value, pasteBlocks, onPasteBlocksChange, onChange])

  /** Snap selection endpoints that land inside a token range to the nearest edge.
   *  Covers drag-select that ends mid-token, touch/long-press handles on mobile,
   *  and any other non-keyboard way selection could split a token. */
  const handleSelectSnap = useCallback(() => {
    recordCaret()
    if (!pasteBlocks.length) return
    const ta = inputRef.current
    if (!ta) return
    const ss = ta.selectionStart ?? 0
    const se = ta.selectionEnd ?? 0
    // Collapsed caret inside a token is handled by the click expander — skip.
    if (ss === se) return
    const ranges = findTokenRanges(ta.value, pasteBlocks)
    if (!ranges.length) return
    const snap = (pos: number) => {
      for (const r of ranges) {
        if (pos > r.start && pos < r.end) {
          // Snap to the nearer edge (ties go to the start).
          return pos - r.start <= r.end - pos ? r.start : r.end
        }
      }
      return pos
    }
    const newSs = snap(ss)
    const newSe = snap(se)
    if (newSs === ss && newSe === se) return
    const dir = ta.selectionDirection || 'forward'
    ta.setSelectionRange(Math.min(newSs, newSe), Math.max(newSs, newSe), dir as 'forward' | 'backward' | 'none')
  }, [pasteBlocks, recordCaret])

  /** Prune paste blocks whose token was deleted from the textarea. */
  useEffect(() => {
    if (!onPasteBlocksChange || !pasteBlocks.length) return
    const pruned = pruneBlocks(value, pasteBlocks)
    if (pruned !== pasteBlocks) onPasteBlocksChange(pruned)
  }, [value, pasteBlocks, onPasteBlocksChange])

  /** Copy/cut that spans one or more collapsed-paste tokens writes the
   *  expanded content to the clipboard instead of the literal token text.
   *  Without this, pasting elsewhere yields "[ Paste #1 · 5 lines ]"
   *  zombie strings that look like chips but have no backing block. Only
   *  tokens *fully* covered by the selection are expanded; partial overlaps
   *  fall back to the literal slice (rare — drag-select snaps to token
   *  edges via handleSelectSnap). */
  const expandSelectionForClipboard = useCallback(
    (start: number, end: number): string | null => {
      if (!pasteBlocks.length || start === end) return null
      const ranges = findTokenRanges(value, pasteBlocks)
      const covered = ranges.filter(r => r.start >= start && r.end <= end)
      if (!covered.length) return null
      let out = ''
      let cursor = start
      for (const r of covered) {
        out += value.slice(cursor, r.start)
        out += r.block.content
        cursor = r.end
      }
      out += value.slice(cursor, end)
      return out
    },
    [value, pasteBlocks],
  )

  const handleCopy = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const ta = e.currentTarget
    const expanded = expandSelectionForClipboard(ta.selectionStart ?? 0, ta.selectionEnd ?? 0)
    if (expanded === null) return
    e.clipboardData.setData('text/plain', expanded)
    e.preventDefault()
  }, [expandSelectionForClipboard])

  const handleCut = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const ta = e.currentTarget
    const start = ta.selectionStart ?? 0
    const end = ta.selectionEnd ?? 0
    const expanded = expandSelectionForClipboard(start, end)
    if (expanded === null) return
    e.clipboardData.setData('text/plain', expanded)
    // Manually excise the selection from the textarea; the pruneBlocks
    // effect above will drop any blocks whose token text was removed.
    const nextValue = value.slice(0, start) + value.slice(end)
    onChange(nextValue)
    requestAnimationFrame(() => {
      if (ta) ta.setSelectionRange(start, start)
    })
    e.preventDefault()
  }, [expandSelectionForClipboard, value, onChange])

  const handleFileInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || [])
    if (files.length && onUploadFiles) onUploadFiles(files)
    e.target.value = '' // reset so same file can be re-selected
  }, [onUploadFiles])

  const hasFiles = pendingFiles.length > 0
  const prevHadFiles = useRef(hasFiles)
  const dragMinH = hasFiles ? INPUT_DRAG_MIN_H + FILE_PREVIEW_H : INPUT_DRAG_MIN_H
  const dragMinHRef = useRef(dragMinH)
  dragMinHRef.current = dragMinH
  // Adjust height transiently when file strip appears/disappears (not persisted — files are session-only)
  useLayoutEffect(() => {
    const wasShowingFiles = prevHadFiles.current
    prevHadFiles.current = hasFiles
    if (wasShowingFiles && !hasFiles) {
      setManualHeight(h => h !== null ? Math.max(INPUT_DRAG_MIN_H, h - FILE_PREVIEW_H) : h)
    } else if (!wasShowingFiles && hasFiles) {
      setManualHeight(h => h !== null ? h + FILE_PREVIEW_H : h)
    }
  }, [hasFiles])

  return (
    // 'input-area' is a stable theming hook — see website/docs/theming-contract.md
    <div className={`input-area px-5 pb-1 ${hasApproval ? 'pt-0' : 'pt-1'} mx-auto w-full flex flex-col`}
      style={{ maxWidth: 'var(--mc-input-width, 900px)', ...(manualHeight !== null ? { minHeight: (pendingFiles.length > 0 ? INPUT_DRAG_MIN_H + FILE_PREVIEW_H : INPUT_DRAG_MIN_H) + 'px' } : {}) }}>

      {aboveComposer}

      {/* Knowledge context chip */}
      {!showGhost && knowledgeChip}

      {/* Ghost follow-up bubbles floating above input */}
      {!showGhost && followUpOptions && followUpOptions.length > 0 && onFollowUpSelect && (
          <FollowUpBar options={followUpOptions} picked={followUpPicked ?? new Set()} onSelect={onFollowUpSelect} onSend={sendFollowUp} quickSend={quickSend} layout={followUpLayout} />
      )}

      {/* Drag handle — always visible, sits above approval bar or input */}
      {/* Pointer-drag resize handle for the message input (double-click resets).
          Resize is a pure visual enhancement — the textarea already auto-sizes to
          its content and there is no per-pixel keyboard resize gesture — so the
          handle is aria-hidden and carries no interactive semantics. */}
      {!showGhost && <div
        aria-hidden="true"
        className="flex items-center justify-center h-[6px] cursor-row-resize group/drag"
        style={{ touchAction: 'none' }}
        {...inputResize}
        onDoubleClick={resetHeight}
        title={i18nT('components.chatInput.drag_to_resize_double_click_to_reset')}
      >
        <div className="w-12 h-[3px] rounded-full bg-border group-hover/drag:bg-accent group-active/drag:bg-accent-hover transition-all duration-200 opacity-0 group-hover/drag:opacity-100" />
      </div>}

      {/* Sub-agent spawn-approval banner — a top-level signal that one or more
       *  sub-agents are queued awaiting the user's approval to run, with inline
       *  Approve/Reject so the decision can be made without leaving the
       *  composer. Single pending → a compact one-line row. Multiple → header
       *  Approve all / Reject all plus a per-agent row (task + Approve/Reject)
       *  so one can run while another is rejected. "Review in panel" opens the
       *  Subagents tab. Not a single <button> wrapper — every control is its
       *  own button. */}
      <AnimatePresence>
        {pendingSpawnApprovals.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300, mass: 0.8 }}
          >
            <div className="w-full bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] border border-border rounded-2xl mb-2 approval-glow">
              <div className="flex items-center gap-1.5 px-3.5 py-2.5 select-none flex-wrap">
                <Bot size={13} className="text-warn shrink-0" />
                <span className="text-[13px] font-body text-muted flex-1 min-w-0">
                  {pendingSpawnApprovals.length === 1
                    ? '1 sub-agent is awaiting your approval to run'
                    : `${pendingSpawnApprovals.length} sub-agents are awaiting your approval to run`}
                </span>
                {spawnApprovalsResolving ? (
                  <span className="inline-flex items-center gap-1 text-[12px] text-muted/60 shrink-0">
                    <Loader2 size={12} className="animate-spin shrink-0" />{i18nT('components.chatInput.resolving')}
                  </span>
                ) : (
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      type="button"
                      onClick={() => resolveSpawnApprovals('approve')}
                      className={approvalBtnClass}
                    >
                      <CheckCircle size={12} className="shrink-0" />
                      {pendingSpawnApprovals.length === 1 ? i18nT('components.chatInput.approve') : i18nT('components.chatInput.approve_all')}
                    </button>
                    <button
                      type="button"
                      onClick={() => resolveSpawnApprovals('reject')}
                      className={`${approvalBtnClass} hover:!text-danger hover:!border-danger`}
                    >
                      <Ban size={12} className="shrink-0" />
                      {pendingSpawnApprovals.length === 1 ? i18nT('components.chatInput.reject') : i18nT('components.chatInput.reject_all')}
                    </button>
                    <button
                      type="button"
                      onClick={reviewSpawnApprovals}
                      className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-text shrink-0 cursor-pointer bg-transparent border-none px-1"
                    >
                      <Target size={11} className="shrink-0" />{i18nT('components.chatInput.review_in_panel')}
                    </button>
                  </div>
                )}
              </div>
              {/* Per-agent rows — only when more than one is pending, so a single
               *  spawn stays a compact one-liner. Each row resolves just its own
               *  sub-agent via resolveOneSpawn. */}
              {pendingSpawnApprovals.length > 1 && (
                <div className="px-3.5 pb-2.5 flex flex-col gap-1.5">
                  {pendingSpawnApprovals.map(a => (
                    <div key={a.id} className="flex items-center gap-2 rounded-lg border border-border/60 bg-bg/40 px-2.5 py-1.5">
                      <code className="text-[11px] font-mono text-muted/80 flex-1 min-w-0 truncate" title={a.task || a.agent || a.id}>
                        {a.task || a.agent || a.id}
                      </code>
                      {a.approving ? (
                        <span className="inline-flex items-center gap-1 text-[11px] text-muted/60 shrink-0">
                          <Loader2 size={11} className="animate-spin shrink-0" />{i18nT('components.chatInput.resolving')}
                        </span>
                      ) : (
                        <div className="flex items-center gap-1 shrink-0">
                          <button
                            type="button"
                            aria-label={i18nT('components.chatInput.approve_sub_agent', { name: a.task || a.agent || a.id })}
                            onClick={() => resolveOneSpawn(a, 'approve')}
                            className={approvalBtnClass}
                          >
                            <CheckCircle size={12} className="shrink-0" />{i18nT('components.chatInput.approve')}
                          </button>
                          <button
                            type="button"
                            aria-label={i18nT('components.chatInput.reject_sub_agent', { name: a.task || a.agent || a.id })}
                            onClick={() => resolveOneSpawn(a, 'reject')}
                            className={`${approvalBtnClass} hover:!text-danger hover:!border-danger`}
                          >
                            <Ban size={12} className="shrink-0" />{i18nT('components.chatInput.reject')}
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Approval bar — always-visible button row, with a "ghost pill"
       *  detail mirror that grows in when the inline pill scrolls out of
       *  viewport. Buttons stay anchored on the same row across both states
       *  for stable muscle memory.
       *
       *  Two stacked <AnimatePresence>s:
       *    outer  → mounts/unmounts the whole bar with the approval lifecycle
       *    inner  → toggles the ghost pill based on inline-pill viewport state
       */}
      <AnimatePresence>
        {pendingApproval && approvalId && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300, mass: 0.8 }}
          >
          <div className={`bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] border border-border ${showGhost ? 'rounded-2xl' : 'border-b-0 rounded-t-2xl'} approval-glow transition-[border-radius,border-color,border-width] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]`}>
              <AnimatePresence initial={false}>
                  {showGhost && (
                      <motion.div
                          key="ghost"
                          initial={{ height: 0, opacity: 0, y: -6 }}
                          animate={{ height: 'auto', opacity: 1, y: 0 }}
                          exit={{ height: 0, opacity: 0, y: -6 }}
                          transition={{ type: 'spring', damping: 24, stiffness: 280, mass: 0.7 }}
                          style={{ overflow: 'hidden' }}
                      >
                          <div className="px-3.5 pt-2.5 pb-1">
                              <div className="inline-flex items-start gap-1 text-[13px] font-mono px-2 py-0.5">
                                  <Lock size={12} className="text-warn shrink-0" style={{ marginTop: '3px' }} />
                                  <span className="text-muted break-words min-w-0 line-clamp-2">{approvalLabel}</span>
                              </div>
                              <ToolDetails
                                  purpose={approvalPurpose}
                                  pillLabel={approvalLabel}
                                  toolName={approvalLabelRaw}
                                  input={approvalToolInput}
                                  output=""
                                  auto={false}
                                  pending={true}
                                  ts={approvalTs}
                                  hasEntry={!!approvalToolInput}
                                  fmtTime={t => t ? fmtDateFields(t, { hour: '2-digit', minute: '2-digit' }) : ''}
                                  barColor="color-mix(in srgb, var(--warn) 70%, transparent)"
                                  layoutId={`ghost-tool-detail-${approvalToolCallId || approvalId}`}
                                  compact
                              />
                          </div>
                          <div className="mx-3.5 h-px bg-[color-mix(in_srgb,var(--warn)_25%,transparent)]" />
                      </motion.div>
                  )}
              </AnimatePresence>
              <div className="flex items-center gap-1.5 px-3.5 py-2.5 select-none flex-wrap">
                  {!showGhost && <>
                      <Lock size={12} className="text-warn shrink-0" />
                      <span className="text-[13px] font-mono text-muted truncate flex-1 min-w-0">{approvalLabel}</span>
                  </>}
                  {showGhost && <div className="flex-1 min-w-0" />}
                  {showGhost && approvalToolCallId && (
                      <button
                          type="button"
                          onClick={showInChat}
                          title={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                          aria-label={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                          className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-transparent border border-border text-muted text-[11px] cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-colors"
                      >
                          <Target size={11} className="shrink-0" />
                          {i18nT('components.chatInput.show_in_chat')}
                      </button>
                  )}
                  <div className="flex gap-1.5 flex-wrap items-center">
                      <button disabled={approvalSubmitting} className={approvalBtnClass} onClick={() => handleApprovalAction('approved')}><CheckCircle size={12} className="shrink-0" />{i18nT('components.chatInput.allow_once')}</button>
                      {approvalIsReadOnly && !approvalIsUnattended && <button disabled={approvalSubmitting} className={approvalBtnClass} onClick={() => handleApprovalAction('trust_reads')}><BookOpen size={12} className="shrink-0" />{i18nT('components.chatInput.trust_reads')}</button>}
                      {!approvalIsUnattended && (
                        <TrustDropdown
                            fullCommand={approvalFullCommand || approvalLabelRaw}
                            baseCommand={approvalBaseCommand || approvalLabelRaw.split(/\s+/)[0] || ''}
                            isShell={approvalIsShell}
                            disabled={approvalSubmitting}
                            className={approvalBtnClass}
                            onAction={(action, pattern) => { handleApprovalAction(action, pattern) }}
                        />
                      )}
                      <button disabled={approvalSubmitting} className={`${approvalBtnClass} hover:!text-danger hover:!bg-[color-mix(in_srgb,var(--danger)_10%,transparent)]`} onClick={() => handleApprovalAction('rejected')}><Ban size={12} className="shrink-0" />{i18nT('components.chatInput.reject')}</button>
                  </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {approvalNotice && (
        <div
          role="status"
          className="flex items-center gap-2 px-4 py-2 mb-1 bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] rounded-lg"
        >
          <Lock size={12} className="text-warn shrink-0" />
          <span className="text-muted text-[13px]">{approvalNotice}</span>
        </div>
      )}

      {!showGhost && prefillHint && (
        <div className="flex items-center gap-2 px-4 py-2 mb-1 bg-accent/10 rounded-lg">
          <span className="text-accent text-[13px]"><ClipboardList className="lucide-inline" /> {i18nT('components.chatInput.plan_pre_filled_add_context_then_send')}</span>
        </div>
      )}

      <input ref={fileInputRef} type="file" aria-label={i18nT('components.chatInput.attach_files')} multiple accept={FILE_ACCEPT} className="hidden" onChange={handleFileInputChange} />

      <SlashCommandMenu input={value} anchorRef={inputRef as React.RefObject<HTMLElement>} open={slashMenuOpen} onSelect={cmd => { onChange(cmd); setSlashMenuOpen(false) }} onClose={() => setSlashMenuOpen(false)} />

      {onFileSelect && (
        <FilePickerMenu
          query={fileQuery}
          anchorRef={inputRef as React.RefObject<HTMLElement>}
          open={filePickerOpen}
          project={project}
          onFileOpen={onFileOpen}
          onSelect={({ path, relativePath }) => {
            applyPickedToken(/(^|[\s])@\S*$/, `@${relativePath} `)
            setFilePickerOpen(false); setFileQuery('')
            onFileSelect(path)
          }}
          onClose={() => { setFilePickerOpen(false); setFileQuery('') }}
        />
      )}

      <SkillPickerMenu
        query={skillQuery}
        anchorRef={inputRef as React.RefObject<HTMLElement>}
        open={skillPickerOpen}
        onSelect={({ leaf }) => {
          // Token left literal — backend appends the skill body; the user still
          // sees their $token marker. Caret-relative replace via shared helper.
          applyPickedToken(/(^|[\s])\$[a-z0-9/_-]*$/, `$${leaf} `)
          setSkillPickerOpen(false); setSkillQuery('')
        }}
        onClose={() => { setSkillPickerOpen(false); setSkillQuery('') }}
      />

      {/* Unified input container — drag-to-resize targets the inner div. */}
      {/* The composer's SHOWN state is initial === animate ({opacity:1,height:auto}),
          so entering it requires NO animation and it can never be stranded
          invisible. Only the transient collapse toward the approval "ghost" bar
          animates (exit -> {opacity:0,height:0}); any re-entry cancels that exit
          and snaps straight back to the shown state. An enter that animated from
          {opacity:0,height:0} to height:auto could be interrupted (e.g. an approval
          resolving while the chat tab is backgrounded, so requestAnimationFrame is
          throttled and the completion that restores height:auto never runs),
          stranding the motion.div at height:0/opacity:0 and hiding the input until
          a remount. Keeping the unmount-while-ghost behavior also means the
          collapsed composer is never a persistently focusable invisible element. */}
      <AnimatePresence initial={false}>
      {!showGhost && (<motion.div
        key="input-container"
        initial={{ opacity: 1, height: 'auto' }}
        animate={{ opacity: 1, height: 'auto' }}
        exit={{ opacity: 0, height: 0 }}
        transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }}
        style={{ overflow: 'hidden' }}
      >{/* File drag-and-drop target. Drag-drop is inherently pointer-only; the
           keyboard-accessible path is the "Attach files" button that opens the
           hidden file input above. Hence the scoped disable for the drop zone. */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        data-testid="input-wrapper"
        ref={wrapperRef}
        className={`${hasApproval ? 'rounded-b-2xl rounded-t-none' : 'rounded-2xl'} relative transition-colors overflow-hidden ${manualHeight !== null ? 'flex flex-col min-h-0' : ''} ${(cleanMode || memoryMode === 'incognito' || memoryMode === 'temporary') ? 'border-2' : 'border'} ${dragOver ? 'border-accent bg-accent/10' : cleanMode ? 'border-accent bg-bg-elevated' : memoryMode === 'temporary' ? 'border-aim bg-bg-elevated' : memoryMode === 'incognito' ? 'border-warn bg-bg-elevated' : 'border-border bg-bg-elevated focus-within:border-accent/50'}`}

        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <FilePreviewStrip files={pendingFiles} resizedInfo={resizedInfo} onRemove={onRemoveFile} />

        {showDictation ? (
          <VoiceDictationPanel sampleRef={showDictation} value={value} partial={voicePartial} deviceLabel={voiceDeviceLabel} onSelectDevice={onSelectVoiceDevice || noopSelectDevice} deviceSwitchIsLive={voiceDeviceSwitchIsLive} streaming={voiceStreaming} />
        ) : (
          <VoiceStatusBar recording={voiceRecording} level={voiceLevel} deviceLabel={voiceDeviceLabel} error={voiceError} onDismissError={onClearVoiceError} onSelectDevice={onSelectVoiceDevice || noopSelectDevice} deviceSwitchIsLive={voiceDeviceSwitchIsLive} />
        )}

        {optimizing && <span className="absolute inset-0 flex items-start px-4 pt-3 text-sm text-white font-medium pointer-events-none z-10 bg-black/60 rounded-2xl"><Sparkles size={14} className="inline mr-1 text-yellow-400" /> {i18nT('components.chatInput.optimizing_prompt')}</span>}
        <div className={`relative ${showDictation ? 'sr-only' : ''} ${manualHeight !== null ? 'flex-1 min-h-0 flex flex-col' : ''}`}>
        <PasteHighlightLayer ref={mirrorRef} value={value} blocks={pasteBlocks} />
        <textarea
          ref={inputRef}
          aria-label={i18nT('components.chatInput.message_input')}
          className={`relative w-full bg-transparent border-none ${INPUT_TYPO} text-text outline-none min-h-[44px] max-h-[50vh] placeholder:text-muted resize-none ${manualHeight !== null ? 'flex-1' : ''} ${disabled ? 'opacity-40 pointer-events-none' : ''} ${optimizing ? 'opacity-30' : ''}`}
          style={manualHeight !== null ? { height: '100%' } : undefined}
          placeholder={!connected ? i18nT('components.chatInput.gateway_offline_message_will_not_send') : disabledProp ? i18nT('components.chatInput.stopping') : voiceRecording ? i18nT('components.chatInput.recording_click_mic_to_stop') : voiceTranscribing ? i18nT('components.chatInput.transcribing_please_wait') : continuePlaceholder || resolvedPlaceholder}
          readOnly={optimizing}
          rows={1}
          value={value}
          onDragOver={e => { e.preventDefault(); onDragOver?.(e); e.stopPropagation() }}
          onDragLeave={e => { onDragLeave?.(e); e.stopPropagation() }}
          onDrop={e => { e.preventDefault(); onDrop?.(e); e.stopPropagation() }}
          onChange={e => {
            valueFromUserRef.current = true // real DOM edit, not a parent-driven draft restore
            const val = e.target.value; onChange(val); setSlashMenuOpen(val.startsWith('/'))
            // Anchor @/$ detection to the token being edited AT THE CARET, not the
            // end of the whole input. `before` ends at the caret, so a match means
            // "the token ends where my cursor is" — which makes both pickers fire
            // mid-sentence and when trailing text/newlines follow the token.
            // Matchers live in composerTokens.ts (unit-tested there).
            const before = val.slice(0, e.target.selectionStart ?? val.length)
            const fileQ = onFileSelect ? matchFileToken(before) : null
            if (fileQ !== null) { setFilePickerOpen(true); setFileQuery(fileQ) }
            else { setFilePickerOpen(false); setFileQuery('') }
            // $ and @ are mutually exclusive (a token starts with one sigil); @ wins.
            const skillQ = fileQ === null ? matchSkillToken(before) : null
            if (skillQ !== null) { setSkillPickerOpen(true); setSkillQuery(skillQ) }
            else { setSkillPickerOpen(false); setSkillQuery('') }
            recordCaret()
          }}
          onKeyDown={handleKeyDown}
          {...ime.composition}
          onPaste={handlePaste}
          onCopy={handleCopy}
          onCut={handleCut}
          onClick={handleTextareaClick}
          onFocus={prefetchSkills}
          onMouseUp={handleSelectSnap}
          onSelect={handleSelectSnap}
          onInput={handleInput}
          onScroll={e => { if (mirrorRef.current) mirrorRef.current.scrollTop = e.currentTarget.scrollTop }}
        />
        </div>

        {/* Bottom icon row */}
        <div className="flex items-center justify-between px-2.5 pb-2 pt-0.5">
          <div className="flex items-center gap-0.5 min-w-0">
            {onUploadFiles && (
              <div className="relative shrink-0" ref={plusWrapRef}>
                <button
                  ref={plusBtnRef}
                  className={`w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all disabled:opacity-30 bg-transparent border-none ${plusOpen ? 'text-text bg-bg-hover' : 'text-muted hover:text-text hover:bg-bg-hover'}`}
                  onClick={togglePlus}
                  disabled={uploading}
                  aria-haspopup="menu"
                  aria-expanded={plusOpen}
                  aria-label={i18nT('components.chatInput.add_files_options')}
                  title={i18nT('components.chatInput.add_files_options')}
                >
                  {uploading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} className={`transition-transform ${plusOpen ? 'rotate-45' : ''}`} />}
                </button>
                {plusOpen && plusRect && createPortal(
                  <div
                    ref={plusMenuRef}
                    className="fixed w-[260px] rounded-xl bg-bg-elevated border border-border shadow-xl p-2 animate-slide-up z-[60]"
                    style={{ left: Math.max(8, Math.min(plusRect.left, window.innerWidth - 260 - 8)), bottom: window.innerHeight - plusRect.top + 8 }}
                  >
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => openPicker(false)}
                        className="flex-1 flex flex-col items-center gap-1.5 px-2 py-3 rounded-lg border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong transition-all cursor-pointer"
                      >
                        <FileText size={18} className="text-muted" />
                        <span className="text-[12px] font-medium text-text">{i18nT('components.chatInput.upload_file')}</span>
                      </button>
                      {(isScreenSnipSupported() || isMac) && !isMobile && onScreenshot && (
                        <button
                          type="button"
                          onClick={() => { setPlusOpen(false); onScreenshot() }}
                          className="flex-1 flex flex-col items-center gap-1.5 px-2 py-3 rounded-lg border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong transition-all cursor-pointer"
                        >
                          <Crop size={18} className="text-muted" />
                          <span className="text-[12px] font-medium text-text">{i18nT('components.chatInput.screenshot')}</span>
                        </button>
                      )}
                    </div>
                    {/* In-input trigger shortcuts: clicking inserts the sigil
                     *  and opens the matching picker (same as typing /, @, $). */}
                    <div className="mt-2 pt-2 border-t border-border flex flex-col gap-0.5">
                      <button
                        type="button"
                        onClick={() => openTrigger('/')}
                        title={i18nT('components.chatInput.slash_commands')}
                        className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                      >
                        <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">/</span>
                        <div className="min-w-0">
                          <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.command')}</div>
                          <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.quick_actions_like_clearing_the_chat_or_checking')}</div>
                        </div>
                      </button>
                      {onFileSelect && (
                        <button
                          type="button"
                          onClick={() => openTrigger('@')}
                          title={i18nT('components.chatInput.reference_a_file')}
                          className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                        >
                          <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">@</span>
                          <div className="min-w-0">
                            <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.file')}</div>
                            <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.let_the_agent_read_one_of_your_files')}</div>
                          </div>
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => openTrigger('$')}
                        title={i18nT('components.chatInput.use_a_skill')}
                        className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                      >
                        <span className="w-4 text-center text-[14px] font-mono leading-none text-muted shrink-0">$</span>
                        <div className="min-w-0">
                          <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.skill')}</div>
                          <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.apply_a_ready_made_set_of_instructions')}</div>
                        </div>
                      </button>
                    </div>
                  </div>,
                  document.body
                )}
              </div>
            )}
            <div className="flex items-center gap-0.5 min-w-0 overflow-x-auto flex-1">

              {onAutoNudgeClick && (
                <AutoNudgePopover
                  slotKey={slotId || ''}
                  loop={autoNudgeLoop || null}
                  open={autoNudgeOpen || false}
                  onOpenChange={v => onAutoNudgeClick(v)}
                  onChange={onAutoNudgeChange || (() => {})}
                />
              )}
              {!isMobile && approvalMode && (
                <ApprovalModePicker mode={approvalMode} slotKey={activeSlot || ''} />
              )}
            </div>
            {isMobile && approvalMode && (
              <ApprovalModePicker mode={approvalMode} slotKey={activeSlot || ''} compact />
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {onVoiceToggle && (
              <button
                className={`w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all border-none ${
                  voiceRecording ? 'bg-danger-subtle text-danger animate-pulse' : voiceTranscribing ? 'bg-accent-subtle text-accent' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'
                } disabled:opacity-30`}
                onClick={onVoiceToggle}
                onPointerDown={onVoicePrewarm}
                disabled={disabled || voiceTranscribing || optimizing}
                aria-label={voiceRecording ? i18nT('components.chatInput.stop_recording') : voiceTranscribing ? i18nT('components.chatInput.transcribing') : i18nT('components.chatInput.voice_input')}
                title={voiceRecording ? i18nT('components.chatInput.stop_recording') : voiceTranscribing ? i18nT('components.chatInput.transcribing') : i18nT('components.chatInput.voice_input')}
              >
                {voiceTranscribing ? <Loader2 size={18} className="animate-spin" /> : <Mic size={18} />}
              </button>
            )}
            {(isRunning || stopState === 'soft_pending' || stopState === 'killing') && onStop ? (
              stopState === 'killing' ? (
                killingEscaped ? (
                  <div className="flex items-center gap-1.5">
                    <button
                      className="w-8 h-8 rounded-lg bg-danger text-danger-fg border-none flex items-center justify-center cursor-pointer hover:bg-danger/80 transition-all"
                      onClick={onStop}
                      title={i18nT('components.chatInput.force_reset_taking_longer_than_expected')}
                      aria-label={i18nT('components.chatInput.force_reset_session_taking_longer_than_expected')}
                      data-testid="stop-button-escape-hatch"
                    >
                      <Square size={18} fill="currentColor" />
                    </button>
                    <span className="text-xs text-muted whitespace-nowrap" data-testid="stop-escape-hint">{i18nT('components.chatInput.taking_longer_than_expected')}</span>
                  </div>
                ) : (
                  <button className="w-8 h-8 rounded-lg bg-danger text-danger-fg border-none flex items-center justify-center cursor-not-allowed transition-all" disabled title={i18nT('components.chatInput.killing')} aria-label={i18nT('components.chatInput.killing_session')} data-testid="stop-button-killing">
                    <Loader2 size={18} className="animate-spin" />
                  </button>
                )
              ) : stopState === 'soft_pending' ? (
                <div className="flex items-center gap-1.5">
                  <motion.button
                    className="w-8 h-8 rounded-lg bg-transparent border-none text-danger hover:bg-danger/10 flex items-center justify-center cursor-pointer transition-all"
                    onClick={onStop}
                    title={i18nT('components.chatInput.force_kill_discards_in_progress_work_and_queued')}
                    aria-label={i18nT('components.chatInput.force_kill_session_discards_in_progress_work_and')}
                    animate={{ opacity: [0.6, 1, 0.6] }}
                    transition={{ duration: 1.2, repeat: Infinity }}
                    data-testid="stop-button-pulsing"
                  >
                    <Square size={18} fill="currentColor" />
                  </motion.button>
                  <span className="text-xs text-muted whitespace-nowrap" data-testid="stop-force-hint">{i18nT('components.chatInput.click_again_to_force_stop')}</span>
                </div>
              ) : isQueued ? (
                <button className="w-8 h-8 rounded-full bg-warn text-warn-fg border-none flex items-center justify-center cursor-pointer hover:bg-warn/80 transition-all" onClick={onStop} title={i18nT('components.chatInput.stopping')} aria-label={i18nT('components.chatInput.stopping_2')}>
                  <Loader2 size={18} className="animate-spin" />
                </button>
              ) : value.trim() || pendingFiles.length ? (
                canSteer && onSteer ? (
                  /* Split send button (mock: [ action | ▾ ]) — main area fires the
                   * selected busy-send mode (steer by default, same as Enter);
                   * the chevron opens a dropdown to switch modes (persisted). */
                  <div className="relative flex items-center" ref={busySplitRef}>
                    <div className={`flex items-stretch h-8 rounded-full overflow-hidden transition-colors ${busySendMode === 'steer' ? 'bg-accent text-accent-fg' : 'bg-warn text-warn-fg'}`}>
                      <button
                        className="w-8 h-8 bg-transparent border-none flex items-center justify-center cursor-pointer hover:bg-black/15 transition-all text-inherit"
                        onClick={fireComposer}
                        disabled={disabled}
                        title={busySendMode === 'steer' ? i18nT('components.chatInput.steer_inject_into_the_running_turn_enter') : i18nT('components.chatInput.queue_run_after_the_current_turn_finishes_enter')}
                        aria-label={busySendMode === 'steer' ? i18nT('components.chatInput.steer') : i18nT('components.chatInput.queue_message')}
                        data-testid="busy-send-button"
                      >
                        {busySendMode === 'steer' ? <Target size={16} /> : <ArrowUpFromLine size={16} />}
                      </button>
                      <div className="w-px my-1.5 bg-current opacity-40" aria-hidden="true" />
                      <button
                        ref={busyCaretRef}
                        className="w-6 h-8 bg-transparent border-none flex items-center justify-center cursor-pointer hover:bg-black/15 transition-all text-inherit"
                        onClick={toggleBusyMenu}
                        aria-haspopup="menu"
                        aria-expanded={busyMenuOpen}
                        aria-label={i18nT('components.chatInput.send_options')}
                        title={i18nT('components.chatInput.send_options')}
                        data-testid="busy-send-caret"
                      >
                        <ChevronDown size={14} className={`transition-transform ${busyMenuOpen ? 'rotate-180' : ''}`} />
                      </button>
                    </div>
                    {busyMenuOpen && busyMenuRect && createPortal(
                      <div
                        ref={busyMenuRef}
                        role="menu"
                        onKeyDown={onBusyMenuKeyDown}
                        className="fixed w-[250px] rounded-xl bg-bg-elevated border border-border shadow-xl p-1.5 animate-slide-up z-[60]"
                        style={{ left: Math.max(8, Math.min(busyMenuRect.right - 250, window.innerWidth - 250 - 8)), bottom: window.innerHeight - busyMenuRect.top + 8 }}
                      >
                        {BUSY_SEND_MODES.map(({ mode, icon }) => (
                          <button
                            key={mode}
                            role="menuitemradio"
                            aria-checked={busySendMode === mode}
                            data-option=""
                            tabIndex={-1}
                            onClick={() => selectBusyMode(mode)}
                            className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover focus:bg-bg-hover focus:outline-none transition-colors cursor-pointer text-left border-none"
                            data-testid={`busy-send-mode-${mode}`}
                          >
                            <span className={`shrink-0 ${mode === 'steer' ? 'text-accent' : 'text-warn'}`}>{icon}</span>
                            <div className="min-w-0 flex-1">
                              <div className="text-[12px] font-medium text-text">{i18nT(BUSY_SEND_MODE_LABEL_KEY[mode])}</div>
                              <div className="text-[11px] text-muted leading-snug">{i18nT(BUSY_SEND_MODE_DESC_KEY[mode])}</div>
                            </div>
                            {busySendMode === mode && <Check size={14} className="text-accent shrink-0" />}
                          </button>
                        ))}
                      </div>,
                      document.body
                    )}
                  </div>
                ) : (
                  <button className="w-8 h-8 rounded-full bg-warn text-warn-fg border-none flex items-center justify-center cursor-pointer hover:bg-warn/80 disabled:opacity-30 disabled:cursor-not-allowed transition-all" onClick={fireComposer} disabled={disabled} title={i18nT('components.chatInput.queue_message')} aria-label={i18nT('components.chatInput.queue_message')}>
                    <ArrowUpFromLine size={18} />
                  </button>
                )
              ) : (
                <button className="w-8 h-8 rounded-lg bg-transparent border-none text-danger hover:bg-danger/10 flex items-center justify-center cursor-pointer transition-all" onClick={onStop} title={i18nT('components.chatInput.stop_generation')} aria-label={i18nT('components.chatInput.stop_generation')} data-testid="stop-button-armed">
                  <Square size={18} fill="currentColor" />
                </button>
              )
            ) : (<>
              <button
                className={`w-8 h-8 rounded-lg border-none flex items-center justify-center cursor-pointer transition-all disabled:cursor-not-allowed ${optimizing ? 'bg-accent/20 text-accent animate-pulse' : 'bg-transparent text-muted hover:text-accent hover:bg-accent/10 disabled:opacity-40 disabled:hover:text-muted disabled:hover:bg-transparent'}`}
                onClick={(e) => { e.stopPropagation(); e.preventDefault(); optimizePrompt() }}
                // A single mutation backs this instance, so only one optimize can
                // run at a time. Disable on the RAW pending flag (not the
                // slot-scoped `optimizing`) so the button also reads as busy on a
                // *different* session while the originating session's optimize is
                // still in flight — matching the re-entrancy guard in
                // optimizePrompt(). optimizing ⊂ optimizePending, so this stays
                // disabled on the originating session too.
                disabled={!value.trim() || optimizePending || !connected}
                aria-label={optimizePending && !optimizing ? i18nT('components.chatInput.optimize_prompt_busy_optimizing_another_chat') : i18nT('components.chatInput.optimize_prompt')}
                title={optimizePending && !optimizing ? i18nT('components.chatInput.optimizing_another_chat_please_wait') : i18nT('components.chatInput.optimize_prompt_2', { shortcut: platformShortcut('Cmd+Shift+Enter') })}
                {...offlineProps(connected, 'optimize', 'Optimize')}
              >
                {optimizing ? <Loader2 size={16} className="animate-spin" /> : <Sparkles size={16} />}
              </button>
              {/* 'primary' is a stable theming hook (button.primary) — see website/docs/theming-contract.md */}
              {/*
                Sixth state of this button. The first five are send / stop /
                queue / steer / disabled; this one claims the ONE state that was
                previously dead weight — an empty composer on a slot whose last
                turn was cut off. Pressing it hands the thread back to the agent
                instead of sending nothing. The moment the user types a character
                the arrow and the send action come back, so the control never
                carries two meanings at once.

                Labeled, not an icon: this is the only control in the row whose
                action a first-time user cannot infer from its glyph. A bare ▶
                reads as "resume paused media", which is the wrong model — the
                agent is not paused, it is being asked for another turn — and an
                icon-only button puts that correction in a tooltip, which does
                not exist on touch. The word carries it instead, and RotateCw
                replaces Play so the glyph stops promising playback. Widening to
                a pill is deliberate: at 32px round it was pixel-identical to
                Send, so the two most consequential buttons in the composer
                differed only by the symbol inside them.

                The visible text is also the accessible name — no aria-label,
                which would override the label a sighted user reads and break
                WCAG 2.5.3 (Label in Name). `title` carries the longer
                explanation for hover.
              */}
              {continuable && onContinue && !value.trim() && !pendingFiles.length ? (
                <button
                  className="primary h-8 px-3 rounded-full bg-accent text-accent-fg border-none inline-flex items-center gap-1.5 text-[12px] font-medium leading-none cursor-pointer hover:bg-accent-hover disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                  onClick={onContinue}
                  disabled={continuing || disabled || optimizing || !connected}
                  title={continueLabel}
                  data-testid="composer-continue"
                  {...offlineProps(connected, 'continue', continueLabel)}
                >
                  {continuing ? <Loader2 size={14} className="animate-spin" /> : <RotateCw size={14} />}
                  {i18nT('components.chatInput.resume')}
                </button>
              ) : (
              <button
                className="primary w-8 h-8 rounded-full bg-accent text-accent-fg border-none flex items-center justify-center cursor-pointer hover:bg-accent-hover disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                onClick={fireComposer}
                disabled={(!value.trim() && !pendingFiles.length) || disabled || optimizing || !connected}
                aria-label={i18nT('components.chatInput.send')}
                {...offlineProps(connected, 'send', 'Send')}
              >
                <ArrowUp size={18} />
              </button>
              )}
            </>)}
          </div>
        </div>

        {/* Mobile bottom sheet */}

      </div></motion.div>)}
      </AnimatePresence>

      {/* Context shelf — plain full-width row below input */}
      {!showGhost && (onProjectClick || (onModelClick && modelName)) && (
        <div ref={shelfRef} className="pt-1 flex items-center gap-2 min-w-0">
          <div className="flex items-center gap-2 min-w-0 flex-1">
          {onAgentClick && agentName && (
            /* Chrome type: an agent name is a label, not code. `font-mono` would
               pin `var(--mono)`, which Settings → Display → Font Family never
               writes, so it would make the shelf ignore the user's typeface. */
            <button
              className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent ${agentSource === 'package' ? 'text-[var(--aim)] hover:text-[var(--aim)]' : 'text-muted hover:text-text disabled:hover:text-muted'}`}
              onClick={e => onAgentClick(e.currentTarget.getBoundingClientRect())}
              disabled={isRunning}
              title={isRunning ? i18nT('components.chatInput.stop_the_current_response_to_switch_agents') : i18nT('components.chatInput.agent', { name: agentName })}
              aria-label={isRunning ? i18nT('components.chatInput.stop_the_current_response_to_switch_agents') : i18nT('components.chatInput.agent', { name: agentName })}
            >
              <Bot size={13} className="shrink-0 opacity-70" />
              {!shelfCompact && <span className="truncate max-w-[160px]">{agentName}</span>}
            </button>
          )}
          {onProjectClick && (
          /* Two sibling buttons inside one visual pill, NOT a nested button:
             the folder segment opens the project picker and the branch segment
             copies. A <button> inside a <button> is invalid HTML and browsers
             collapse it, so the pill is a plain container and each segment owns
             its own click target and hover state. */
          <div className="inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] text-muted">
          <button
            className="inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] text-muted hover:text-text px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-muted"
            onClick={e => onProjectClick(e.currentTarget.getBoundingClientRect())}
            disabled={isRunning}
            title={isRunning ? i18nT('components.chatInput.stop_the_current_response_to_switch_project') : projectChipTitle}
            aria-label={isRunning ? i18nT('components.chatInput.stop_the_current_response_to_switch_project') : projectChipTitle}
          >
            <FolderOpen size={13} className="shrink-0 opacity-70" />
            {/* Budget favours the branch: the folder name is also in the tooltip
                and the picker, whereas a clipped branch ("feat/pro…") is exactly
                the ambiguity this label exists to remove. The enclosing shelf
                group is flex-1/min-w-0, so both segments still shrink below
                these caps on a narrow window. */}
            {!shelfCompact && <span className="truncate max-w-[160px]">{project ? (project.split('/').filter(Boolean).pop() || project) : i18nT('components.chatInput.project')}</span>}
          </button>
          {!shelfCompact && !!projectBranch && (
            <>
              <span className="opacity-40 shrink-0" aria-hidden="true">·</span>
              {/* Copying stays enabled while a response is running — unlike
                  switching project, reading the branch name is harmless. A git
                  ref IS code, so it sets `font-mono` itself (the pill container
                  does not supply it). */}
              <CopyBranchButton
                branch={projectBranch}
                label={projectDetached ? 'commit' : 'branch name'}
                className="max-w-[220px] font-mono opacity-70 hover:opacity-100 hover:text-text"
              />
            </>
          )}
          </div>
          )}
          </div>
          <div className="flex items-center shrink-0">
          {contextPct != null && (
            <div ref={ctxWrapRef} className="relative flex items-center">
              <button
                className={`inline-flex items-center h-7 px-2.5 rounded-md transition-colors border-none cursor-pointer ${ctxPopoverOpen ? 'bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]' : 'bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]'}`}
                onClick={() => setCtxPopoverOpen(o => !o)}
                title={contextTip(contextPct)}
                aria-label={i18nT('components.chatInput.context_usage')}
              >
                <ContextBar pct={contextPct} width={40} height={3} />
                {showContextPct && <span className="text-[11px] ml-1.5 tabular-nums" style={{ color: contextColor(contextPct) }}>{contextPctClamped(contextPct)}%</span>}
              </button>
              {ctxPopoverOpen && (
                <div className="absolute bottom-full right-0 mb-1 z-[60] w-52 rounded-xl border border-border bg-bg-elevated shadow-xl p-3 animate-slide-up">
                    {(() => {
                      const pct = Math.round(contextPct)
                      const win = contextWindowTokens || 0
                      const used = contextUsedTokens != null ? contextUsedTokens : (win ? Math.round((pct / 100) * win) : 0)
                      const remaining = win ? Math.max(win - used, 0) : 0
                      const approx = contextUsedTokens == null
                      const k = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : `${n}`
                      const pctColor = pct >= 90 ? 'var(--danger)' : pct >= 75 ? 'var(--warn)' : 'var(--accent)'
                      return (
                        <>
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-[11px] font-semibold text-text">{i18nT('components.chatInput.context_window')}</span>
                            <span className="text-[12px] font-mono font-bold" style={{ color: pctColor }}>{pct}%</span>
                          </div>
                          <div className="flex flex-col gap-1 text-[11px] font-mono">
                            <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.used')}</span><span className="text-text">{approx ? '~' : ''}{k(used)}</span></div>
                            <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.remaining')}</span><span className="text-text">{approx ? '~' : ''}{k(remaining)}</span></div>
                            <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.total')}</span><span className="text-text">{k(win)}</span></div>
                          </div>
                          {modelName && (
                            <div className="mt-2 pt-2 border-t border-border flex justify-between text-[11px] font-mono">
                              <span className="text-muted">{i18nT('components.chatInput.model')}</span><span className="text-text truncate max-w-[120px]" title={modelName}>{modelName}</span>
                            </div>
                          )}
                        </>
                      )
                    })()}
                  </div>
              )}
            </div>
          )}
          {onModelClick && modelName && (
            <button
              className="inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] text-muted hover:text-text px-2 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-muted"
              onClick={e => onModelClick(e.currentTarget.getBoundingClientRect())}
              disabled={isRunning}
              title={isRunning ? i18nT('components.chatInput.stop_the_current_response_to_switch_model') : i18nT('components.chatInput.model_2', { name: modelName })}
            >
              <span className="truncate max-w-[180px]">{modelName}</span>
              {onReasoningEffortClick && !shelfCompact && (
                <>
                  <span className="opacity-30 select-none shrink-0" aria-hidden="true">·</span>
                  <span className="opacity-60 shrink-0">{effortLabel(reasoningEffort || '')}</span>
                </>
              )}
            </button>
          )}
          </div>
        </div>
      )}
    </div>
  )
}

export default memo(ChatInput)
