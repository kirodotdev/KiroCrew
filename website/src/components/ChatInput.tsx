import { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo, useId, memo } from 'react'
import { markComposerResize } from '../utils/composerResize'
import { ArrowUpFromLine, ArrowUp, Loader2, RotateCw, Plus, Crop, Bot, Mic, Keyboard, Square, BookOpen, X, ClipboardList, CheckCircle, Ban, Sparkles, Target, Lock, Folder, FolderOpen, FileText, FileDiff, PenLine } from 'lucide-react'
import SketchDialog from './SketchDialog'
import CopyBranchButton from './CopyBranchButton'
import RejectDropdown from './RejectDropdown'
import { usePointerDrag } from '../hooks/usePointerDrag'
import { useScrollEdges } from '../hooks/useScrollEdges'
import VoiceStatusBar from './VoiceStatusBar'
import VoiceDictationPanel, { useDictationPanelUsable } from './VoiceDictationPanel'
import type { AudioSample } from '../hooks/mic'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useBranding } from '../hooks/useBranding'
import { useAppSelector, useAppDispatch } from '../store'
import { resolveByApprovalId, openActivityToTool, openActivityToTab, selectSlotPendingApproval, selectSlotPendingSpawnApprovals, markSubagentApproving, sseSubagentDone, setAgentSwitchNotice } from '../store/chatSlice'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import { useSlotId } from '../providers/SlotContext'
import { useToolPillVisible } from '../store/toolPillRegistry'
import { ToolDetails } from '../pages/chat/ToolDetails'
import { api, ApiError } from '../api/client'
import { safeSetItem, safeGetItem } from '../utils/safeStorage'
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
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'
import { Btn, Slider } from './ui'
import { useTouchPushToTalk } from '../hooks/useTouchPushToTalk'
import { consumeComposerRelease } from '../pages/chat/composerFocus'
import BusySendButton, { useBusySendMode } from './BusySendButton'
import { isScreenSnipSupported } from '../hooks/useScreenSnip'
import { useImeGuard } from '../hooks/useImeGuard'
import ContextBar, { contextTip, contextColor, composeContextReadout, contextPctClamped, fmtTokens } from './ContextBar'
import PasteHighlightLayer, { INPUT_TYPO } from './PasteHighlightLayer'
import PasteHoverLayer, { type PasteHoverHandle } from './PasteHoverLayer'
import FollowUpBar from './FollowUpBar'
import { dispatchLightbox } from './MarkdownRenderer'
import { IMG_EXT, buildFileLabels } from '../utils/fileTokens'
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
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

// Upload picker accept hints. Client-side ONLY (UX) — the server validates type
// (magic bytes), size, and runs malware scanning per input-validation guidance.
const IMAGE_ACCEPT = 'image/png,image/jpeg,image/gif,image/webp,image/bmp,image/svg+xml'
// Video containers the server accepts (see `_ALLOWED_VIDEO_EXT`). MIME form, not
// extensions, because this string is also what the MOBILE photo picker filters
// the library by: iOS shows videos only when a video/* type is listed, so an
// extension-only hint is what made a phone able to attach photos and nothing else.
// One MIME per accepted extension — `video/x-m4v` is NOT covered by `video/mp4`
// in a picker's filter, so omitting it hides a file the server would accept.
// test_accept_list_covers_every_accepted_extension pins this set against the
// server's, from the Python side, since a vitest cannot read the Python constant.
const VIDEO_ACCEPT = 'video/mp4,video/x-m4v,video/quicktime,video/webm'
const FILE_ACCEPT = IMAGE_ACCEPT + ',' + VIDEO_ACCEPT + ',.txt,.md,.json,.excalidraw,.har,.yaml,.yml,.xml,.csv,.log,.py,.js,.ts,.tsx,.jsx,.html,.css,.sh,.bash,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf,.zip,.tar,.gz'

// Extension per image MIME type, mirroring IMAGE_ACCEPT. Used to synthesize a
// filename for clipboard-pasted images (see nameClipboardImage).
const IMAGE_MIME_EXT: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/bmp': 'bmp',
  'image/svg+xml': 'svg',
}

/** Give a clipboard-pasted image a distinguishable filename.
 *
 *  Clipboard image blobs arrive unnamed or with the browser's fixed
 *  placeholder (Chrome/Firefox hand every pasted screenshot to us as
 *  "image.png"): an unnamed file has no extension so the server's extension
 *  allowlist rejects it outright, and repeated pastes in one message all
 *  render identical attachment-chip labels. Synthesize
 *  `pasted-image-<timestamp>[-<n>].<ext>` for those; a file that carries a
 *  real name (e.g. a file copied from the OS file manager) keeps it, so
 *  pasted and picked files stay indistinguishable downstream.
 *
 *  `batchIndex` disambiguates multiple images arriving in a SINGLE paste
 *  (same-millisecond timestamp). The timestamp is a technical identifier
 *  embedded in a filename, not display text, so it is deliberately not
 *  locale-formatted. */
function nameClipboardImage(f: File, batchIndex: number): File {
  const ext = IMAGE_MIME_EXT[f.type]
  if (!ext) return f // not an image type: never rename (spec: images only)
  const generic = !f.name || f.name === `image.${ext}` || f.name === 'image.png'
  if (!generic) return f
  const d = new Date()
  const pad = (n: number, w = 2) => String(n).padStart(w, '0')
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}${pad(d.getMilliseconds(), 3)}`
  const suffix = batchIndex > 0 ? `-${batchIndex + 1}` : ''
  return new File([f], `pasted-image-${stamp}${suffix}.${ext}`, { type: f.type, lastModified: f.lastModified })
}

import ApprovalModePicker, { APPROVAL_MODE_ADJUSTED_LS_KEY } from './ApprovalModePicker'
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
import type { FileKind } from './FilePickerMenu'
import SkillPickerMenu from './SkillPickerMenu'
import { skillsCacheStaleTime } from '../lib/skillsCache'
import ProjectSkillsTrustDialog from './ProjectSkillsTrustDialog'
import { matchFileToken, matchSkillToken, replaceTokenAtCaret } from './composerTokens'
import { useStopEscapeHatch } from '../hooks/useStopEscapeHatch'
import { useMeasuredHeight } from '../hooks/useMeasuredHeight'

import { i18nT } from '../i18n/t'
import { fmtDateFields, fmtPercent } from '../i18n/format'
import SessionRefStrip from './SessionRefStrip'
import type { SessionRef } from '../utils/sessionRefs'
const INPUT_MIN_H = 44
const INPUT_DEFAULT_MAX_H = 140
const INPUT_PREFILL_MAX_H = 320
const INPUT_DRAG_MIN_H = 93
const INPUT_DRAG_MAX_RATIO = 0.5
const INPUT_HEIGHT_LS_KEY = 'mc-input-height'
/**
 * Whether the composer is in hold-to-talk mode (`'1'`) — the WeChat-style swap
 * where the textarea is replaced by a hold target. Persisted because using voice
 * is a habit rather than a per-message choice: someone who dictates does it all
 * day, and resetting to the keyboard on every mount taxes exactly them.
 */
const VOICE_MODE_LS_KEY = 'mc-voice-mode'

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

// Decisions mapped here resolve via the ONE-SHOT `api.resolveApproval`
// endpoint, which has no trust verb: POST /api/approvals/{id}/{action} honors
// exactly `approve`, `reject` and `reject_once` (dashboard/handlers/sessions.py),
// and the next identical call prompts again. Any UI feeding this path must offer
// only those decisions — mapping a trust verb to `approve` here runs the tool
// once while the composer reports a standing grant the backend never recorded
// (#5400 on the spawn-approval card, #5434 on the collapsed tool row, #5486
// here). The Trust affordances are withheld from this path at their render
// sites (`approvalTrustGrantable`); this arm stays fail-closed so a trust verb
// that reaches it anyway is rejected rather than silently upgraded — the same
// rule ChatPage's `toApiDecision` carries verbatim.
function toApiDecision(d: string): 'approve' | 'reject' | 'reject_once' {
  if (d === 'approved') return 'approve'
  if (d === 'rejected_once') return 'reject_once'
  return 'reject'
}

/** Approval sources that run unattended, with no human bound to the chat the
 *  card renders in. Session-scoped Trust is meaningless for these (see
 *  `approvalIsUnattended`), so the Trust controls are withheld and only
 *  Allow once / Reject are offered. Kept in sync with the backend's
 *  `_BACKGROUND_APPROVAL_SOURCES` minus `autonudge`, which does run in-session. */
export const UNATTENDED_APPROVAL_SOURCES = new Set(['cron', 'heartbeat', 'taskrunner'])

/** B2 nudge: after this many manual one-shot approvals in one slot while the
 *  mode is still `normal`, offer the approval-mode picker once. Three is the
 *  point where repeated prompting reads as friction rather than safety. */
const APPROVAL_NUDGE_THRESHOLD = 3

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

/** True when the text on the caret's line, before the caret, is ONLY markdown
 *  blockquote markers — `>`, `> > `, optionally indented. A collapsed-paste
 *  chip then flows on that line (`> [ Paste #1 · N lines ]`) instead of being
 *  forced onto its own line, which strands the `>` above the chip and makes
 *  the user delete the injected newline to quote a paste. Whitespace alone
 *  (no `>`) is NOT a quote prefix — the own-line shape stays for those.
 *  Linear scan, no regex. */
function isBlockquotePrefix(linePrefix: string): boolean {
  let sawMarker = false
  for (let i = 0; i < linePrefix.length; i++) {
    const c = linePrefix.charCodeAt(i)
    if (c === 62 /* > */) { sawMarker = true; continue }
    if (c === 32 /* space */ || c === 9 /* \t */) continue
    return false
  }
  return sawMarker
}

/** Off-screen twin used to measure the composer's content height.
 *
 *  Measuring must NOT touch the live textarea's box. The live element is a flex
 *  item, so setting its height (even for one synchronous read) changes what the
 *  transcript scroller above it is allotted — the scroller reclaims the height
 *  one-for-one, measured on the real dashboard: composer 44 -> 140px moved the
 *  scroller's clientHeight 561 -> 465px. A momentarily TALLER scroller has a
 *  smaller maximum scrollTop, so the engine clamps any reader parked closer to
 *  the bottom than the textarea is tall, and the reader lands at the end with no
 *  application write anywhere. `overflow:hidden` does not prevent this: overflow
 *  governs scrollbars, not a flex item's contribution to its parent.
 *
 *  Engine asymmetry is why this reads as an iOS-only defect: Blink defers scroll
 *  offset clamping to the rendering lifecycle, so a transient that is undone
 *  inside the same task never clamps, while WebKit clamps during layout. A
 *  Chromium reproduction of the keystroke case therefore shows nothing at all. */
/** Far enough off-screen that no scrollable ancestor can reach the twin. */
const TWIN_OFFSCREEN_PX = '-99999px'

let measureTwin: HTMLTextAreaElement | null = null

/** Content height of `el`'s value, measured without mutating `el`. */
function measuredContentHeight(el: HTMLTextAreaElement): number {
  if (typeof document === 'undefined') return INPUT_MIN_H
  if (!measureTwin) {
    measureTwin = document.createElement('textarea')
    measureTwin.setAttribute('aria-hidden', 'true')
    measureTwin.tabIndex = -1
    measureTwin.readOnly = true
    document.body.appendChild(measureTwin)
  }
  const twin = measureTwin
  const cs = window.getComputedStyle(el)
  // `position:fixed` keeps the twin out of every flow, so no ancestor of the live
  // composer — and therefore not the transcript scroller — can see it at all. It
  // also escapes a transformed ancestor, which a `position:absolute` twin would not.
  // Set per property rather than through one `cssText` declaration string: that
  // form reads as user-facing copy to the i18n gate, and this one matches the
  // property-by-property copying below.
  twin.style.position = 'fixed'
  twin.style.top = TWIN_OFFSCREEN_PX
  twin.style.left = TWIN_OFFSCREEN_PX
  twin.style.visibility = 'hidden'
  twin.style.pointerEvents = 'none'
  twin.style.height = '0'
  twin.style.overflow = 'hidden'
  twin.style.resize = 'none'
  twin.style.border = '0'
  // Everything that can move where the text wraps or how tall a line is. Width and
  // the horizontal box must match or the twin wraps at a different column and
  // reports a height the live element would never have. The live textarea is
  // `border-none`, which is why clearing the border above is safe: under
  // `box-sizing:border-box` a themed border would otherwise give the twin a WIDER
  // content box than the element it stands in for.
  const COPIED = [
    'width', 'boxSizing',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'font', 'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'fontStretch',
    'fontFeatureSettings', 'fontVariationSettings', 'fontKerning',
    'lineHeight', 'letterSpacing', 'wordSpacing', 'textIndent', 'textTransform',
    'whiteSpace', 'wordBreak', 'overflowWrap', 'hyphens', 'tabSize',
    'direction', 'writingMode', 'unicodeBidi',
  ] as const
  const style = twin.style as unknown as Record<string, string>
  const computed = cs as unknown as Record<string, string>
  for (const prop of COPIED) {
    const v = computed[prop]
    // Firefox returns '' for the `font` shorthand; the longhands below it cover the
    // same ground, so skip rather than clobber a good value with an empty one.
    if (v) style[prop] = v
  }
  // An empty composer still renders its PLACEHOLDER in the content box, and that
  // counts toward scrollHeight — several of these placeholders are long translated
  // strings that wrap to two lines at phone width, so measuring the empty value
  // alone would clip the box to one line. The text is measured as the twin's VALUE
  // rather than as its `placeholder` attribute: the two lay out through the same
  // path at the same width, and an off-screen node carrying a real placeholder
  // attribute would answer accessibility and test queries meant for the live one.
  twin.value = el.value || el.placeholder || ''
  return twin.scrollHeight
}

/** Auto-size textarea to fit content (only when not manually sized).
 *
 *  The measurement happens on an off-screen twin (see `measuredContentHeight`),
 *  so this function's only write to the live element is its FINAL height.
 *
 *  `parked` is a hard precondition, not an optimisation. Voice hold mode and the
 *  dictation panel both keep the textarea mounted inside an `sr-only` box (value,
 *  caret and IME state have to survive the swap), and `sr-only` is a 1px clip — a
 *  textarea one pixel wide reports a `scrollHeight` of the better part of a
 *  viewport, which this function would then clamp to `cap` and WRITE BACK as an
 *  inline height. That height outlives the parking (nothing re-measures until
 *  `value` changes again), so a single voice round-trip left the composer stuck
 *  at the 140px ceiling with an empty box, on a surface whose only way to shrink
 *  it — the drag handle's double-click — does not exist under a finger. */
function applyHeight(
  el: HTMLTextAreaElement,
  manualHeight: number | null,
  prefillHint?: boolean,
  parked?: boolean,
) {
  if (parked) return // clipped out of layout — there is nothing valid to measure
  if (manualHeight !== null) return // manual height — wrapper controls size
  const cap = prefillHint ? INPUT_PREFILL_MAX_H : INPUT_DEFAULT_MAX_H
  const prev = el.style.height
  const next = Math.max(INPUT_MIN_H, Math.min(measuredContentHeight(el), cap)) + 'px'
  if (next !== prev) {
    el.style.height = next
    // Attribute the transcript's resulting viewport change to the composer, so the
    // transcript can hold still instead of chasing it (see composerResize.ts).
    markComposerResize()
  }
  // When typing at the end of overflowing content, snap to the bottom so the caret
  // stays visible.
  const caretAtEnd = el.selectionStart === el.value.length && el.selectionEnd === el.value.length
  if (document.activeElement === el && el.scrollHeight > el.clientHeight && caretAtEnd) {
    el.scrollTop = el.scrollHeight
  }
}

/** Stable empty result for suppressed spawn-approval reads — a fresh [] per render would churn every dependent memo. */
const EMPTY_SPAWN_APPROVALS: ReturnType<typeof selectSlotPendingSpawnApprovals> = []

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
  /** When true (composer is busy — a running turn, or background sub-agents
   * still running for the slot), show the split Steer/Queue send button.
   * Steer's meaning follows the state: mid-turn it injects into the live turn;
   * with only sub-agents running it starts a turn now instead of parking the
   * message behind them. If the slot's backend is not steer-capable (e.g.
   * claude), the POST safely falls through to the queue server-side.
   * Plumbing a per-slot capability flag is a follow-up. */
  canSteer?: boolean
  /** Act on the composer NOW rather than queueing: a mid-turn steer into the
   * running turn, or a fresh turn when only sub-agents are running. Reads the
   * composer text and pending files itself (ChatPage) and clears them
   * atomically — ChatInput must NOT clear the value around this call. */
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
  /** Pending folder references for the preview strip: RELATIVE paths with trailing slash, derived from `@rel/` composer tokens (a path reference handed to the agent, not an upload) */
  pendingDirs?: string[]
  /** Resize details keyed by pending-file path; renders a badge on the chip */
  resizedInfo?: Record<string, ResizeInfo>
  /** Remove a pending file by path */
  onRemoveFile?: (path: string) => void
  /** Remove a pending folder reference by its relative path (strips its composer token) */
  onRemoveDir?: (path: string) => void
  /** Session references staged by dragging a session onto the chat pane.
   *  Rendered as chips above the textarea, the same treatment as attachments.
   *  Serialized as links (never transcripts) when the message is sent. */
  pendingSessions?: SessionRef[]
  /** Unstage a session reference by its session key */
  onRemoveSessionRef?: (key: string) => void
  /** Show macOS-only buttons (screenshot) */
  isMac?: boolean
  /** Drag-and-drop handler for the entire input bar */
  onDrop?: (e: React.DragEvent) => void
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
  /**
   * "Is a transcription in flight ANYWHERE" — ungated by session ownership, the
   * same distinction `voiceCaptureActive` draws for capture.
   *
   * `voiceTranscribing` is gated (`owned && transcribing`), but the refusal it
   * has to predict is global: `startVoice` returns early on `voice.transcribing`
   * outright, because the mic is one shared device. Gating it meant that while
   * another session's transcript was still landing, THIS composer's voice
   * controls looked live, invited a press, and captured nothing.
   *
   * Only the voice affordances read this. The composer's own text behaviour
   * (focus, Enter-to-send) stays on the gated flag — another slot transcribing
   * is no reason to stop this one from typing and sending.
   */
  voiceTranscribeActive?: boolean
  onVoiceToggle?: () => void
  /** Cancel (discard) an in-progress dictation without transcribing — Esc. */
  onVoiceCancel?: () => void
  /** Pre-warm the mic on pointer-down so recording starts instantly on click. */
  onVoicePrewarm?: () => void
  /** Begin capture. Distinct from `onVoiceToggle` because the hold-to-talk
   *  gesture must open and close a session on separate edges of one press —
   *  a toggle cannot express "the finger went down" on its own. */
  onVoiceStart?: () => Promise<void> | void
  /** End capture AND transcribe — the commit half of the hold gesture. */
  onVoiceStop?: () => void
  /**
   * Is capture in flight AT ALL — ungated by session ownership, unlike
   * `voiceRecording`.
   *
   * The two are not interchangeable and the difference loses speech. Streaming
   * STT flips its own `recording` true the moment the worklet is wired and PCM is
   * buffering, but `useVoiceInput` assigns `sessionOwner` only AFTER the server
   * handshake resolves — so for the length of that handshake real audio exists
   * while `voiceRecording` (which is `owned && recording`) still reads false. The
   * gesture's commit veto asks "did capture begin?", and answering it with the
   * ownership-gated flag made a release inside that window take the discard
   * branch and drop what the user had just said.
   *
   * Use this ONLY for that question. Anything presentational keeps
   * `voiceRecording`, so one slot never renders another slot's capture.
   */
  voiceCaptureActive?: boolean
  /** Mic error (null = none), live input level [0,1], active device label, and error-dismiss. */
  voiceError?: string | null
  voiceLevel?: number
  voiceDeviceLabel?: string
  /** deviceId of the track actually capturing (data-driven picker checkmark). */
  voiceDeviceId?: string
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
  /** Byte progress of the one-time speech-model download the live session waits
   *  on, or null. Both recording surfaces render it: a multi-hundred-megabyte
   *  transfer with nothing on screen is indistinguishable from a hung mic. */
  voiceDownload?: { done: number; total: number } | null
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
  /** Show used/window token counts in the inline context readout. */
  showContextTokens?: boolean
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
  /** Invoked when an @-mention picks a file or directory. `kind` defaults to
   *  'file'. `token` is the exact composer text the pick inserted (e.g.
   *  "@src/pages/"), computed against the picker's search root — the staging
   *  side records it so a later chip-remove can strip precisely this token. */
  onFileSelect?: (path: string, kind?: FileKind, token?: string) => void
  onFileOpen?: (path: string) => void
  project?: string
  /** Checked-out branch of the active project (or short SHA when detached). */
  projectBranch?: string
  /** True when the project's HEAD is detached, so the label is a commit. */
  projectDetached?: boolean
  /** Uncommitted file count in the project's working tree (0 = clean). */
  projectGitDirty?: number
  /** Commits ahead of / behind the branch's upstream, when it tracks one. */
  projectGitAhead?: number
  projectGitBehind?: number
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
  /** Select a follow-up option — handler toggles text in input (see ChatPage wiring).
   *  Third arg is `followUpSourceKey` as it was when the chip was CLICKED (the
   *  chip debounces, and the row can advance inside that window); `undefined`
   *  when no `followUpSourceKey` is supplied. */
  onFollowUpSelect?: (option: string, event: React.MouseEvent, sourceKeyAtClick?: string | null) => void
  /** Immediate send (double-click / Send-now). Second arg is the click-time
   *  row identity FollowUpBar already snapshots for onSelect. */
  onFollowUpSend?: (text?: string, sourceKeyAtClick?: string | null) => void
  /** Quick Send enabled — clicking sends immediately */
  quickSend?: boolean
  /** Layout mode for the follow-up bar: 'multiline' (default) or 'scroll' (original single-line). */
  followUpLayout?: 'multiline' | 'scroll'
  /** Identity of the transcript row the follow-up options were derived from.
   *  Forwarded to FollowUpBar so a chip click carries the row it acted on. */
  followUpSourceKey?: string | null
  /** The option text carrying a `(recommended)` marker — a host's `followUpRecommended`. */
  followUpRecommended?: string | null
  /** Collapsed paste blocks backing `⌜🗒 Pasted …⌟` tokens in `value`. */
  pasteBlocks?: PasteBlock[]
  /** Replace the current list of paste blocks (add/remove). */
  onPasteBlocksChange?: (next: PasteBlock[]) => void
  /** Optional knowledge chip rendered above the input */
  knowledgeChip?: React.ReactNode
  /** When this key changes, focus the textarea (e.g. on chat session switch). */
  /** Focus-on-switch key. Any new consumer of this prop must honor the
   *  composerFocus one-shot (consumeComposerRelease) or macOS keyboard
   *  switches will autofocus through the release — see composerFocus.ts. */
  autoFocusKey?: string | null
  /**
   * Accessible name for the textarea. Defaults to the main chat's "Message
   * input". A host mounting a SECOND composer on the same screen (the side
   * panel) must pass a distinct name, or a screen-reader user tabbing between
   * the two hears the same announcement for both.
   */
  inputAriaLabel?: string
  /**
   * The typed '/' command and '$' skill triggers, their pickers, and their
   * rows in the plus menu. Defaults on. A host whose sends bypass command
   * handling (the side panel's isolated Q&A turns treat text literally)
   * turns this off so the menus cannot offer commands that would be sent as
   * plain text.
   */
  typedCommandMenus?: boolean
  /**
   * The slot's approval chrome (tool-approval bar, spawn-approval banner).
   * Defaults on. These are store-driven for the composer's slot, so a second
   * composer on the SAME slot (the side panel) must opt out or the main
   * turn's approvals render twice on one screen.
   */
  slotApprovalChrome?: boolean
  /**
   * The prompt-optimizer button. Defaults on. Its slot-mismatch completion
   * path routes a late result through `onOptimizeResult`; a host whose
   * displayed slot can change mid-optimize and that supplies no such route
   * (the side panel) must opt out, or an optimize finished after a session
   * switch silently discards the draft it produced.
   */
  promptOptimizer?: boolean
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

/** Accent pill under a downscaled attachment chip. Hover (or focus) shows a
 *  styled tooltip with the resize details, portal-rendered above the chip so
 *  the strip's overflow-x-auto can't clip it. */
function ResizeBadge({ resize }: { resize: ResizeInfo }) {
  const [tip, setTip] = useState<{ top: number; left: number } | null>(null)
  const ref = useRef<HTMLButtonElement>(null)
  const show = () => {
    const r = ref.current?.getBoundingClientRect()
    if (r) setTip({ top: r.top - 8, left: r.left })
  }
  const hide = () => setTip(null)
  return (
    <>
      {/* In flow under the thumbnail, not overlaid on it. The tile is a fixed
          64px square, while the widest catalog values need 105px (bn) and
          104px (de). Overlaid, that ends as one of two defects — an unbreakable
          Latin word spilling sideways onto the neighbouring chip, or a
          per-character-breaking script stacking down and covering the
          thumbnail. In flow, the chip is simply as wide as the wider of tile
          and pill, so each locale pays only its own width and the thumbnail is
          never covered in any of them. `whitespace-nowrap` is what makes the
          chip grow instead of the pill wrapping. */}
      <button
        type="button"
        ref={ref}
        aria-label={i18nT('components.chatInput.resized_to_fit_model_limits_2', { fromW: resize.fromW, fromH: resize.fromH, toW: resize.toW, toH: resize.toH })}
        className="px-1.5 py-[1px] rounded-full border-0 text-[10px] font-bold bg-accent text-accent-fg shadow-sm cursor-default whitespace-nowrap"
        onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}
      >{i18nT('components.chatInput.resized')}</button>
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

/** Stable default so an omitted `dirs` prop does not re-run the remeasure
 *  effect on every render (a fresh [] literal changes deps each time). */
const NO_DIRS: string[] = []

function FilePreviewStrip({ files, dirs = NO_DIRS, resizedInfo, onRemove, onRemoveDir, rootRef }: { files: string[]; dirs?: string[]; resizedInfo?: Record<string, ResizeInfo>; onRemove?: (path: string) => void; onRemoveDir?: (path: string) => void; rootRef?: (node: HTMLDivElement | null) => void }) {
  const [attachScroller, edges, remeasure] = useScrollEdges<HTMLDivElement>()
  // Chips are added and removed while the strip stays mounted (a paste, a
  // remove), and the scroller keeps its own box through those changes, so the
  // ResizeObserver never fires and no scroll event lands. Without this the cue
  // goes stale: dark over a row that now fits, or absent over one that clips.
  useEffect(() => { remeasure() }, [files, dirs, remeasure])
  const imgs = files.filter(p => IMG_EXT.test(p))
  const nonImgs = files.filter(p => !IMG_EXT.test(p))
  if (!imgs.length && !nonImgs.length && !dirs.length) return null
  return (
    // The wrapper exists for the edge cues: absolutely-positioned children of
    // the scroller itself would travel with the scrolled content, so the fades
    // anchor to a non-scrolling parent, same shape as the sibling strips.
    <div className="relative" ref={rootRef}>
      {/* items-start, not items-end: a chip carrying a resize pill is taller than a
          plain one, and bottom-alignment would spend that difference staggering the
          THUMBNAILS (the thing being compared) instead of letting the pills hang. */}
      <div ref={attachScroller} data-testid="preview-strip" className="flex gap-2 px-4 py-2 border-t border-border bg-chrome/50 overflow-x-auto items-start" data-image-scope="">
      {imgs.map((path, i) => {
        const src = `/api/file-raw?path=${encodeURIComponent(path)}`
        const resize = resizedInfo?.[path]
        return (
          <div key={path} className="group/preview shrink-0 flex flex-col items-start gap-0.5" title={path}>
            {/* The corner controls anchor to the IMAGE, not to the chip: the chip
                is as wide as the wider of tile and resize pill, so a locale
                whose pill is wider than the 64px tile (de: 104px pill) would
                otherwise strand the remove button 40px out in the empty space
                beside the thumbnail it removes. */}
            <div className="relative">
            <span className="absolute -top-1.5 -left-1.5 w-5 h-5 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center z-10">{i + 1}</span>
            <button
              type="button"
              aria-label={i18nT('components.chatInput.open_preview_of', { name: path.split('/').pop() })}
              className="block cursor-pointer"
              onClick={(e) => { const img = e.currentTarget.querySelector('img'); if (img) dispatchLightbox(img) }}
            >
              {/* Fixed 64×64 square tile: every image chip is the same size, so
                  a phone screenshot (31px at intrinsic ratio) is as recognisable
                  as a landscape shot, and the strip's row stays uniform.
                  object-cover center-crops instead of letterboxing — the full
                  image is one click away in the lightbox, so the tile only has
                  to be identifiable, not complete. bg-bg-hover backs
                  transparent PNGs so the border reads as a tile rather than a
                  see-through frame. */}
              {/* The listener refreshes the scroll cue; the image is inside the
                  actual preview button and is not itself interactive. */}
              {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
              <img src={src} alt={path} className="w-16 h-16 rounded border border-border object-cover bg-bg-hover hover:opacity-80 transition-opacity"
                data-lightbox-image=""
                // The tile's box is fixed, but chips mount before their bytes
                // arrive and remove/add churns the strip's scrollWidth without
                // resizing the scroller's own box — no ResizeObserver fires and
                // no scroll lands, so this load signal still refreshes the cue.
                onLoad={remeasure} />
            </button>
            {onRemove && (
              <button
                aria-label={i18nT('components.chatInput.remove')}
                className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-danger text-white text-[12px] flex items-center justify-center opacity-0 group-hover/preview:opacity-100 transition-opacity cursor-pointer"
                onClick={() => onRemove(path)} title={i18nT('components.chatInput.remove')}
              ><X className="lucide-inline" /></button>
            )}
            </div>
            {resize && <ResizeBadge resize={resize} />}
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
      {/* Folder references: a path handed to the agent, not an upload. No
          /api/file-raw thumbnail is fetched — there is no content to preview.
          Labels are basename-first and widen by parent segments on collision
          (shared buildFileLabels rule), so two staged `pages/` folders from
          different parents stay tellable apart. */}
      {(() => {
        // buildFileLabels splits on `/` only, so normalize Windows separators
        // for label computation; keys and tooltips keep the original rel.
        const normDir = (d: string) => d.replace(/\\/g, '/').replace(/\/+$/, '')
        const dirLabels = buildFileLabels(dirs.map(normDir))
        return dirs.map(path => (
        <div
          key={path}
          data-dir-chip=""
          title={path}
          className="relative group/preview shrink-0 flex items-center gap-1.5 px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text"
        >
          <Folder size={12} aria-label={i18nT('components.filePickerMenu.folder')} className="shrink-0 lucide-inline" />
          <span>{(dirLabels.get(normDir(path)) || path) + '/'}</span>
          {onRemoveDir && (
            <button aria-label={i18nT('components.filePickerMenu.remove_folder')} className="text-muted hover:text-danger cursor-pointer bg-transparent border-none p-0" onClick={() => onRemoveDir(path)} title={i18nT('components.filePickerMenu.remove_folder')}><X size={12} /></button>
          )}
        </div>
        ))
      })()}
      </div>
      {/* Edge cues, same treatment as the sibling strips (SidePanelLayout's
          tab strip, FollowUpBar's scroll row): a gradient says content
          continues past the clipped edge, because the overlay scrollbar on
          macOS/iOS leaves no visible sign while idle. from-bg-elevated matches
          the composer surface the strip sits on. z-10 keeps the fade above the
          chips' own z-10 badges; pointer-events-none keeps those interactive. */}
      {edges.left && (
        <div aria-hidden="true" data-testid="preview-strip-cue-left" className="pointer-events-none absolute left-0 top-px bottom-0 w-6 z-10 bg-gradient-to-r from-bg-elevated to-transparent" />
      )}
      {edges.right && (
        <div aria-hidden="true" data-testid="preview-strip-cue-right" className="pointer-events-none absolute right-0 top-px bottom-0 w-6 z-10 bg-gradient-to-l from-bg-elevated to-transparent" />
      )}
    </div>
  )
}


/** Stable no-op so an unwired embedder does not remount the picker each render. */
const noopSelectDevice = () => {}
/** Zero-arg stand-in for an absent voice control. Separate from
 *  `noopSelectDevice`, whose one parameter makes it unassignable to `() => void`. */
const noopVoiceControl = () => {}

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
  pendingDirs = [],
  resizedInfo,
  onRemoveFile,
  onRemoveDir,
  pendingSessions = [],
  onRemoveSessionRef,
  isMac = false,
  onDrop,
  onDragOver,
  onDragLeave,
  voiceRecording = false,
  onSelectVoiceDevice,
  voiceDeviceSwitchIsLive = false,
  voiceTranscribing = false,
  voiceTranscribeActive,
  onVoiceToggle,
  onVoiceCancel,
  onVoicePrewarm,
  onVoiceStart,
  onVoiceStop,
  voiceCaptureActive,
  voiceError = null,
  voiceLevel = 0,
  voiceDeviceLabel = '',
  voiceDeviceId = '',
  voiceDictationPanel = false,
  voiceStreaming = false,
  voiceSampleRef,
  voicePartial = '',
  voiceDownload = null,
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
  showContextTokens,
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
  projectGitDirty,
  projectGitAhead,
  projectGitBehind,
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
  followUpSourceKey,
  followUpRecommended,
  pasteBlocks = [],
  onPasteBlocksChange,
  knowledgeChip,
  autoFocusKey,
  inputAriaLabel,
  typedCommandMenus = true,
  slotApprovalChrome = true,
  promptOptimizer = true,
  connected = true,
  onOptimizeResult,
}: ChatInputProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const disabled = disabledProp
  const dispatch = useAppDispatch()
  const slotId = useSlotId()
  const pendingApprovalRaw = useAppSelector(s => selectSlotPendingApproval(s, slotId), shallowEqual)
  // Suppressed at the READ so every consumer (bar, ghost, pill, rounded-corner
  // class) follows one judgment instead of each render site re-deciding.
  const pendingApproval = slotApprovalChrome ? pendingApprovalRaw : null
  const hasApproval = !!pendingApproval
  const [approvalSubmitting, setApprovalSubmitting] = useState(false)
  // A2: bumping this opens the footer ApprovalModePicker with a spotlight
  // ring, so the approval bar's hint lands the user on the real control.
  const [approvalPickerSignal, setApprovalPickerSignal] = useState(0)
  // A1: the hint retires once the user has ever adjusted the mode themselves.
  // Read per approval arrival (cheap), not once per mount, so adjusting the
  // mode hides the hint on the very next approval without a reload.
  const approvalModeAdjusted = !!pendingApproval && !!safeGetItem(APPROVAL_MODE_ADJUSTED_LS_KEY)
  // B2: per-slot manual one-shot approval tally for this dashboard session.
  // In-memory by design — "3 approvals in one sitting" is the annoyance
  // signal; persisting it would fire the nudge on stale history.
  const approvalCountsRef = useRef<Record<string, number>>({})
  const [approvalNudgeSlot, setApprovalNudgeSlot] = useState<string | null>(null)
  const approvalNudgeActive = !!approvalNudgeSlot && approvalNudgeSlot === slotId
  // Permanent dismissal (buttons / menu open): the callout has delivered its
  // lesson, so the A1 hint retires with it — otherwise a "Got it" user keeps
  // seeing "Tired of confirming every step?" on every later approval.
  const dismissApprovalNudge = useCallback(() => {
    setApprovalNudgeSlot(null)
    // One flag carries both retirements: the adjusted/discovery flag already
    // suppresses the hint AND gates the nudge, so a separate dismissed flag
    // would only ever be written alongside it — dead state.
    safeSetItem(APPROVAL_MODE_ADJUSTED_LS_KEY, '1')
  }, [])
  // Session-scoped hide (Escape): a reflexive Escape aimed at the composer
  // must not spend the one-time callout unseen; it may re-fire on a later
  // approval in this sitting.
  const hideApprovalNudge = useCallback(() => setApprovalNudgeSlot(null), [])
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
  const approvalIsShell = approvalMeta?.is_shell === '1'
  // Command-scoped trust is offered only when the gateway proved a canonical,
  // unredacted scope.  The title/input preview are presentation data and must
  // never be promoted into grant authority by a frontend fallback.
  const approvalTrustCommandGrantable = approvalMeta?.trust_command_grantable === '1'
  const approvalTrustBaseGrantable = approvalMeta?.trust_base_grantable === '1'
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
  /** True when a standing Trust grant can actually be RECORDED for this card.
   *  FAIL-CLOSED: the Trust affordances are withheld unless this holds, because
   *  the only other resolve path is the one-shot `api.resolveApproval`, which
   *  has no trust verb — offering Trust there claims a standing grant the
   *  backend never records (#5400, #5434, #5486).
   *  - `activeSlot`: `api.approveChatSlot` is slot-scoped, so with no slot the
   *    grant has nowhere to land and `handleApprovalAction` falls through to the
   *    one-shot endpoint.
   *  - `!approvalIsUnattended`: session trust is incoherent for a job that is
   *    not this session (see `approvalSource` above). */
  const approvalTrustGrantable = !!activeSlot && !approvalIsUnattended
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
      // B2: tally manual one-shot approvals per slot. Only 'approved' counts —
      // a trust grant already reduces future prompts, and a rejection is not
      // approval fatigue. Fires once per dashboard install (localStorage
      // guard) and only while the slot still asks about everything (normal).
      if (decision === 'approved' && activeSlot && !approvalIsUnattended) {
        const n = (approvalCountsRef.current[activeSlot] || 0) + 1
        approvalCountsRef.current[activeSlot] = n
        if (
          n >= APPROVAL_NUDGE_THRESHOLD &&
          approvalMode === 'normal' &&
          !safeGetItem(APPROVAL_MODE_ADJUSTED_LS_KEY)
        ) {
          setApprovalNudgeSlot(activeSlot)
        }
      }
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
  }, [approvalId, activeSlot, approvalIsUnattended, approvalSource, approvalMode, dispatch])

  // Pending sub-agent SPAWN approvals for this slot (blocked on user approval).
  // Surfaced as a top-level banner with inline Approve/Reject so the user can
  // resolve pending spawns without leaving the composer. A single pending spawn
  // gets a compact one-line row; with several, the header carries Approve all /
  // Reject all and each sub-agent gets its own row with per-agent Approve/Reject
  // (so one can be run and another rejected). "Review in panel" opens the
  // Subagents tab for the fuller per-agent view (task + streaming output).
  // Resolution goes through the same api.resolveApproval + markSubagentApproving
  // path the panel uses, so the two surfaces stay consistent for a given id.
  const pendingSpawnApprovalsRaw = useAppSelector(s => selectSlotPendingSpawnApprovals(s, slotId), shallowEqual)
  const pendingSpawnApprovals = slotApprovalChrome ? pendingSpawnApprovalsRaw : EMPTY_SPAWN_APPROVALS
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
  // Hover detection layer that shows paste previews on mouseover; scroll-synced
  // identically to the backdrop mirror.
  const hoverRef = useRef<PasteHoverHandle>(null)
  // Id of the open paste-preview tooltip (or null). Wired to the textarea's
  // aria-describedby so keyboard/screen-reader users get the preview announced
  // when the caret enters a token — the AT half of the paste-preview a11y fix.
  const [pastePreviewPanelId, setPastePreviewPanelId] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const fileInputId = useId()
  // "+" drop-up menu (upload file / image + browse toggle).
  const [plusOpen, setPlusOpen] = useState(false)
  const [sketchOpen, setSketchOpen] = useState(false)
  const [ctxPopoverOpen, setCtxPopoverOpen] = useState(false)
  // Per-session auto-compact threshold (slider in the context popover). The
  // debounce timer collapses a slider drag into one POST; the fetch itself is
  // the React Query below (declared after the shared queryClient), so the
  // value lives in the standard cache rather than hand-rolled state.
  const autoCompactTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const autoCompactPending = useRef<{ slot: string; pct: number | null } | null>(null)
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
  // Tooltip for the working-tree badge. Reuses the Git panel's catalog entry
  // so the badge adds no i18n keys; the arrow segments are glyph+number only
  // (script-neutral, plain concatenation — a template literal here reads as an
  // untranslated string to the i18n gate). Empty when the tree is clean and in
  // sync, which is also what hides the badge.
  const gitBadgeTitle = useMemo(() => {
    const parts: string[] = []
    if (projectGitDirty) parts.push(i18nT('components.gitPanel.uncommitted', { count: projectGitDirty }))
    if (projectGitAhead) parts.push('\u2191' + String(projectGitAhead))
    if (projectGitBehind) parts.push('\u2193' + String(projectGitBehind))
    return parts.join(' \u00b7 ')
  }, [projectGitDirty, projectGitAhead, projectGitBehind])
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
  // Split send button while the composer is BUSY: 'steer' (default) vs 'queue'.
  // The mode is a persisted PER-SLOT preference — see BusySendButton.
  const [busySendMode, setBusySendMode] = useBusySendMode(slotId)
  // Steer is the active Enter/send action only while the composer is busy and
  // not stopping, on a steer-capable slot, and the user hasn't switched the
  // split button to Queue. Everywhere else the composer falls back to onSend
  // (normal send, or server-side queue while busy).
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
  const sendFollowUp = useCallback((text?: string, sourceKeyAtClick?: string | null) => {
    if (!disabled) onFollowUpSend?.(text, sourceKeyAtClick)
  }, [disabled, onFollowUpSend])
  const { botName } = useBranding()
  const isMobile = useIsMobile()
  const directFilePicker = isMobile || isTouchDevice()
  const [attachControlRow, controlRowEdges, remeasureControlRow] = useScrollEdges<HTMLDivElement>()
  // The control row's chips are prop-driven (the auto-nudge loop chip, the
  // approval-mode picker) and appear or change label while the row keeps its
  // own box, so neither the ResizeObserver nor a scroll event reports the new
  // content width — only this remeasure can refresh the cue. Boolean presence,
  // not the callback itself: the handler's identity may change every render
  // and would re-run the effect for nothing.
  const hasAutoNudge = !!onAutoNudgeClick
  useEffect(() => { remeasureControlRow() }, [hasAutoNudge, autoNudgeLoop, approvalMode, isMobile, remeasureControlRow])
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
  // The one expression both surfaces key off: the composer offers Resume
  // exactly when the loop chip must stop pulsing. Hoisted so the two cannot
  // drift — recomputing it at each site is how the chip silently regresses to
  // claiming active work over a dead session.
  const resumeOffered = !!(continuable && onContinue && continueIsRecovery)
  const continuePlaceholder = resumeOffered
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
  // Project skill awaiting consent, together with the exact chat/project/request
  // that initiated it. A grant can outlive this dialog, so completion must not
  // write into a different draft or supersede a newer consent request.
  const nextTrustRequestIdRef = useRef(0)
  const activeTrustRequestIdRef = useRef<number | null>(null)
  const [trustPrompt, setTrustPrompt] = useState<{
    requestId: number
    leaf: string
    slotKey?: string
    project?: string
  } | null>(null)
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
  // Warm the per-slot-and-project skills cache when the input gains focus so the first
  // `$` trigger renders the picker instantly (the fetch is the only latency).
  // prefetchQuery is a no-op if the cache is already fresh (staleTime), so it's
  // cheap to call on every focus. The key and the session key must match
  // SkillPickerMenu's exactly — including the trailing agent segment — or the
  // prefetch warms a different entry and the menu still pays the fetch on open.
  // The deadline binds HERE too, not only in the menu: react-query dedupes on that
  // shared key, so the menu opening onto this fetch never runs its own queryFn.
  const queryClient = useQueryClient()
  // Per-session auto-compact threshold: fetched lazily on popover open (the
  // slots frame stays untouched), cached under the standard query layer. The
  // slider writes optimistically into the cache per step and the debounced
  // mutation collapses a drag into one POST; the response re-syncs the cache.
  const autoCompactQuery = useQuery({
    queryKey: ['slot-autocompact', activeSlot ?? null],
    queryFn: () => api.chatSlotAutocompact(activeSlot as string),
    enabled: ctxPopoverOpen && !!activeSlot,
    staleTime: 30_000,
  })
  const autoCompact = autoCompactQuery.data ?? null
  // INVARIANT: every threshold POST is chained onto the previous
  // write for that slot, so writes commit in issue order — a delayed earlier
  // POST can never land after (and overwrite) a newer value on the server.
  // ALL dispatch sites (the debounced mutation, the cross-slot flush, the
  // unmount flush) MUST go through enqueueAutoCompactWrite; never call
  // api.setChatSlotAutocompact directly from this component.
  const autoCompactChain = useRef<Map<string, Promise<unknown>>>(new Map())
  const enqueueAutoCompactWrite = useCallback((slot: string, pct: number | null) => {
    const prev = autoCompactChain.current.get(slot) ?? Promise.resolve()
    // Chain through settle (not just success): a failed write must not block
    // — or reorder — the writes queued behind it.
    const next = prev.then(
      () => api.setChatSlotAutocompact(slot, pct),
      () => api.setChatSlotAutocompact(slot, pct),
    )
    autoCompactChain.current.set(slot, next.then(() => undefined, () => undefined))
    return next
  }, [])
  const autoCompactMutation = useMutation({
    mutationFn: ({ slot, pct }: { slot: string; pct: number | null }) => enqueueAutoCompactWrite(slot, pct),
    onSuccess: (r, vars) => {
      queryClient.setQueryData(
        ['slot-autocompact', vars.slot],
        (prev: { pct: number | null; global_pct: number; min: number; max: number } | undefined) =>
          prev ? { ...prev, pct: r.pct, global_pct: r.global_pct } : prev,
      )
    },
    onError: (err, vars) => {
      // A rejected write must not leave the optimistic value cached: refetch
      // the server truth so the slider snaps back to the applied threshold.
      queryClient.invalidateQueries({ queryKey: ['slot-autocompact', vars.slot] })
      // Surface the failure the way the sibling per-slot settings do (model,
      // reasoning effort): a silent snap-back leaves the user's compaction
      // intent unapplied with no explanation — and with the popover closed,
      // no visible change at all.
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(err)))
    },
  })
  const pushAutoCompact = useCallback((pct: number | null) => {
    if (!activeSlot) return
    const slot = activeSlot
    queryClient.setQueryData(
      ['slot-autocompact', slot],
      (prev: { pct: number | null; global_pct: number; min: number; max: number } | undefined) =>
        prev ? { ...prev, pct } : prev,
    )
    if (autoCompactTimer.current) clearTimeout(autoCompactTimer.current)
    // Debouncing only ever supersedes a write for the SAME slot. A pending
    // write for another slot (drag on A, switch, drag on B within the window)
    // is a different session's change: flush it now instead of discarding it,
    // or A would silently keep its old threshold on the server.
    const pending = autoCompactPending.current
    if (pending && pending.slot !== slot) {
      autoCompactPending.current = null
      autoCompactMutation.mutate({ slot: pending.slot, pct: pending.pct })
    }
    autoCompactPending.current = { slot, pct }
    autoCompactTimer.current = setTimeout(() => {
      autoCompactPending.current = null
      autoCompactMutation.mutate({ slot, pct })
    }, 400)
  }, [activeSlot, queryClient, autoCompactMutation])
  // Flush (not discard) a pending debounced write on unmount: cancelling the
  // sole POST would leave the server on the old threshold while the user saw
  // their change accepted. Fire the API call directly -- the component is
  // gone, so the mutation's cache re-sync has nothing left to update.
  useEffect(() => () => {
    if (autoCompactTimer.current) clearTimeout(autoCompactTimer.current)
    const pending = autoCompactPending.current
    if (pending) {
      autoCompactPending.current = null
      // On rejection, drop the optimistic value from the cache so a return to
      // this slot refetches server truth instead of showing a threshold the
      // session never applied (mirrors the mutation's onError). Routed through
      // the per-slot chain so the flush cannot overtake an in-flight write.
      void enqueueAutoCompactWrite(pending.slot, pending.pct).catch((err) => {
        queryClient.invalidateQueries({ queryKey: ['slot-autocompact', pending.slot] })
        // The component is gone but the store is not: surface the failure
        // like the sibling settings do, or the user's last change before
        // navigating away silently never applies.
        dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(err)))
      })
    }
  }, [queryClient, enqueueAutoCompactWrite, dispatch])
  const skillSlotKey = slotId ? `dashboard:${slotId}` : undefined
  const skillSlotKeyRef = useRef(skillSlotKey)
  skillSlotKeyRef.current = skillSlotKey
  const skillProjectRef = useRef(project)
  skillProjectRef.current = project
  const prefetchSkills = useCallback(() => {
    queryClient.prefetchQuery({
      queryKey: ['skills', skillSlotKey ?? null, project ?? null, agentName ?? null],
      queryFn: ({ signal }) => api.skills(skillSlotKey, agentName, signal),
      staleTime: skillsCacheStaleTime(project),
    })
  }, [queryClient, skillSlotKey, project, agentName])
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
  /** The persisted drag-to-resize preference. Read `manualHeight` below instead —
   *  this is the raw stored value and is not what the composer renders at. */
  const [manualHeightPref, setManualHeight] = useState<number | null>(() => {
    const saved = localStorage.getItem(INPUT_HEIGHT_LS_KEY)
    const n = saved ? parseInt(saved, 10) : NaN
    return !isNaN(n) && n >= INPUT_MIN_H ? n : null
  })
  /**
   * Drag-to-resize is pointer-only, so on a touch device the composer always
   * auto-sizes and the persisted preference is ignored outright.
   *
   * Nobody drags a phone's message box, and the affordance is not merely unused
   * there — it is a trap. The handle is a 6px strip with `touch-action:none` and a
   * zero-px drag threshold sitting directly above the input, so a thumb that lands
   * short pins the height on the spot; and the only way back out is a
   * double-click, which no finger can produce. One stray tap and the box was that
   * size for good, across reloads.
   *
   * Derived rather than baked into the state's seed so a pointer-class change
   * mid-session (a tablet gaining a trackpad) is honoured in both directions:
   * the preference is never destroyed, only disregarded while there is no pointer
   * to have set it. Every consumer below — the wrapper's height, the textarea's
   * `flex-1`, the manual-resize floor, `applyHeight`'s bail — reads this and so
   * follows automatically.
   */
  const isTouch = useIsTouchDevice()
  const manualHeight = isTouch ? null : manualHeightPref

  // Drag-to-resize refs — resize wrapper div via direct DOM writes, commit on mouseup.
  // Resizing the wrapper (not the textarea) avoids layout thrashing: the textarea
  // fills the wrapper with height:100% so the browser only reflows the wrapper's
  // subtree, not the entire flex column + Virtuoso list above.
  const dragging = useRef(false)
  const dragStartY = useRef(0)
  const dragStartH = useRef(0)
  /** Mirrors `textareaParked` (defined with the voice-mode derivations, far below)
   *  for the handlers declared above it. Assigned during render, like the other
   *  prop/state mirrors in this file, so it is already current by the time any
   *  effect or event handler reads it. */
  const parkedRef = useRef(false)

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
    // A keyboard-driven switch released the composer (macOS chord chaining —
    // see releaseComposerForKeyboardSwitch): consume the one-shot and skip
    // this transition's autofocus entirely. The ref advances so the
    // disabled-retry path cannot resurrect the skipped focus later.
    if (consumeComposerRelease()) {
      prevAutoFocusKeyRef.current = autoFocusKey
      return
    }
    if (disabled || isMobile || isTouchDevice()) return
    prevAutoFocusKeyRef.current = autoFocusKey
    const ae = document.activeElement as HTMLElement | null
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)) return
    inputRef.current?.focus()
  }, [autoFocusKey, disabled, isMobile])

  // Global "/" shortcut to focus chat input (like GitHub, YouTube, Slack).
  // Only the primary command composer claims it: with a second instance
  // mounted (the side panel), two document-level listeners would contend and
  // the last-registered one would silently win the focus.
  useEffect(() => {
    if (!typedCommandMenus) return
    const onSlashFocus = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable) return
      e.preventDefault()
      inputRef.current?.focus()
    }
    document.addEventListener('keydown', onSlashFocus)
    return () => document.removeEventListener('keydown', onSlashFocus)
  }, [typedCommandMenus])

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
  }, [manualHeight, pendingFiles.length, pendingSessions.length])

  // The two effects that MEASURE the textarea (auto-size, and the paste-mirror
  // scroll sync that reads the scrollTop auto-size just wrote) are declared much
  // further down, immediately below `textareaParked` — they must not run while the
  // textarea is clipped out of layout, and a dep can only name a variable already
  // in scope. Do not move them back up here.

  // Reset manual height when input is cleared (new message sent)
  const prevValueRef = useRef(value)
  useEffect(() => {
    if (prevValueRef.current && !value) {
      resetHeight()
      // Picker open state is derived only in the textarea's own onChange, so the
      // parent-driven send-clear would otherwise leave a stale menu open.
      setSlashMenuOpen(false)
      setFilePickerOpen(false); setFileQuery('')
      setSkillPickerOpen(false); setSkillQuery('')
    }
    // Exit history mode when value diverges from the recalled message
    // (user edited it, or the send pipeline cleared it).
    if (historyIdxRef.current !== -1 && value !== sentMessages?.[historyIdxRef.current]) {
      historyIdxRef.current = -1
      draftRef.current = ''
    }
    prevValueRef.current = value
  }, [value, resetHeight, sentMessages])

  // ChatInput is one instance shared by every slot, so a switch would carry the
  // previous tab's menu over; an unsent draft never hits the clear above.
  useEffect(() => {
    setSlashMenuOpen(false)
    setFilePickerOpen(false); setFileQuery('')
    setSkillPickerOpen(false); setSkillQuery('')
  }, [slotId])

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
    if (!dragging.current) applyHeight(e.target as HTMLTextAreaElement, manualHeight, prefillHint, parkedRef.current)
  }, [manualHeight, prefillHint])

  const setTextUndoable = useCallback((text: string) => {
    const el = inputRef.current
    if (!el) { onChange(text); return }
    el.readOnly = false
    el.focus()
    el.select()
    // Same reconciliation handlePaste does, for the same reason: execCommand's
    // boolean is not evidence. It is absent entirely on some engines, and iOS
    // Safari reports success on a <textarea> while leaving the field untouched.
    // Here the whole field was just select()ed, so an unverified failure leaves
    // the ORIGINAL prompt on screen with the optimizer's result discarded and
    // no error — indistinguishable from "the optimizer changed nothing".
    let inserted = false
    try {
      inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, text)
    } catch { inserted = false }
    // Reconcile through the controlled value either way, exactly as handlePaste
    // does: after a real insert this is the same string the textarea's own
    // onChange already pushed up (React bails), while an insert React never saw
    // would be reverted to the stale `value` prop on the next render — the same
    // silent vanish by a different route. Marked user-driven so the undo
    // recorder treats it as an edit (a new boundary) rather than a
    // parent-driven draft restore, which is what keeps this "undoable".
    const nativeOk = inserted && el.value === text
    valueFromUserRef.current = true
    onChange(text)
    if (nativeOk) return // the native insert placed the caret itself
    requestAnimationFrame(() => {
      if (el && document.activeElement === el) el.setSelectionRange(text.length, text.length)
    })
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
    // Gated on `promptOptimizer` like the Optimize button and plus-menu row:
    // a host that opted out (e.g. the side panel) has no optimize affordance,
    // so the combo falls through to ordinary Enter/Shift+Enter handling there
    // instead of rewriting a draft the surface meant to treat literally.
    // preventDefault always fires when the combo is detected so the browser's
    // default Enter behavior (newline insert) doesn't leak through when the
    // gateway is offline. The action itself is gated on `connected` to match
    // the disabled-state on the Optimize button (line ~1734).
    if (promptOptimizer && e.key === 'Enter' && (e.metaKey || e.ctrlKey) && e.shiftKey) {
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
    if (sendKey && !e.defaultPrevented) {
      // The key is ours as soon as it matches the send binding, so claim it before
      // deciding what to do with it — `claimEnter` suppresses the default and returns
      // false for an Enter the IME is committing. Every early return below therefore
      // leaves the draft untouched instead of gaining a newline, which is what the
      // browser does with an Enter nobody consumed.
      // The send itself is gated on `connected` to match the Send button's disabled
      // state, and skipped while a prompt optimization owns the draft.
      // While the composer is busy, Enter follows the split-button mode:
      // steer (default) acts on the text now; queue defers it.
      if (!ime.claimEnter(e)) return
      if (optimizingRef.current) return
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
  }, [fireComposer, onChange, sentMessages, sendOnEnter, pasteBlocks, onPasteBlocksChange, connected, ime, optimizePrompt, promptOptimizer])

  /** Intercept clipboard paste — files go to upload path, big text gets collapsed into a token. */
  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    // Cmd/Ctrl+Shift+V bypass: consume the one-shot flag up front, before any
    // early return below, so it can never leak into a later paste (e.g. a
    // context-menu paste with no intervening keydown to clear it).
    const forceRaw = rawPasteRef.current
    rawPasteRef.current = false
    // File paste takes precedence — but not when text is also insertable. Only
    // text/plain defers: a <textarea> can only ever insert the text/plain
    // representation, so when the clipboard carries text/html WITHOUT
    // text/plain (a browser's "Copy Image", an Office chart copy) deferring
    // would make the whole paste a silent no-op — there is no text to insert.
    // macOS Office TEXT copies do include text/plain alongside their junk
    // image rendering of the selection, so real text pastes still win over
    // the image (see ChatInput.paste.test.tsx).
    const clipTypes = e.clipboardData.types || []
    const hasText = clipTypes.includes('text/plain')
    let renamedCount = 0
    const files = Array.from(e.clipboardData.items)
      .filter(i => i.kind === 'file')
      .map(i => i.getAsFile())
      .filter((f): f is File => f !== null)
      .map(f => {
        // Count only files actually renamed, so a paste of [real-name.png,
        // image.png] synthesizes an unsuffixed name (no orphan "-2").
        const named = nameClipboardImage(f, renamedCount)
        if (named !== f) renamedCount += 1
        return named
      })
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
      // and the trailing one when the caret is at the end of a line. Also
      // skip the leading one when everything before the caret on its line is
      // a bare blockquote prefix (`> `, `> > `, optionally indented): the
      // user is quoting the paste, and forcing the chip down a line strands
      // the `>` above it.
      const linePrefix = before.slice(before.lastIndexOf('\n') + 1)
      const leadingNewline = before && !before.endsWith('\n') && !isBlockquotePrefix(linePrefix) ? '\n' : ''
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
      const next = before + cleaned + after
      // Insert through the native input path so the textarea's own onChange runs:
      // that fires the /, @, $ picker detection, marks the edit user-driven, and
      // keeps native undo. Fall back to a controlled-value splice where
      // execCommand is unavailable (jsdom/tests) or reports failure.
      let inserted = false
      try {
        inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, cleaned)
      } catch { inserted = false }
      // That boolean is not evidence on its own. iOS Safari's native paste
      // callout reports success on a <textarea> and can leave the field
      // untouched, and this branch has ALREADY called preventDefault() — so
      // trusting the return value drops the paste with no visible trace at all.
      // Read the DOM back instead, and reconcile the controlled value either
      // way: after a real insert this is the same string the textarea's own
      // onChange already pushed up (React bails), while an insert React never
      // saw would otherwise be reverted to the stale `value` prop on the next
      // render — the same silent vanish by a different route.
      const nativeOk = inserted && ta.value === next
      valueFromUserRef.current = true
      onChange(next)
      if (nativeOk) return // the native insert placed the caret itself
      requestAnimationFrame(() => {
        if (ta && document.activeElement === ta) {
          const pos = before.length + cleaned.length
          ta.setSelectionRange(pos, pos)
        }
      })
    }
  }, [onUploadFiles, onPasteBlocksChange, pasteBlocks, value, onChange])

  /** Replace a collapsed-paste token with its full content in the textarea and
   *  drop the backing block. The caret lands just past the inserted content. */
  const expandTokenRange = useCallback((range: { start: number; end: number; block: PasteBlock }) => {
    const expanded = value.slice(0, range.start) + range.block.content + value.slice(range.end)
    onChange(expanded)
    onPasteBlocksChange?.(pasteBlocks.filter(b => b.id !== range.block.id))
    requestAnimationFrame(() => {
      const ta = inputRef.current
      if (ta) {
        const pos = range.start + range.block.content.length
        ta.setSelectionRange(pos, pos)
        ta.focus()
      }
    })
  }, [value, pasteBlocks, onPasteBlocksChange, onChange])

  /** Click/tap on a collapsed-paste token expands it to the original full
   *  content in the textarea.
   *
   *  Two gestures reach expansion, because a single gesture cannot serve both
   *  pointer classes:
   *   - Mouse: a two-step click — 1st click (detail=1) selects the token as a
   *     range (visual highlight), a quick 2nd click (detail>=2, the browser's
   *     own double-click) expands. `event.detail` is the click count the
   *     browser computes with its double-click timing, so no ref/selection
   *     tracking is needed and Chrome/Electron/Safari/Firefox all agree.
   *   - Touch: a single tap expands. Two discrete taps never coalesce into a
   *     `detail>=2` click the way mouse clicks do, so the double-click path is
   *     unreachable under a finger; gating expansion on it left the token only
   *     ever selectable on touch, never openable. A tap matches the sent-bubble
   *     PastedChip, which is a real <button> that toggles on one tap. */
  const handleTextareaClick = useCallback((e: React.MouseEvent<HTMLTextAreaElement>) => {
    if (!onPasteBlocksChange || !pasteBlocks.length) return
    const ta = e.currentTarget
    const caret = ta.selectionStart ?? 0
    const range = tokenRangeAt(value, pasteBlocks, caret)
    if (!range) return

    // Touch has no double-click to reach the expand branch below, so the first
    // tap inside a token expands directly — the select-first step is a
    // mouse-only refinement.
    if (isTouchDevice()) { expandTokenRange(range); return }

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
    expandTokenRange(range)
  }, [value, pasteBlocks, onPasteBlocksChange, expandTokenRange])

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
    // Keyboard/AT peek: a collapsed caret landing inside a token opens the
    // preview (the handle no-ops for a non-collapsed selection).
    hoverRef.current?.handleCaret(ss, se)
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

  const hasSessionRefs = pendingSessions.length > 0
  const [fileStripRef, fileStripH] = useMeasuredHeight<HTMLDivElement>()
  const [sessionStripRef, sessionStripH] = useMeasuredHeight<HTMLDivElement>()
  /** True when the composer holds something a send would carry.
   *
   *  Hoisted because the hold-to-talk gate has to agree with the send button, and
   *  an inline fifth copy is how that agreement rots. It replaces exactly ONE
   *  inline spelling (the mid-turn split-send branch); the resume and idle-send
   *  branches keep theirs, because they ask a DIFFERENT question — they include
   *  `hasSessionRefs` and this deliberately does not.
   *
   *  That exclusion is the point, not an oversight: a session ref is an
   *  attachment, not text to read back and edit, so dictating while one is
   *  pending is a normal thing to want and hold mode stays available for it. A
   *  refs-only composer therefore keeps the hold bar while the send button is
   *  live, which is correct for both. */
  const composerHasDraft = !!value.trim() || pendingFiles.length > 0
  /**
   * Hold-to-talk mode: the textarea is swapped for a press-and-hold target and
   * the mic button becomes the switch between the two.
   *
   * TOUCH ONLY, and that means the POINTER CLASS — not the width. Desktop already
   * has keyboard push-to-talk (`usePushToTalk`), so a pointer-hold mode there
   * would be a second way to do one thing, and this gesture exists precisely
   * because a thumb has no Esc key to discard with. A narrowed desktop window is
   * still a mouse: including `isMobile` here handed it the mode switch and took
   * away click-to-record, which is the opposite of an addition. `directFilePicker`
   * above pairs the two predicates because a native file dialog genuinely is a
   * width call; this one is not, so it gates on the pointer alone.
   *
   * Resolved inline rather than through a second coarse-pointer subscription: the
   * pointer class does not change under a mounted composer.
   */
  const voiceModeAvailable = !!onVoiceStart && !!onVoiceStop && !!onVoiceCancel && isTouchDevice()
  const [voiceModePref, setVoiceModePref] = useState(() => localStorage.getItem(VOICE_MODE_LS_KEY) === '1')
  /**
   * A draft SUSPENDS hold mode instead of exiting it, so the preference survives.
   *
   * This is the state every finished dictation lands in: the transcript arrives
   * in `value`, and reading, fixing and sending it are all things a hold target
   * cannot do. Suspending hands the textarea back for exactly as long as there is
   * something in it, then returns the hold bar without the user re-choosing it.
   *
   * When a capture the touch gesture OWNS is in flight, the draft check is
   * overridden — the mechanics and the reason live with `voiceHoldMode` below.
   */
  /** "Is capture in flight at all" — see the `voiceCaptureActive` prop doc. Falls
   *  back to the gated flag so the prop stays optional for other callers. */
  const captureInFlight = voiceCaptureActive ?? voiceRecording
  /** "Is a transcription in flight at all" — see the `voiceTranscribeActive` prop
   *  doc. Falls back to the gated flag so the prop stays optional. */
  const transcribeInFlight = voiceTranscribeActive ?? voiceTranscribing
  /** State, not a ref: the hold target mounts only once hold mode is on, and the
   *  gesture hook can only bind its listeners when that arrival is observable.
   *  Declared above `touchPtt` because the hook binds to it. */
  const [holdTarget, setHoldTarget] = useState<HTMLButtonElement | null>(null)
  const touchVoice = useMemo(
    () => ({
      recording: captureInFlight,
      start: onVoiceStart ?? noopVoiceControl,
      stop: onVoiceStop ?? noopVoiceControl,
      cancel: onVoiceCancel ?? noopVoiceControl,
    }),
    [captureInFlight, onVoiceStart, onVoiceStop, onVoiceCancel],
  )
  /*
   * `disabled` deliberately omits `!voiceHoldMode`, and that omission is what
   * lets `voiceHoldMode` read the hook's ownership below without a cycle. The
   * term is implied rather than lost: the hook binds only to `holdTarget`, the
   * only writer of `holdTarget` is the hold bar's ref, and the bar renders under
   * `voiceHoldMode &&` — so outside hold mode the hook has no element, no
   * listeners, and nothing left to disable. Leaving hold mode unmounts the bar,
   * which clears the target and runs the hook's own abandon path.
   */
  const touchPtt = useTouchPushToTalk(touchVoice, {
    target: holdTarget,
    disabled: disabled || transcribeInFlight || optimizing,
  })
  /*
   * A draft suspends hold mode, EXCEPT while the touch gesture's own capture is
   * still running — otherwise a transcript landing in the composer would unmount
   * the bar from under the finger that is still holding it.
   *
   * `touchPtt.owns` is what distinguishes the gesture's capture from any other,
   * and it has to be asked: `captureInFlight` alone also matches capture opened
   * elsewhere — the mic-as-record-button, or the keyboard push-to-talk binding
   * on a coarse-pointer device that also has a hardware keyboard. The previous
   * proxy, `holdTarget !== null`, could not tell those apart either: the bar is
   * mounted for EVERY capture that happens while hold mode is on, so a keyboard
   * dictation whose streaming partial landed in the composer kept hold mode
   * alive and rendered a disabled `settling` bar beside a disabled mode switch —
   * two dead touch controls describing a capture neither of them owned (#5753).
   * Ownership comes from the hook's own state machine instead, recorded at the
   * pointerdown that opens capture and relinquished when the gesture resolves.
   *
   * Relinquished AT THE RELEASE, deliberately: a draft the gesture itself
   * streamed in drops hold mode the moment the finger lifts, and the mic — a
   * record toggle again once hold mode drops — is the live stop control for
   * whatever drain remains. The old proxy instead held the surface as a
   * disabled `settling` bar until capture fully ended: a window where nothing
   * on screen was pressable. (What is VISIBLE through that drain depends on the
   * dictation panel: its own gate reads `voiceRecording`, so when enabled — the
   * default — it stays up and the textarea returns when capture ends; the
   * panel's `gestureDriven` carries the settling term for the same window, see
   * the render site.)
   */
  const voiceHoldMode = voiceModeAvailable && voiceModePref
    && (!composerHasDraft || (captureInFlight && touchPtt.owns))
  /**
   * True when the mic press changes MODE rather than starting a recording.
   *
   * ONE predicate for the label, the icon, the action and the disabled state. It
   * is written as a single value because deriving them separately is how a control
   * comes to say one thing and do another: with `!composerHasDraft` alone, a
   * streaming partial landing mid-capture made the label read "Switch to keyboard"
   * (which keys off `voiceHoldMode`, still true because capture overrides the
   * draft) while the click ran `onVoiceToggle` and stopped the recording.
   *
   * `voiceHoldMode ||` is the fix and it is not redundant: hold mode being ON is
   * itself proof there is a mode to switch out of, draft or no draft. The
   * `!composerHasDraft` half covers the other direction — an empty composer with
   * the preference off, where the switch is how voice gets turned on.
   */
  const micIsModeSwitch = voiceModeAvailable && (voiceHoldMode || !composerHasDraft)
  /**
   * Capture is winding DOWN: the gesture is over but the transport has not let go.
   *
   * Streaming `stop()` keeps `recording` true until its socket is cleaned up, and
   * `transcribeInFlight` is still false through that drain — so the bar fell back to
   * "Hold to talk" while enabled, and the next press hit the hook's
   * existing-recording branch and STOPPED the phantom session instead of opening a
   * new one. The user's next utterance was simply not captured.
   *
   * Derived from `touchPtt.bar` and used only for the label and the button's
   * `disabled` — deliberately NOT fed back into the hook's own `disabled`, which
   * would be circular. It does not need to be: a disabled <button> dispatches no
   * pointer events, so the gesture cannot start from a bar that is switched off.
   */
  const voiceSettling = voiceHoldMode && touchPtt.bar === 'settling'
  /**
   * The textarea is PARKED: still mounted, but clipped out of layout by the
   * `sr-only` box the hold bar and the dictation panel both put it in.
   *
   * Anything that measures the textarea has to ask this first — see `applyHeight`
   * for what a 1px-wide measurement did to the composer's height. It also has to
   * be a dep of those effects, so the height is recomputed on the way BACK: the
   * value that was streamed in while parked is exactly the value whose height was
   * never measurable.
   */
  const textareaParked = !!showDictation || voiceHoldMode
  parkedRef.current = textareaParked

  // Auto-resize textarea to fit content. Moved down here from the other composer
  // effects so it can name `textareaParked` — see the note at that site.
  useEffect(() => {
    if (inputRef.current && !dragging.current) applyHeight(inputRef.current, manualHeight, prefillHint, textareaParked)
  }, [value, prefillHint, manualHeight, textareaParked])

  // Keep the paste-highlight mirror's scroll aligned with the textarea after
  // value/height changes (applyHeight mutates scrollTop programmatically, which
  // doesn't fire the textarea's onScroll). rAF lets layout settle first.
  useEffect(() => {
    const id = requestAnimationFrame(() => {
      if (mirrorRef.current && inputRef.current) mirrorRef.current.scrollTop = inputRef.current.scrollTop
    })
    return () => cancelAnimationFrame(id)
  }, [value, prefillHint, manualHeight, textareaParked])
  const toggleVoiceMode = useCallback(() => {
    setVoiceModePref(prev => {
      const next = !prev
      safeSetItem(VOICE_MODE_LS_KEY, next ? '1' : '0')
      return next
    })
  }, [])
  /**
   * One label for the mic button, which is two different controls depending on
   * the device: a mode SWITCH on touch, the record toggle everywhere else.
   *
   * The draft case is spelled out rather than left as a bare greyed button —
   * "why can I not press this" is otherwise unanswerable, and the answer (there
   * is unsent text in the composer) is something the user can act on.
   */
  // Branches on `micIsModeSwitch` FIRST, so the label can only ever describe the
  // job the click actually performs. Reading `voiceHoldMode` first was what let the
  // two diverge — and a transcription elsewhere must not relabel a control whose
  // only job here is handing the keyboard back.
  const micLabel = micIsModeSwitch
    ? voiceHoldMode
      ? i18nT('components.chatInput.switch_to_keyboard')
      : i18nT('components.chatInput.switch_to_voice')
    : transcribeInFlight
      ? i18nT('components.chatInput.transcribing')
      // Not a switch: it records. Same two labels the desktop mic has always had.
      : voiceRecording
        ? i18nT('components.chatInput.stop_recording')
        : i18nT('components.chatInput.voice_input')
  /**
   * What the hold bar says, which must describe what the NEXT press or release
   * ACTUALLY does. Two of these were wrong for the same reason — the WeChat
   * gesture this copies sends on release, and the labels borrowed its promises
   * without borrowing its behaviour:
   *
   * - Releasing does NOT send. `stopVoice` sets `sttEndpointDisarmedRef` on
   *   purpose, so a manual stop cannot become an unrequested send; the transcript
   *   arrives as a composer draft. A user trusting "Release to send" would release,
   *   pocket the phone, and never notice the message was still sitting there — a
   *   silent failure on a chat surface's core action. It says `Release to
   *   transcribe`, which is what release does.
   * - While transcribing, the bar is disabled and used to still read "Hold to
   *   talk", so the dead control explained nothing.
   */
  const holdBarLabel = transcribeInFlight
    ? i18nT('components.chatInput.transcribing')
    : touchPtt.bar === 'settling'
      // NOT "Transcribing": the drain has not handed anything to the transcriber
      // yet. Saying so would claim work that has not started — the same overclaim
      // this bar has already been corrected for twice.
      ? i18nT('components.chatInput.finishing')
      : touchPtt.bar === 'armed-cancel'
        ? i18nT('components.chatInput.release_to_cancel')
        : touchPtt.bar === 'holding'
          ? i18nT('components.chatInput.release_to_transcribe')
          : touchPtt.bar === 'tap-too-short'
            ? i18nT('components.chatInput.keep_holding_to_record')
            : i18nT('components.chatInput.hold_to_talk')
  /**
   * Discovery hint for the mic switch, shown only where the switch exists and
   * only while it is reachable.
   *
   * Deliberately ranked BELOW `continuePlaceholder` and below a caller-supplied
   * `placeholder`: the resume hint is about a broken turn and outranks a feature
   * tour, and a caller that named its own placeholder means it.
   *
   * It names where the mic LEADS, not an action to perform. Two earlier wordings
   * were both wrong for the same reason — a two-step affordance does not fit in one
   * line, and compressing it produced a promise the tap does not keep:
   *
   * - "hold to talk" named a gesture with no target in keyboard mode (the textarea
   *   cannot be one, since a long press there opens the iOS selection loupe).
   * - "tap the mic to talk" was worse: the tap runs `toggleVoiceMode` and starts no
   *   capture, so anyone who tapped and spoke was not recorded at all.
   *
   * So it promises only what the tap delivers — voice becomes available — and the
   * hold bar that appears teaches the gesture where the gesture actually exists.
   */
  const voiceModePlaceholder = voiceModeAvailable && !voiceHoldMode && !composerHasDraft && !placeholder
    ? i18nT('components.chatInput.send_a_message_or_tap_the_mic_for_voice')
    : ''
  /** Combined height of every strip currently stacked above the textarea,
   *  MEASURED rather than predicted from the strips' Tailwind classes. The
   *  manual-resize floor and the transient height adjustment below both work off
   *  this total, so adding a strip can never leave one of them counting only
   *  attachments.
   *
   *  Each strip reports 0 while unmounted, so the sum needs no per-strip
   *  booleans: an absent strip reserves nothing by construction. That also
   *  retires the `hasResizedFile` special case — a chip carrying a resize pill
   *  is simply taller when measured, instead of needing a second predicted
   *  height, which is how the third constant came to exist in the first place.
   */
  const stripH = fileStripH + sessionStripH
  /** Whether `stripH` describes what is actually on screen right now.
   *
   *  A measured height arrives one commit AFTER the strip mounts: the ref
   *  callback cannot read a box that has not been laid out yet. Without this
   *  gate the settling 0 -> 81 reads as "a strip appeared" and the transient
   *  adjustment below inflates a persisted manual height by the strip's height
   *  on every mount that already had something staged. Waiting for a mounted
   *  strip to report a non-zero box makes the first value a BASELINE rather
   *  than a change. */
  const stripsMounted = pendingFiles.length > 0 || pendingDirs.length > 0 || hasSessionRefs
  const stripHSettled = stripsMounted ? stripH > 0 : stripH === 0
  const prevStripH = useRef<number | null>(null)
  const dragMinH = INPUT_DRAG_MIN_H + stripH
  const dragMinHRef = useRef(dragMinH)
  dragMinHRef.current = dragMinH
  // Adjust height transiently when a strip appears/disappears (not persisted —
  // staged files and session refs are both session-scoped). Diffing the TOTAL
  // rather than a per-strip boolean keeps the arithmetic correct when both
  // strips change in the same commit (e.g. send clears files and refs at once).
  useLayoutEffect(() => {
    if (!stripHSettled) return
    const prev = prevStripH.current
    prevStripH.current = stripH
    // `null` is the first settled reading: there is no previous state to have
    // moved from, so it establishes the baseline instead of adjusting.
    if (prev === null || prev === stripH) return
    setManualHeight(h => h !== null ? Math.max(INPUT_DRAG_MIN_H, h + (stripH - prev)) : h)
  }, [stripH, stripHSettled])

  return (
    // 'input-area' is a stable theming hook — see website/docs/theming-contract.md
    <div className={`input-area px-4 pb-1 ${hasApproval ? 'pt-0' : 'pt-1'} mx-auto w-full flex flex-col`}
      style={{ maxWidth: 'var(--mc-input-width, 900px)', ...(manualHeight !== null ? { minHeight: (INPUT_DRAG_MIN_H + stripH) + 'px' } : {}) }}>

      {/* Knowledge context chip */}
      {!showGhost && knowledgeChip}

      {/* Ghost follow-up bubbles floating above input */}
      {!showGhost && followUpOptions && followUpOptions.length > 0 && onFollowUpSelect && (
          <FollowUpBar options={followUpOptions} recommended={followUpRecommended} picked={followUpPicked ?? new Set()} onSelect={onFollowUpSelect} onSend={sendFollowUp} quickSend={quickSend} layout={followUpLayout} sourceKey={followUpSourceKey} />
      )}

      {/* Tip / folder-suggestion band — LAST above the composer so it always
          hugs the input box. Options (FollowUpBar) answer the assistant's
          question and belong with the transcript above; the tip is an ambient
          note attached to the composer, so a taller options row must never
          push it away from the box. */}
      {aboveComposer}

      {/* Drag handle — sits above approval bar or input, on pointer devices only */}
      {/* Pointer-drag resize handle for the message input (double-click resets).
          Resize is a pure visual enhancement — the textarea already auto-sizes to
          its content and there is no per-pixel keyboard resize gesture — so the
          handle is aria-hidden and carries no interactive semantics.

          Absent under a finger, and its absence is the feature: the reset is a
          double-click, so on touch the gesture could only ever pin the height, never
          undo it. See `manualHeight` for why the persisted value is disregarded
          there too.

          Its 6px box is ALSO the only thing separating the strip above (the
          options row, the tip band) from the composer box — so dropping the
          handle on touch dropped that separation with it, and the options row sat
          flush against the input. Touch therefore keeps the box and drops only
          the affordance, which puts the composer at the same offset under both
          pointer types instead of leaving the gap a side effect of a
          pointer-only control. */}
      {!showGhost && (isTouch
        ? <div aria-hidden="true" data-testid="composer-top-gap" className="h-[6px] shrink-0" />
        : <div
        aria-hidden="true"
        data-testid="composer-resize-handle"
        className="flex items-center justify-center h-[6px] cursor-row-resize group/drag"
        style={{ touchAction: 'none' }}
        {...inputResize}
        onDoubleClick={resetHeight}
        title={i18nT('components.chatInput.drag_to_resize_double_click_to_reset')}
      >
        <div className="w-12 h-[3px] rounded-full bg-border group-hover/drag:bg-accent group-active/drag:bg-accent-hover transition-all duration-200 opacity-0 group-hover/drag:opacity-100" />
      </div>)}

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
                      {approvalIsReadOnly && approvalTrustGrantable && <button disabled={approvalSubmitting} className={approvalBtnClass} onClick={() => handleApprovalAction('trust_reads')}><BookOpen size={12} className="shrink-0" />{i18nT('components.chatInput.trust_reads')}</button>}
                      {approvalTrustGrantable && approvalTrustCommandGrantable && (
                        <TrustDropdown
                            fullCommand={approvalFullCommand}
                            baseCommand={approvalBaseCommand}
                            isShell={approvalIsShell && approvalTrustBaseGrantable}
                            hasCommand={approvalTrustCommandGrantable}
                            disabled={approvalSubmitting}
                            className={approvalBtnClass}
                            onAction={(action, pattern) => { handleApprovalAction(action, pattern) }}
                        />
                      )}
                      <RejectDropdown
                          disabled={approvalSubmitting}
                          className={`${approvalBtnClass} hover:!text-danger hover:!bg-[color-mix(in_srgb,var(--danger)_10%,transparent)]`}
                          onAction={(action) => { handleApprovalAction(action) }}
                      />
                  </div>
              </div>
              {/* A1 discoverability hint: points at the footer mode picker so a
                  new user learns approval prompting is adjustable. Withheld for
                  unattended sources (the mode picker governs THIS slot, not the
                  job that raised the card), in the ghost state (the collapsed
                  composer unmounts the picker, so the link would have nothing
                  to open), while the B2 nudge is up (two pointers at one
                  control), and retired forever once the user has found the
                  picker — via this link or by adjusting the mode. */}
              {!showGhost && !approvalIsUnattended && !approvalModeAdjusted && !approvalNudgeActive && approvalMode && (
                <div className="flex items-center gap-1.5 flex-wrap px-3.5 pb-2 -mt-1 text-[12px] text-muted select-none">
                  <span>{i18nT('components.chatInput.approval_hint_question')}</span>
                  <button
                    type="button"
                    className="inline-flex items-center gap-0.5 p-0 bg-transparent border-none text-accent text-[12px] cursor-pointer hover:underline"
                    onClick={() => {
                      // Discovery achieved: the picker is about to open under a
                      // spotlight, so the hint has done its job for good.
                      safeSetItem(APPROVAL_MODE_ADJUSTED_LS_KEY, '1')
                      setApprovalPickerSignal(n => n + 1)
                    }}
                  >
                    {i18nT('components.chatInput.approval_hint_adjust')}
                  </button>
                </div>
              )}
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

      <input id={fileInputId} ref={fileInputRef} type="file" aria-label={i18nT('components.chatInput.attach_files')} multiple accept={FILE_ACCEPT} className="sr-only" onChange={handleFileInputChange} />
      {onUploadFiles && (
        <SketchDialog open={sketchOpen} onOpenChange={setSketchOpen} onInsert={onUploadFiles} returnFocusRef={inputRef} />
      )}

      {typedCommandMenus && <SlashCommandMenu input={value} anchorRef={inputRef as React.RefObject<HTMLElement>} open={slashMenuOpen} sendOnEnter={sendOnEnter} onSelect={cmd => { onChange(cmd); setSlashMenuOpen(false) }} onClose={() => setSlashMenuOpen(false)} />}

      {onFileSelect && (
        <FilePickerMenu
          query={fileQuery}
          anchorRef={inputRef as React.RefObject<HTMLElement>}
          open={filePickerOpen}
          project={project}
          sendOnEnter={sendOnEnter}
          onFileOpen={onFileOpen}
          onSelect={({ path, relativePath, kind }) => {
            // relativePath already carries a trailing slash for directories
            // (see selectionFor in FilePickerMenu), so the inserted token reads
            // as e.g. "@src/pages/ " and is unambiguously a folder.
            applyPickedToken(/(^|[\s])@\S*$/, `@${relativePath} `)
            setFilePickerOpen(false); setFileQuery('')
            onFileSelect(path, kind, `@${relativePath}`)
          }}
          onClose={() => { setFilePickerOpen(false); setFileQuery('') }}
        />
      )}

      {typedCommandMenus && <SkillPickerMenu
        query={skillQuery}
        anchorRef={inputRef as React.RefObject<HTMLElement>}
        open={skillPickerOpen}
        sendOnEnter={sendOnEnter}
        slotKey={skillSlotKey}
        project={project}
        agent={agentName}
        onSelect={({ leaf }) => {
          // Token left literal — backend appends the skill body; the user still
          // sees their $token marker. Caret-relative replace via shared helper.
          applyPickedToken(/(^|[\s])\$[a-z0-9/_-]*$/, `$${leaf} `)
          setSkillPickerOpen(false); setSkillQuery('')
        }}
        onTrustRequest={({ leaf }) => {
          // An unconsented project skill: close the menu and ask, rather than
          // inserting a token that would resolve to nothing.
          setSkillPickerOpen(false); setSkillQuery('')
          const requestId = nextTrustRequestIdRef.current + 1
          nextTrustRequestIdRef.current = requestId
          activeTrustRequestIdRef.current = requestId
          setTrustPrompt({ requestId, leaf, slotKey: skillSlotKey, project })
        }}
        onClose={() => { setSkillPickerOpen(false); setSkillQuery('') }}
      />}
      <ProjectSkillsTrustDialog
        key={trustPrompt?.requestId ?? 0}
        open={trustPrompt !== null}
        skillLeaf={trustPrompt?.leaf ?? ''}
        slotKey={trustPrompt?.slotKey}
        onClose={() => {
          activeTrustRequestIdRef.current = null
          setTrustPrompt(null)
        }}
        onTrusted={leaf => {
          const completedPrompt = trustPrompt
          if (
            !completedPrompt
            || completedPrompt.requestId !== activeTrustRequestIdRef.current
          ) return
          if (
            completedPrompt.slotKey !== skillSlotKeyRef.current
            || completedPrompt.project !== skillProjectRef.current
            || completedPrompt.leaf !== leaf
          ) {
            // Retire this prompt only if it is still current. A superseding
            // request has a different id and must remain open.
            activeTrustRequestIdRef.current = null
            setTrustPrompt(current =>
              current?.requestId === completedPrompt.requestId ? null : current)
            return
          }
          activeTrustRequestIdRef.current = null
          setTrustPrompt(null)
          // The grant makes the token resolvable, so insert it now — the user
          // asked for this skill and has just consented to its directory.
          applyPickedToken(/(^|[\s])\$[a-z0-9/_-]*$/, `$${completedPrompt.leaf} `)
        }}
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
        className={`${hasApproval ? 'rounded-b-2xl rounded-t-none' : 'rounded-2xl'} relative transition-colors overflow-hidden ${manualHeight !== null ? 'flex flex-col min-h-0' : ''} ${(cleanMode || memoryMode === 'incognito' || memoryMode === 'temporary') ? 'border-2' : 'border'} ${cleanMode ? 'border-accent bg-bg-elevated' : memoryMode === 'temporary' ? 'border-aim bg-bg-elevated' : memoryMode === 'incognito' ? 'border-warn bg-bg-elevated' : 'border-border bg-bg-elevated focus-within:border-accent/50'}`}

        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <SessionRefStrip refs={pendingSessions} onRemove={onRemoveSessionRef} rootRef={sessionStripRef} />
        <FilePreviewStrip files={pendingFiles} dirs={pendingDirs} resizedInfo={resizedInfo} onRemove={onRemoveFile} onRemoveDir={onRemoveDir} rootRef={fileStripRef} />

        {/* Cancel cue for the hold gesture. Rendered above the dictation panel so
            the drop zone is genuinely UP from the thumb, and only while a press is
            live — a permanent hint would be noise in a composer used mostly for
            typing. `aria-live` announces the arm/disarm flip, which is the only
            feedback a screen-reader user gets for a gesture with no focus change. */}
        {voiceHoldMode && touchPtt.phase !== 'idle' && (
          <div
            data-testid="hold-cancel-cue"
            aria-live="polite"
            className={`flex items-center justify-center gap-1.5 py-1.5 text-[11.5px] font-medium transition-colors ${
              touchPtt.armedCancel ? 'bg-danger text-danger-fg' : 'text-muted border-b border-dashed border-border-strong'
            }`}
          >
            {touchPtt.armedCancel ? (
              <><X size={12} className="shrink-0" />{i18nT('components.chatInput.release_to_cancel')}</>
            ) : (
              <><ArrowUp size={12} className="shrink-0" />{i18nT('components.chatInput.slide_up_to_cancel')}</>
            )}
          </div>
        )}


        {showDictation ? (
          /* `gestureDriven` carries the settling term because ownership ends at
             the release while this panel outlives it: `showDictation` is gated
             on `voiceRecording`, which stays true through the streaming drain.
             `bar === 'settling'` can only name the gesture's OWN drain (the
             hook records `draining` solely on its own commit path), so the
             keyboard hint stays suppressed for exactly the drain the finger
             just committed — and stays SHOWN for a keyboard-binding capture,
             where Esc/Enter genuinely work. */
          <VoiceDictationPanel sampleRef={showDictation} value={value} partial={voicePartial} deviceLabel={voiceDeviceLabel} deviceId={voiceDeviceId} onSelectDevice={onSelectVoiceDevice || noopSelectDevice} deviceSwitchIsLive={voiceDeviceSwitchIsLive} streaming={voiceStreaming} gestureDriven={voiceHoldMode || touchPtt.bar === 'settling'} download={voiceDownload} />
        ) : (
          <VoiceStatusBar recording={voiceRecording} level={voiceLevel} deviceLabel={voiceDeviceLabel} deviceId={voiceDeviceId} error={voiceError} onDismissError={onClearVoiceError} onSelectDevice={onSelectVoiceDevice || noopSelectDevice} deviceSwitchIsLive={voiceDeviceSwitchIsLive} download={voiceDownload} />
        )}

        {optimizing && <span className="absolute inset-0 flex items-start px-4 pt-3 text-sm text-white font-medium pointer-events-none z-10 bg-black/60 rounded-2xl"><Sparkles size={14} className="inline mr-1 text-yellow-400" /> {i18nT('components.chatInput.optimizing_prompt')}</span>}
        <div className={`relative ${showDictation || voiceHoldMode ? 'sr-only' : ''} ${manualHeight !== null ? 'flex-1 min-h-0 flex flex-col' : ''}`}>
        <PasteHighlightLayer ref={mirrorRef} value={value} blocks={pasteBlocks} />
        <textarea
          ref={inputRef}
          aria-label={inputAriaLabel ?? i18nT('components.chatInput.message_input')}
          data-composer-input=""
          aria-describedby={pastePreviewPanelId ?? undefined}
          data-composer-typo
          className={/* focus-cue-ok: the cue is the composer shell's focus-within border-accent brightening; a second ring on the textarea would double-paint one control. */ `relative w-full bg-transparent border-none ${INPUT_TYPO} text-text outline-none min-h-[44px] max-h-[50vh] placeholder:text-muted resize-none ${manualHeight !== null ? 'flex-1' : ''} ${disabled ? 'opacity-40 pointer-events-none' : ''} ${optimizing ? 'opacity-30' : ''}`}
          style={manualHeight !== null ? { height: '100%' } : undefined}
          placeholder={!connected ? i18nT('components.chatInput.gateway_offline_message_will_not_send') : disabledProp ? i18nT('components.chatInput.stopping') : voiceRecording ? i18nT('components.chatInput.recording_click_mic_to_stop') : voiceTranscribing ? i18nT('components.chatInput.transcribing_please_wait') : continuePlaceholder || voiceModePlaceholder || resolvedPlaceholder}
          readOnly={optimizing}
          rows={1}
          value={value}
          onDragOver={e => { e.preventDefault(); onDragOver?.(e); e.stopPropagation() }}
          onDragLeave={e => { onDragLeave?.(e); e.stopPropagation() }}
          onDrop={e => { e.preventDefault(); onDrop?.(e); e.stopPropagation() }}
          onChange={e => {
            valueFromUserRef.current = true // real DOM edit, not a parent-driven draft restore
            const val = e.target.value; onChange(val); setSlashMenuOpen(typedCommandMenus && val.startsWith('/'))
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
            if (typedCommandMenus && skillQ !== null) { setSkillPickerOpen(true); setSkillQuery(skillQ) }
            else { setSkillPickerOpen(false); setSkillQuery('') }
            recordCaret()
          }}
          onKeyDown={handleKeyDown}
          {...ime.bindComposition<HTMLTextAreaElement>({
            // The paste-hover preview dismisses on blur; the guard's latch reset rides
            // in the binding itself, so these handlers only carry what is local here.
            onFocus: prefetchSkills,
            onBlur: () => { if (hoverRef.current) hoverRef.current.handleMouseLeave() },
          })}
          onPaste={handlePaste}
          onCopy={handleCopy}
          onCut={handleCut}
          onClick={handleTextareaClick}
          onMouseUp={handleSelectSnap}
          onSelect={handleSelectSnap}
          onInput={handleInput}
          onScroll={e => { if (mirrorRef.current) mirrorRef.current.scrollTop = e.currentTarget.scrollTop }}
          onMouseMove={e => { if (pasteBlocks.length && hoverRef.current) hoverRef.current.handleMouseMove(e) }}
          onMouseLeave={() => { if (hoverRef.current) hoverRef.current.handleMouseLeave() }}
        />
        {pasteBlocks.length > 0 && <PasteHoverLayer ref={hoverRef} value={value} blocks={pasteBlocks} mirrorRef={mirrorRef} onActivePanelChange={setPastePreviewPanelId} />}
        </div>

        {/* The hold target. A real <button>, not the textarea: a long press on a
            text field opens iOS's selection loupe and swallows the pointermoves the
            cancel gesture is measured from, so "hold the input box" cannot be built
            on the input box. `touch-action:none` stops the page claiming the drag as
            a scroll, and the two -webkit rules stop the long-press callout.
            `flex-1` mirrors the textarea so a manually-resized composer does not
            fight the persisted height. */}
        {voiceHoldMode && (
          <div className={`flex px-2.5 pt-2 pb-0.5 ${manualHeight !== null ? 'flex-1 min-h-0' : ''}`}>
            <Btn
              type="button"
              ref={setHoldTarget}
              data-testid="hold-to-talk"
              style={{ touchAction: 'none', WebkitUserSelect: 'none', WebkitTouchCallout: 'none' }}
              // `flex-1` inside a flex row, NOT `flex` on its own: a <button> sizes
              // to fit-content even as a block-level flex container (UA form-control
              // sizing), so a bare display swap leaves a small pill where the whole
              // point is a target a thumb can hit without aiming.
              className={`flex-1 min-h-[44px] justify-center rounded-xl font-semibold select-none ${
                touchPtt.bar === 'armed-cancel'
                  ? 'border-dashed border-danger bg-danger-subtle text-danger'
                  : touchPtt.bar === 'holding'
                    ? 'border-accent bg-accent text-accent-fg'
                    : 'border-border-strong bg-card text-text-strong'
              }`}
              disabled={disabled || transcribeInFlight || optimizing || voiceSettling}
              aria-label={holdBarLabel}
            >
              <Mic size={15} className="shrink-0" />
              {holdBarLabel}
            </Btn>
          </div>
        )}

        {/* Bottom icon row */}
        <div className="flex items-center justify-between px-2.5 pb-2 pt-0.5">
          <div className="flex items-center gap-0.5 min-w-0">
            {onUploadFiles && (
              <div className="relative shrink-0" ref={plusWrapRef}>
                {directFilePicker ? (
                  /* Association is intentionally absent while uploads disable the control. */
                  <label
                    htmlFor={uploading ? undefined : fileInputId}
                    aria-disabled={uploading || undefined}
                    className={`w-8 h-8 rounded-lg flex items-center justify-center transition-all bg-transparent ${uploading ? 'opacity-30 cursor-default' : 'cursor-pointer text-muted hover:text-text hover:bg-bg-hover'}`}
                    aria-label={i18nT('components.chatInput.attach_files')}
                    title={i18nT('components.chatInput.attach_files')}
                  >
                    {uploading ? <Loader2 size={18} className="animate-spin" /> : <Plus size={18} />}
                  </label>
                ) : (
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
                )}
                {!directFilePicker && plusOpen && plusRect && createPortal(
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
                    {/* Sketch is a full-width menu ROW, not a third tile: the
                        tile group above is capped at two peer actions by the
                        max-two-buttons-per-row rule, and wrapping a third onto
                        a second grid line is the remedy that rule explicitly
                        rejects. A stacked row (the same shape as the trigger
                        shortcuts below) is its own row by construction. */}
                    <div className="mt-2 flex flex-col gap-0.5">
                      <button
                        type="button"
                        onClick={() => { setPlusOpen(false); setSketchOpen(true) }}
                        title={i18nT('components.chatInput.sketch')}
                        className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
                      >
                        <PenLine size={14} className="w-4 shrink-0 text-muted lucide-inline" />
                        <div className="min-w-0">
                          <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.sketch')}</div>
                          <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.sketch_desc')}</div>
                        </div>
                      </button>
                    </div>
                    {/* In-input trigger shortcuts: clicking inserts the sigil
                     *  and opens the matching picker (same as typing /, @, $). */}
                    <div className="mt-2 pt-2 border-t border-border flex flex-col gap-0.5">
                      {typedCommandMenus && <button
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
                      </button>}
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
                      {typedCommandMenus && <button
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
                      </button>}
                    </div>
                  </div>,
                  document.body
                )}
              </div>
            )}
            {/* Touch path: directFilePicker replaces the "+" drop-up with a
                bare file-input label, so the menu's Sketch row never mounts
                there. A pencil button restores the entry on exactly the
                devices where finger/stylus drawing works best. Two peer
                actions (label + pencil) — at the max-two-buttons-per-row cap,
                not over it; the non-touch branch keeps Sketch in the menu. */}
            {onUploadFiles && directFilePicker && (
              <button
                className="w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all disabled:opacity-30 bg-transparent border-none text-muted hover:text-text hover:bg-bg-hover shrink-0"
                onClick={() => setSketchOpen(true)}
                disabled={uploading}
                aria-haspopup="dialog"
                aria-label={i18nT('components.chatInput.sketch')}
                title={i18nT('components.chatInput.sketch')}
              >
                <PenLine size={17} />
              </button>
            )}
            {/* The wrapper exists for the edge cues: absolutely-positioned
                children of the scroller itself would travel with the scrolled
                content, so the fades anchor to this non-scrolling parent. It
                also owns the flex sizing so the scroller keeps filling the
                row. */}
            <div className="relative min-w-0 flex-1">
              <div ref={attachControlRow} data-testid="composer-control-row" className="flex items-center gap-0.5 overflow-x-auto">

              {onAutoNudgeClick && (
                <AutoNudgePopover
                  slotKey={slotId || ''}
                  loop={autoNudgeLoop || null}
                  open={autoNudgeOpen || false}
                  onOpenChange={v => onAutoNudgeClick(v)}
                  onChange={onAutoNudgeChange || (() => {})}
                  // Same condition as the Resume placeholder (`resumeOffered`):
                  // whenever the composer says "press Resume", the loop chip
                  // must not pulse as if a cycle were executing.
                  interrupted={resumeOffered}
                />
              )}
              {!isMobile && approvalMode && (
                <ApprovalModePicker mode={approvalMode} slotKey={activeSlot || ''} openSignal={approvalPickerSignal} nudge={approvalNudgeActive} onNudgeDismiss={dismissApprovalNudge} onNudgeHide={hideApprovalNudge} />
              )}
              </div>
              {/* Edge cues, same treatment as the sibling strips that already
                  ship it (FollowUpBar's scroll row, SidePanelLayout's tab
                  strip): at narrow widths the loop chip and approval picker
                  clip silently, and the overlay scrollbar on macOS/iOS leaves
                  no idle trace. from-bg-elevated matches the composer surface.
                  Deliberately NO z-index: positioned elements already paint
                  above the row's in-flow buttons, and an explicit z-10 would
                  win the tree-order tiebreak against the optimizing dim
                  overlay (also z-10, earlier in the tree), punching an
                  undimmed wedge through it. */}
              {controlRowEdges.left && (
                <div aria-hidden="true" data-testid="control-row-cue-left" className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 bg-gradient-to-r from-bg-elevated to-transparent" />
              )}
              {controlRowEdges.right && (
                <div aria-hidden="true" data-testid="control-row-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 bg-gradient-to-l from-bg-elevated to-transparent" />
              )}
            </div>
            {isMobile && approvalMode && (
              <ApprovalModePicker mode={approvalMode} slotKey={activeSlot || ''} compact openSignal={approvalPickerSignal} nudge={approvalNudgeActive} onNudgeDismiss={dismissApprovalNudge} onNudgeHide={hideApprovalNudge} />
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {onVoiceToggle && (
              <button
                type="button"
                className={`w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all border-none ${
                  voiceRecording ? 'bg-danger-subtle text-danger animate-pulse' : (!micIsModeSwitch && transcribeInFlight) ? 'bg-accent-subtle text-accent' : voiceHoldMode ? 'bg-accent-subtle text-accent' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'
                } disabled:opacity-30`}
                // The mic does whichever voice thing is AVAILABLE right now, which is
                // what keeps it from becoming a dead control. On an empty composer
                // that is the mode switch. With a draft, hold mode is suspended
                // anyway (a hold bar cannot show text you need to read and edit),
                // so the mic reverts to the job it had before this feature: tap to
                // dictate, transcript spliced in at the caret.
                //
                // Without that second branch the switch was disabled on every draft,
                // on every coarse-pointer device — including for someone who never
                // opened hold mode — and since the mic is the only voice entry point
                // on touch, dictating onto existing text became impossible. Speak,
                // glance, speak again is how a long message actually gets composed
                // on a phone, so losing it is not a cost of the new mode; it would
                // have been an unconditional regression in the old one.
                onClick={micIsModeSwitch ? toggleVoiceMode : onVoiceToggle}
                // Prewarm only when the press will actually record. On the switch it
                // would acquire the mic for a press that changes layout, and in hold
                // mode the gesture's own pointerdown opens capture earlier anyway.
                onPointerDown={micIsModeSwitch ? undefined : onVoicePrewarm}
                /* A foreign transcription blocks STARTING a capture, so it gates the
                   mic only while the mic is the record button. As a MODE SWITCH the
                   click starts nothing — it hands the keyboard back — and disabling
                   it there strands the user in voice mode, unable to type or send
                   until unrelated work in another session finishes. */
                disabled={disabled || optimizing || (micIsModeSwitch ? captureInFlight : transcribeInFlight)}
                aria-label={micLabel}
                title={micLabel}
              >
                {!micIsModeSwitch && transcribeInFlight ? <Loader2 size={18} className="animate-spin" /> : voiceHoldMode ? <Keyboard size={18} /> : <Mic size={18} />}
              </button>
            )}
            {/* The busy branch is reachable with EITHER a stop affordance or a
                steer path: a host without onStop (the side panel — stopping the
                main turn from there would be misdirected) still needs the
                split steer/queue button while a turn runs. */}
            {(isRunning || stopState === 'soft_pending' || stopState === 'killing') && (onStop || (canSteer && onSteer)) ? (
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
              ) :
              // Deliberately NOT gated on hasSessionRefs, unlike the idle send
              // button below. This branch is the mid-turn split button, whose
              // steer mode refuses a payload of refs alone (ChatPage's steer()
              // bails on `!raw && !files.length`, because a failed steer cannot
              // restore what it cleared). Including refs here would enable a
              // primary button whose press does nothing — and that state was
              // unreachable before session refs existed, since an empty composer
              // mid-turn rendered the stop button instead. A bare ref therefore
              // waits for the turn to end and rides the idle send button.
              composerHasDraft ? (
                canSteer && onSteer ? (
                  <BusySendButton
                    mode={busySendMode}
                    onModeChange={setBusySendMode}
                    onFire={fireComposer}
                    disabled={disabled}
                  />
                ) : (
                  <button className="w-8 h-8 rounded-full bg-warn text-warn-fg border-none flex items-center justify-center cursor-pointer hover:bg-warn/80 disabled:opacity-30 disabled:cursor-not-allowed transition-all" onClick={fireComposer} disabled={disabled} title={i18nT('components.chatInput.queue_message')} aria-label={i18nT('components.chatInput.queue_message')}>
                    <ArrowUpFromLine size={18} />
                  </button>
                )
              ) : onStop ? (
                <button className="w-8 h-8 rounded-lg bg-transparent border-none text-danger hover:bg-danger/10 flex items-center justify-center cursor-pointer transition-all" onClick={onStop} title={i18nT('components.chatInput.stop_generation')} aria-label={i18nT('components.chatInput.stop_generation')} data-testid="stop-button-armed">
                  <Square size={18} fill="currentColor" />
                </button>
              ) : (
                // No stop affordance and nothing typed: keep the split button
                // in place (disabled) so the composer's shape does not jump
                // when the first character lands.
                <BusySendButton
                  mode={busySendMode}
                  onModeChange={setBusySendMode}
                  onFire={fireComposer}
                  disabled
                />
              )
            ) : (<>
              {promptOptimizer && <button
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
              </button>}
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
              {continuable && onContinue && !value.trim() && !pendingFiles.length && !hasSessionRefs ? (
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
                disabled={(!value.trim() && !pendingFiles.length && !hasSessionRefs) || disabled || optimizing || !connected}
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
          {!!projectBranch && !!gitBadgeTitle && (
            /* Working-tree badge: dirty count (warn pill) plus ahead/behind
               arrows, the Git panel's own vocabulary. Renders only when there
               is signal, so a clean in-sync tree keeps the footer as it was.
               A passive READOUT, not a button: the shelf row already carried
               three actions on base (agent, picker, copy) and
               max-two-buttons-per-row forbids growing a 3+ row, exactly like
               the context readout on the right. The Git panel stays one click
               away in the sidebar. NOT gated on shelfCompact: icon+digits
               have no text label to shed, and hiding the badge at narrow
               widths would remove the only tree-state signal
               (narrow-viewport-required). */
            <span
              className="inline-flex items-center gap-1 h-7 shrink-0 text-[11px] font-mono px-1.5 text-muted"
              role="status"
              title={gitBadgeTitle}
              aria-label={gitBadgeTitle}
            >
              {!!projectGitDirty && (
                /* The icon makes the count read as "changed files" on a cold
                   look — a bare warn number beside a branch name could be
                   anything (UX review finding). */
                <span className="inline-flex items-center gap-0.5 px-1 py-px rounded bg-warn/15 text-warn">
                  <FileDiff size={11} className="shrink-0" />
                  {projectGitDirty}
                </span>
              )}
              {(!!projectGitAhead || !!projectGitBehind) && (
                <span>
                  {!!projectGitAhead && <>&#x2191;{projectGitAhead}</>}
                  {!!projectGitBehind && <>{projectGitAhead ? ' ' : ''}&#x2193;{projectGitBehind}</>}
                </span>
              )}
            </span>
          )}
          </div>
          <div className="flex items-center shrink-0">
          {contextPct != null && (() => {
            const pct = Math.round(contextPct)
            const win = contextWindowTokens || 0
            const used = contextUsedTokens != null ? contextUsedTokens : (win ? Math.round((pct / 100) * win) : 0)
            const remaining = win ? Math.max(win - used, 0) : 0
            const approx = contextUsedTokens == null
            const pctColor = contextColor(contextPct)
            const showAnyReadout = !!(showContextPct || showContextTokens)
            // Graceful degrade: on a narrow shelf, collapse to the percentage
            // alone (or tokens, if that's the only segment enabled) so the
            // readout never crowds out the agent/model controls.
            const readout = shelfCompact
              ? composeContextReadout(contextPct, used, win, { approx, showPct: showContextPct, showTokens: !!showContextTokens && !showContextPct })
              : composeContextReadout(contextPct, used, win, { approx, showPct: showContextPct, showTokens: showContextTokens })
            return (
            <div ref={ctxWrapRef} className="relative flex items-center">
              <button
                className={`inline-flex items-center h-7 px-2.5 rounded-md transition-colors border-none cursor-pointer ${ctxPopoverOpen ? 'bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]' : 'bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))]'}`}
                onClick={() => setCtxPopoverOpen(o => !o)}
                title={contextTip(contextPct)}
                aria-label={i18nT('components.chatInput.context_usage')}
              >
                <ContextBar pct={contextPct} width={40} height={3} />
                {showAnyReadout && <span className="text-[11px] ml-1.5 tabular-nums whitespace-nowrap" style={{ color: pctColor }}>{readout}</span>}
              </button>
              {ctxPopoverOpen && (
                <div className="absolute bottom-full right-0 mb-1 z-[60] w-52 rounded-xl border border-border bg-bg-elevated shadow-xl p-3 animate-slide-up">
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-[11px] font-semibold text-text">{i18nT('components.chatInput.context_window')}</span>
                            <span className="text-[12px] font-mono font-bold" style={{ color: pctColor }}>{fmtPercent(contextPctClamped(contextPct) / 100)}</span>
                          </div>
                          <div className="flex flex-col gap-1 text-[11px] font-mono">
                            <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.used')}</span><span className="text-text">{approx ? '~' : ''}{fmtTokens(used)}</span></div>
                            <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.remaining')}</span><span className="text-text">{approx ? '~' : ''}{fmtTokens(remaining)}</span></div>
                            <div className="flex justify-between"><span className="text-muted">{i18nT('components.chatInput.total')}</span><span className="text-text">{fmtTokens(win)}</span></div>
                          </div>
                          {modelName && (
                            <div className="mt-2 pt-2 border-t border-border flex justify-between text-[11px] font-mono">
                              <span className="text-muted">{i18nT('components.chatInput.model')}</span><span className="text-text truncate max-w-[120px]" title={modelName}>{modelName}</span>
                            </div>
                          )}
                          {autoCompactQuery.isLoading && (
                            <div className="mt-2 pt-2 border-t border-border" aria-hidden="true">
                              <div className="h-4 mb-1 rounded bg-bg-hover animate-pulse" />
                              <div className="h-5 rounded bg-bg-hover animate-pulse" />
                            </div>
                          )}
                          {autoCompactQuery.isError && !autoCompact && (
                            <div className="mt-2 pt-2 border-t border-border text-[10px] text-muted">
                              {i18nT('components.chatInput.auto_compact_load_failed')}
                            </div>
                          )}
                          {autoCompact && (
                            <div className="mt-2 pt-2 border-t border-border">
                              <div className="flex items-center justify-between mb-1">
                                <span className="text-[11px] text-muted">{i18nT('components.chatInput.auto_compact_at')}</span>
                                <span className="text-[12px] font-mono font-bold text-accent">{fmtPercent(Math.round(autoCompact.pct ?? autoCompact.global_pct) / 100)}</span>
                              </div>
                              <Slider
                                value={autoCompact.pct ?? autoCompact.global_pct}
                                onChange={v => pushAutoCompact(v)}
                                min={autoCompact.min}
                                max={autoCompact.max}
                                step={1}
                                formatValue={v => fmtPercent(v / 100)}
                                aria-label={i18nT('components.chatInput.auto_compact_threshold')}
                              />
                              {autoCompact.pct != null ? (
                                <Btn
                                  className="mt-1 px-0 py-0 border-none text-[10px] text-muted underline hover:text-text hover:bg-transparent"
                                  onClick={() => pushAutoCompact(null)}
                                >
                                  {i18nT('components.chatInput.reset_to_global', { pct: Math.round(autoCompact.global_pct) })}
                                </Btn>
                              ) : (
                                <div className="mt-1 text-[10px] text-muted">{i18nT('components.chatInput.following_global', { pct: Math.round(autoCompact.global_pct) })}</div>
                              )}
                            </div>
                          )}
                  </div>
              )}
            </div>
            )
          })()}
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
