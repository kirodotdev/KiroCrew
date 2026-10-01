import { useRef, useEffect, useMemo, useId, memo, lazy, Suspense } from 'react'
import { markComposerResize } from '../utils/composerResize'
import { ArrowUp, Loader2, RotateCw, Sparkles, Target, CheckCircle, Lock, FolderOpen, ClipboardList, PenLine, MoreHorizontal } from 'lucide-react'
import SketchDialog from './SketchDialog'
import CopyBranchButton from './CopyBranchButton'
import RejectDropdown from './RejectDropdown'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useBranding } from '../hooks/useBranding'
import { useAppStore, useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import { useSlotId } from '../providers/SlotContext'
import { ToolDetails } from '../pages/chat/ToolDetails'
import { safeSetItem } from '../utils/safeStorage'
import { offlineProps } from '../utils/offline'
import { motion, AnimatePresence } from 'framer-motion'
import { useComposerSpellcheck } from '../hooks/useComposerSpellcheck'
import { useComposerSendMode } from '../hooks/useComposerSendMode'
import TrustDropdown from './TrustDropdown'
import { useIsMobile } from '../hooks/useIsMobile'
import { isTouchDevice } from '../utils/isTouchDevice'
import ErrorNotice from './ErrorNotice'
import PromptLengthNotice from './PromptLengthNotice'
import { useImeGuard } from '../hooks/useImeGuard'
import PasteHighlightLayer, { INPUT_TYPO } from './PasteHighlightLayer'
import PasteHoverLayer from './PasteHoverLayer'
import FollowUpBar from './FollowUpBar'
import { platformShortcut } from '../utils/platform'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { useComposerDraftText, useComposerVoiceSlice, type ComposerVoiceInputProps } from '../chat-core/composer/Composer'
import { useComposerTreeDrop } from './composerTreeDrop'
import { useStopEscapeHatch } from '../hooks/useStopEscapeHatch'
import { useStopDeclinedHint } from '../hooks/useStopDeclinedHint'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from './ui/dropdown-menu'
import { i18nT } from '../i18n/t'
import { fmtDateFields } from '../i18n/format'
import SessionRefStrip from './SessionRefStrip'
import { Glass } from './Glass'
import type { ChatInputProps } from './chat-input/props'
import { LexicalComposerInput, ComposerLoadBoundary, useComposerEngine } from './chat-input/engine'
import { approvalBtnClass, useSpawnApprovals, useToolApproval } from './chat-input/approval'
import { SpawnApprovalCard } from './chat-input/SpawnApprovalCard'
import { useComposerPickers } from './chat-input/pickers'
import { ComposerPickerMenus } from './chat-input/PickerMenus'
import { useDictationControls, useHoldToTalk } from './chat-input/voice'
import { HoldToTalkBar, MicButton, VoiceCaptureStatus } from './chat-input/VoiceControls'
import { AgentChip, ContextUsageControl, ModelChip, SessionControlChips, useContextPopover, useShelfMeasure } from './chat-input/ContextShelf'
import { useAutoCompactThreshold } from './chat-input/autoCompact'
import { AttachMenu, usePlusMenu } from './chat-input/attach'
import { BusySendControls, CompactingIndicator, useComposerSend } from './chat-input/busySend'
import { CollapsedComposerBar, collapseMenuRowElement, useComposerCollapse } from './chat-input/collapse'
import { useComposerFocus, useComposerKeyDown, useEditorInput } from './chat-input/keyboard'
import { INPUT_DRAG_MIN_H, useManualHeight, useStripHeights, useTextareaAutosize } from './chat-input/sizing'
import { usePromptHistory, useUndoHistory } from './chat-input/draftHistory'
import { usePromptOptimizer } from './chat-input/optimizer'
import { usePasteTokens } from './chat-input/paste'
import { FilePreviewStrip } from './chat-input/FilePreviewStrip'

/* The chat composer. This module is its only import path: the default export
   is the memoized component every host renders, and the named exports below
   are the surface other modules and specs import from here. The responsibilities
   live in `./chat-input/` -- each owner exports the hooks this component calls
   (in the order their effects must run) and the controls it renders -- and none
   of them imports this module. What stays here is the wiring, the layout
   skeleton, and the constructs other gates read in this file by path. */

const SessionAutomationPopover = lazy(() => import('./SessionAutomationPopover'))

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
// Audio keeps the normal 50 MB cap while streaming through the server's media
// gate. Extensions align the picker exactly with the verified server allowlist
// instead of broadening the dialog to neighboring unsupported formats.
const AUDIO_ACCEPT = '.mp3,.m4a,.wav,.ogg,.oga,.opus,.flac'
const FILE_ACCEPT = IMAGE_ACCEPT + ',' + VIDEO_ACCEPT + ',' + AUDIO_ACCEPT + ',.txt,.text,.xwiki,.md,.json,.jsonl,.excalidraw,.har,.yaml,.yml,.xml,.drawio,.csv,.tsv,.log,.py,.js,.ts,.tsx,.jsx,.html,.css,.sh,.bash,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.odp,.rtf,.zip,.tar,.gz'

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
export { UNATTENDED_APPROVAL_SOURCES } from './chat-input/approval'
export type { ComposerBusyMode } from './chat-input/props'

/** No Voice atom mounted: every dictation value at its idle default. */
const NO_VOICE: Partial<ComposerVoiceInputProps> = {}

function ChatInput({
  aboveComposer,
  value: valueProp,
  onChange,
  onSend,
  canSteer,
  onSteer,
  jevAutoAvailable = false,
  busyMode = 'split',
  disabled: disabledProp = false,
  placeholder = '',
  prefillHint,
  onScreenshot,
  onUploadFiles,
  uploading = false,
  onCancelUpload,
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
  agentName,
  agentLabel,
  agentIsInheritedDefault,
  modelIsInheritedDefault,
  modelIsAutoChosen,
  modelIsJevRouted,
  agentSource,
  modelName,
  onAgentClick,
  onModelClick,
  onProjectClick,
  sessionControls,
  onSessionControlClick,
  contextPct,
  contextUsedTokens,
  contextWindowTokens,
  showContextPct,
  showContextTokens,
  isRunning = false,
  compacting = false,
  stopDeclined = false,
  onStop,
  continuable = false,
  continueIsRecovery = false,
  onContinue,
  continuing = false,
  isQueued = false,
  stopState,
  approvalMode,
  reasoningEffort,
  effortIsDefault = false,
  hasEffort,
  providerId: _providerId,
  onFileSelect,
  onTreeEntryDrop,
  clampDropOffset,
  onFileOpen,
  project,
  projectBranch,
  projectDetached,
  memoryMode,
  sentMessages,
  onEditLastRequest,
  onAutomationClick,
  automation,
  automationOpen,
  onAutomationChange,
  automationCreationReady,
  automationSnapshotFailed,
  sessionMode,
  sendOnEnter: sendOnEnterProp,
  followUpOptions,
  followUpPicked,
  onFollowUpSelect,
  onFollowUpSend,
  quickSend,
  followUpLayout,
  followUpSourceKey,
  followUpPendingOptions,
  followUpRefusedOptions,
  followUpError,
  pasteBlocks = [],
  onPasteBlocksChange,
  showFullPastes = false,
  lexicalComposer = false,
  knowledgeChip,
  autoFocusKey,
  inputAriaLabel,
  typedCommandMenus = true,
  slotApprovalChrome = true,
  promptOptimizer = true,
  collapsible = false,
  connected = true,
  onOptimizeResult,
}: ChatInputProps) {
  // Under a `<Composer draft>` root the text arrives through the root's store,
  // subscribed HERE, so a keystroke re-renders this composer and not its host.
  const draftText = useComposerDraftText()
  const value = draftText ?? valueProp ?? ''
  // Dictation state comes from the Composer root's Voice atom (mounted by the
  // root beside this input), not from host-wired props: one hook, the same
  // values the atom computes for every surface, and a host cannot forget to
  // wire it. Null outside a `<Composer>` root — then there is simply no mic.
  const composerVoice = useComposerVoiceSlice()
  const {
    voiceRecording = false,
    onSelectVoiceDevice,
    voiceDeviceSwitchIsLive = false,
    voiceTranscribing = false,
    voiceTranscribeActive,
    voiceDrainCancellable = false,
    voiceBusyElsewhere = false,
    voiceBusyElsewhereSession = null,
    voiceHeldLanded = false,
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
  } = composerVoice?.inputProps ?? NO_VOICE
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const disabled = disabledProp
  const dispatch = useAppDispatch()
  const slotId = useSlotId()
  // The store handle, read at click time (not subscribed) so "Optimize prompt"
  // can pull THIS pane's slot messages without re-rendering every composer on
  // each streamed frame. useAppStore returns the Provider-injected store, the
  // same pattern ChatPane uses for its at-send reads.
  const chatStore = useAppStore()
  const {
    pendingApproval, hasApproval, approvalId, approvalSubmitting, approvalPickerSignal, setApprovalPickerSignal,
    approvalModeAdjusted, approvalNudgeActive, dismissApprovalNudge, hideApprovalNudge,
    approvalNotice, setApprovalNotice, approvalNoticeKind,
    approvalToolInput, approvalIsReadOnly, approvalFullCommand, approvalBaseCommand, approvalIsShell,
    approvalTrustCommandGrantable, approvalTrustBaseGrantable, approvalTrustAllGrantable, approvalIsUnattended, approvalTrustGrantable,
    approvalLabelRaw, approvalToolCallId, approvalPurpose, approvalTs, approvalLabel, showGhost, showInChat, handleApprovalAction,
  } = useToolApproval({ slotId, slotApprovalChrome, approvalMode, dispatch })
  const activeSlot = slotId
  // Read the composer-spellcheck preference here rather than as a prop, so every
  // render site of this component honours it and none can forget to pass it.
  const spellCheck = useComposerSpellcheck()
  // Same for the send-key mode: the stored preference is the fallback, not a
  // hardcoded 'enter'. A host omitting the prop (session-grid pane, side panel)
  // would otherwise send on plain Enter for a user who chose Ctrl/Cmd+Enter.
  const storedSendMode = useComposerSendMode()
  const sendOnEnter = sendOnEnterProp ?? storedSendMode

  // Stop button: killing-state escape hatch (re-enable after 15s)
  const { escaped: killingEscaped } = useStopEscapeHatch(stopState)
  // Timed client-side from the frame that carried the decline; see the hook.
  const stopDeclinedArmed = useStopDeclinedHint(stopDeclined)

  const spawnApprovals = useSpawnApprovals({ slotId, slotApprovalChrome, dispatch })

  const {
    inputRef, composerAnchorRef, lexicalControlRef, lexicalLoadFailed, setLexicalLoadFailed,
    lexicalFailedNoticeDismissed, setLexicalFailedNoticeDismissed, lexicalControlRevision, markLexicalReady,
    composerControl, setTextareaRef,
  } = useComposerEngine({ lexicalComposer })
  // Whether the editor held focus when the model chip was pressed. Taken on
  // `mousedown`, which runs BEFORE the browser's default action moves focus
  // onto the chip — by `click` the editor has already lost it. Consumed and
  // cleared by the chip's `click`, so a keyboard activation (no mousedown; the
  // chip itself is focused) reads false rather than a stale press.
  const modelChipPressedFromComposerRef = useRef(false)
  // Attribute the Lexical editor's own growth to the composer (see
  // composerResize.ts). The textarea path attributes inside `applyHeight`, which
  // the contenteditable never runs — its box grows through CSS min/max-height as
  // content changes — so without this observer the transcript would chase every
  // line the editor gains, recreating the composer bounce on the opt-in path.
  // Height only: a width change is window- or panel-driven, not composer-driven,
  // and must stay attributable to whatever caused it.
  useEffect(() => {
    if (!lexicalComposer || lexicalLoadFailed) return
    if (typeof ResizeObserver === 'undefined') return
    const root = lexicalControlRef.current?.getRootElement()
    if (!root) return
    let lastHeight = root.offsetHeight
    const ro = new ResizeObserver(() => {
      const height = root.offsetHeight
      if (height !== lastHeight) {
        lastHeight = height
        markComposerResize()
      }
    })
    ro.observe(root)
    return () => ro.disconnect()
  }, [lexicalComposer, lexicalLoadFailed, lexicalControlRevision, lexicalControlRef])
  const queryClient = useQueryClient()
  const pickers = useComposerPickers({ project, onFileSelect, typedCommandMenus, value, onChange, composerControl, queryClient, slotId, agentName })
  const { anyPickerOpenRef, closePickers, openPickersForText, prefetchSkills } = pickers
  const { publishLexicalSelection, recordCaret, showDictation, cancelVoiceDrain } = useDictationControls({
    composerControl, value, autoFocusKey, anyPickerOpenRef, voiceCaretRef, voicePendingCaretRef, voiceDictationPanel,
    voiceRecording, voiceError, voiceSampleRef, voiceTranscribing, onVoiceCancel, onVoiceToggle,
  })
  const wrapperRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const fileInputId = useId()
  const { shelfRef, shelfHeight, shelfCompact, shelfTiny } = useShelfMeasure()
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
  const { ctxPopoverOpen, setCtxPopoverOpen, ctxWrapRef } = useContextPopover()
  const plus = usePlusMenu({ pickers, value, onChange, composerControl })
  const { setPlusOpen, sketchOpen, setSketchOpen } = plus
  // Client-side `accept` is a UX hint only (input-validation guidance: server enforces type via
  // magic bytes, size, and malware scanning — never trust the extension/MIME here).
  const openPicker = (imageOnly: boolean) => {
    const el = fileInputRef.current
    if (!el) return
    el.accept = imageOnly ? IMAGE_ACCEPT : FILE_ACCEPT
    el.click()
    setPlusOpen(false)
  }
  const { effectiveBusyMode, setBusySendMode, steerOnly, overLimitPending, fireComposer, stopWithTap, sendFollowUp } = useComposerSend({
    slotId, busyMode, isRunning, stopState, canSteer, onSteer, jevAutoAvailable, disabled, voiceTranscribing, value, pasteBlocks, contextWindowTokens,
    pendingFilesCount: pendingFiles.length, pendingSessionsCount: pendingSessions.length, onSend, onStop, onFollowUpSend,
  })
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
  const hasAutomation = !!onAutomationClick
  useEffect(() => { remeasureControlRow() }, [hasAutomation, automation, approvalMode, isMobile, remeasureControlRow])
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
    ? i18nT('components.chatInput.turn_interrupted_press_resume')
    : ''
  const continueLabel = i18nT(continueIsRecovery
    ? 'components.chatInput.resume_interrupted_turn'
    : 'components.chatInput.continue_thread')
  const autoCompactThreshold = useAutoCompactThreshold({ activeSlot, ctxPopoverOpen, queryClient, dispatch })
  const { composerCollapsed, collapsedBarRef, collapseComposer, expandComposer, collapsedDraftLine } = useComposerCollapse({ collapsible, composerControl, value })
  const collapseMenuRow = collapsible ? collapseMenuRowElement(() => { setPlusOpen(false); collapseComposer() }) : null
  // Refs mirror frequently-changing props/state read from inside the keydown handler
  // so it doesn't re-create on every keystroke.
  const valueRef = useRef(value)
  valueRef.current = value
  // Mirror the paste blocks so the undo-recording effect (keyed on
  // [value, autoFocusKey], not pasteBlocks) always snapshots the freshest set.
  const pasteBlocksRef = useRef(pasteBlocks)
  pasteBlocksRef.current = pasteBlocks
  // True when the latest `value` change came from a real DOM edit (user typing,
  // IME, execCommand) rather than a parent-driven prop change (slot draft
  // restore). Lets the slot-settling logic tell a keystroke apart from the
  // draft restore regardless of whether ChatPage restores sync or async.
  const valueFromUserRef = useRef(false)
  // Written by the optimizer during render; the undo recorder (called before
  // it) and the keydown handler read it at effect and event time.
  const optimizingRef = useRef(false)

  useComposerFocus({ autoFocusKey, disabled, isMobile, composerControl, lexicalControlRevision, typedCommandMenus, composerCollapsed, expandComposer })
  const { manualHeight, setManualHeight, isTouch, dragging, dragMinHRef, inputResize, resizeHandleLifecycleRef, resetHeight } = useManualHeight({
    wrapperRef, pendingFilesCount: pendingFiles.length, pendingSessionsCount: pendingSessions.length,
  })
  const promptHistory = usePromptHistory()

  // Reset manual height when input is cleared (new message sent)
  const prevValueRef = useRef(value)
  useEffect(() => {
    if (prevValueRef.current && !value) {
      resetHeight()
      // Picker open state is derived only in the editor's own change handler, so
      // the parent-driven send-clear would otherwise leave a stale menu open.
      closePickers()
    }
    // Exit history mode when value diverges from the recalled message
    // (user edited it, or the send pipeline cleared it).
    promptHistory.exitIfDiverged(value)
    prevValueRef.current = value
  }, [value, resetHeight, closePickers, promptHistory])

  // ChatInput is one instance shared by every slot, so a switch would carry the
  // previous tab's menu over; an unsent draft never hits the clear above.
  useEffect(() => {
    closePickers()
    // Prompt-history browsing belongs to the slot it started in.
    promptHistory.endBrowsing()
  }, [slotId, closePickers, promptHistory])

  const { handleUndoKey, appendBoundary, removeFileEndingUndoBurst, removeDirEndingUndoBurst } = useUndoHistory({
    value, pasteBlocks, autoFocusKey, composerControl, pasteBlocksRef, valueFromUserRef, optimizingRef, onChange, onPasteBlocksChange, onRemoveFile, onRemoveDir, inputRef, ime,
  })
  const { optimizeError, setOptimizeError, optimizePending, optimizing, optimizePrompt } = usePromptOptimizer({
    slotId, chatStore, valueRef, pasteBlocks, onChange, onOptimizeResult, lexicalComposer, lexicalLoadFailed, composerControl, inputRef,
    valueFromUserRef, optimizingRef, appendUndoBoundary: appendBoundary,
  })
  // A file-tree row dropped here goes to the host's "Add to chat" handler;
  // OS file and text drags fall through to the host's handlers.
  const treeDrop = useComposerTreeDrop({
    enabled: !disabled && !optimizing,
    project: project ?? '',
    onTreeEntryDrop,
    clampDropOffset,
    getControl: composerControl,
    containerRef: wrapperRef,
    onDragOver,
    onDragLeave,
    onDrop,
  })
  const {
    mirrorRef, hoverRef, pastePreviewPanelId, setPastePreviewPanelId, rawPasteRef,
    handleTokenKey, handlePaste, handleTextareaClick, handleSelectSnap, handleCopy, handleCut, handleFileInputChange,
  } = usePasteTokens({ value, onChange, pasteBlocks, onPasteBlocksChange, showFullPastes, onUploadFiles, inputRef, valueRef, valueFromUserRef, recordCaret, ime })
  const handleKeyDown = useComposerKeyDown({
    rawPasteRef, handleUndoKey, handleTokenKey, promptOptimizer, connected, optimizePrompt, sendOnEnter, onChange, optimizingRef,
    fireComposer, ime, sentMessages, onEditLastRequest, anyPickerOpenRef, promptHistory, valueRef, inputRef,
  })
  const { handleTextareaChange, handleLexicalChange } = useEditorInput({ onChange, valueFromUserRef, openPickersForText, recordCaret, lexicalControlRef, voiceCaretRef })

  const hasSessionRefs = pendingSessions.length > 0
  const { fileStripRef, sessionStripRef, stripH } = useStripHeights({
    pendingFilesCount: pendingFiles.length, pendingDirsCount: pendingDirs.length, hasSessionRefs, setManualHeight, dragMinHRef,
  })
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
  const {
    transcribeInFlight, transcribingIsHonest, micHeldElsewhere, micBlocked, micOwnerTitle, micHeldElsewhereLabel,
    setHoldTarget, touchPtt, voiceHoldMode, micIsModeSwitch, voiceSettling, textareaParked, toggleVoiceMode, micLabel, holdBarLabel,
    voiceModePlaceholder,
  } = useHoldToTalk({
    onVoiceStart, onVoiceStop, onVoiceCancel, voiceCaptureActive, voiceRecording, voiceTranscribeActive, voiceTranscribing, voiceDownload,
    voiceBusyElsewhere, voiceBusyElsewhereSession, composerHasDraft, disabled, optimizing, placeholder, showDictation,
  })
  // The name in the status row is the way there: one click switches to the
  // chat that holds the mic, where the user can end the capture.
  const micHeldElsewhereAction = micOwnerTitle && voiceBusyElsewhereSession
    ? { label: micOwnerTitle, onClick: () => { void dispatch(switchSlot({ key: voiceBusyElsewhereSession, announceOnMissing: true })) } }
    : undefined
  const activePlaceholder = !connected ? i18nT('components.chatInput.gateway_offline_message_will_not_send') : disabledProp ? i18nT('components.chatInput.stopping') : voiceRecording ? i18nT('components.chatInput.recording_click_mic_to_stop') : transcribingIsHonest ? i18nT('components.chatInput.transcribing_please_wait') : continuePlaceholder || voiceModePlaceholder || resolvedPlaceholder
  // The sigil hint is a label and may be cut to one line. Every other
  // placeholder here is a sentence the user needs whole, so it still wraps —
  // including a caller's own `placeholder`, which `resolvedPlaceholder` carries.
  const placeholderIsHint = !placeholder && activePlaceholder === resolvedPlaceholder
  const { handleInput } = useTextareaAutosize({ inputRef, mirrorRef, value, prefillHint, manualHeight, dragging, textareaParked, activePlaceholder })

  return (
    // 'input-area' is a stable theming hook — see website/docs/theming-contract.md
    <div className={`input-area px-4 pb-1 ${hasApproval ? 'pt-0' : 'pt-1'} mx-auto w-full flex flex-col`}
      style={{ maxWidth: 'var(--mc-input-width, 900px)', ...(manualHeight !== null ? { minHeight: (INPUT_DRAG_MIN_H + stripH) + 'px' } : {}) }}>

      {/* Knowledge context chip */}
      {!showGhost && knowledgeChip}

      {/* Ghost follow-up bubbles floating above input */}
      {!showGhost && followUpOptions && followUpOptions.length > 0 && onFollowUpSelect && (
          <FollowUpBar options={followUpOptions} picked={followUpPicked ?? new Set()} onSelect={onFollowUpSelect} onSend={sendFollowUp} quickSend={quickSend} layout={followUpLayout} sourceKey={followUpSourceKey} pendingOptions={followUpPendingOptions} refusedOptions={followUpRefusedOptions} error={followUpError} />
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
          undo it. See `useManualHeight` (chat-input/sizing.ts) for why the
          persisted value is disregarded there too.

          Its 6px box is ALSO the only thing separating the strip above (the
          options row, the tip band) from the composer box — so dropping the
          handle on touch dropped that separation with it, and the options row sat
          flush against the input. Touch therefore keeps the box and drops only
          the affordance, which puts the composer at the same offset under both
          pointer types instead of leaving the gap a side effect of a
          pointer-only control.

          A COLLAPSED composer takes the same branch, for the same reason stated
          the other way round: there is no box left to resize, so the affordance
          would pin a height nobody can see being pinned — while the 6px box is
          still the only thing separating the strip above from the bar that
          replaces the composer. Keep the box, drop the affordance. */}
      {!showGhost && (isTouch || composerCollapsed
        ? <div aria-hidden="true" data-testid="composer-top-gap" className="h-[6px] shrink-0" />
        : <div
        ref={resizeHandleLifecycleRef}
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

      <SpawnApprovalCard {...spawnApprovals} hasApproval={hasApproval} />

      {/* Approval bar — always-visible button row, with a "ghost pill"
       *  detail mirror that grows in when the inline pill scrolls out of
       *  viewport. Buttons stay anchored on the same row across both states
       *  for stable muscle memory.
       *
       *  Two stacked <AnimatePresence>s:
       *    outer  → mounts/unmounts the whole bar with the approval lifecycle
       *    inner  → toggles the ghost pill based on inline-pill viewport state
       */}
      {/* The dock pane. ONE Liquid Glass surface (components/Glass.tsx) holds the
          approval bar, the notices, the composer and the collapsed bar, so a bar
          fused to the composer's top shares its pane instead of meeting it at a
          seam; it is always mounted so an approval landing never remounts the
          editor. It wears the same neutral `glass-shadow` as every other glass
          pane (and, like every glass pane, does not change on focus -- no theme
          color, no step; the caret is the indicator), and adds the approval glow
          while a decision is pending: the glow takes the shadow slot. */}
      <Glass
        radius={16}
        data-testid="composer-dock"
        className={hasApproval ? 'glass-shadow approval-glow' : 'glass-shadow'}
      >
      <AnimatePresence>
        {pendingApproval && approvalId && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300, mass: 0.8 }}
          >
          <div className={`bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] ${showGhost ? 'rounded-2xl' : 'rounded-t-2xl border-b border-[color:var(--glass-edge)]'} transition-[border-radius,border-color,border-width] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]`}>
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
                  {/* `data-approval-actions` is the probe `queryPendingApprovalAction`
                      resolves — a keyboard chord lands on this row's first enabled
                      control. A `data-` hook rather than button text because every
                      catalog translates the labels. */}
                  <div data-approval-actions className="flex gap-1.5 flex-wrap items-center">
                      <button disabled={approvalSubmitting} className={approvalBtnClass} onClick={() => handleApprovalAction('approved')}><CheckCircle size={12} className="shrink-0" />{i18nT('components.chatInput.allow_once')}</button>
                      {/* One dropdown carries every standing grant this card can
                          record. Trust-reads is a tier inside it, not a sibling
                          button: the row is capped at three controls
                          (`max-two-buttons-per-row` grandfathers Allow once +
                          Trust + Reject and forbids a fourth), and a read-only
                          scopeless card can offer reads and session trust at
                          once. */}
                      {approvalTrustGrantable && (approvalTrustCommandGrantable || approvalTrustAllGrantable || approvalIsReadOnly) && (
                        <TrustDropdown
                            fullCommand={approvalFullCommand}
                            baseCommand={approvalBaseCommand}
                            isShell={approvalIsShell && approvalTrustBaseGrantable}
                            hasCommand={approvalTrustCommandGrantable}
                            trustReadsLabelKey={approvalIsReadOnly ? 'components.chatInput.trust_reads' : undefined}
                            showTrustAll={approvalTrustAllGrantable || approvalTrustCommandGrantable}
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

      {optimizeError && (
        <div className="px-4 mb-1">
          {/* No hand-off: the composer draft below (the prompt that was restored) is unsaved. */}
          <ErrorNotice
            variant="inline"
            testId="optimize-error"
            message={optimizeError}
            onDismiss={() => setOptimizeError('')}
          />
        </div>
      )}
      {approvalNotice && approvalNoticeKind === 'error' && (
        <div className="px-4 mb-1">
          {/* No hand-off: the composer draft below is unsaved. */}
          <ErrorNotice
            testId="approval-decision-error"
            message={approvalNotice}
            onDismiss={() => setApprovalNotice(null)}
          />
        </div>
      )}
      {approvalNotice && approvalNoticeKind === 'status' && (
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
        <SketchDialog open={sketchOpen} onOpenChange={setSketchOpen} onInsert={onUploadFiles} returnFocusRef={composerAnchorRef} />
      )}

      <ComposerPickerMenus pickers={pickers} value={value} onChange={onChange} composerAnchorRef={composerAnchorRef} sendOnEnter={sendOnEnter} typedCommandMenus={typedCommandMenus} project={project} agentName={agentName} onFileSelect={onFileSelect} onFileOpen={onFileOpen} />

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
          collapsed composer is never a persistently focusable invisible element.

          `composerCollapsed` joins this gate rather than bringing its own
          mechanism, so a user-initiated collapse inherits both properties
          verbatim. The difference is only who asked and how long it lasts: the
          ghost is transient and the app decides it, so it needs no way back,
          while a deliberate collapse persists and therefore does — the bar
          rendered after this block is that way back. */}
      <AnimatePresence initial={false}>
      {!showGhost && !composerCollapsed && (<motion.div
        key="input-container"
        initial={{ opacity: 1, height: 'auto' }}
        animate={{ opacity: 1, height: 'auto' }}
        exit={{ opacity: 0, height: 0 }}
        transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }}
        // This element clips its content for the height:0 exit, and it paints
        // no shadow of its own: it belongs to the Glass dock pane that wraps it
        // (`.glass-shadow`, index.css) and sits outside this clip. The pane does
        // not change on focus (maintainer decision; the caret is the indicator).
        // With an approval box attached above, that pane wears `approval-glow`,
        // whose warn glow takes the shadow slot.
        style={{ overflow: 'hidden' }}
      >{/* File drag-and-drop target. Drag-drop is inherently pointer-only; the
           keyboard-accessible path is the "Attach files" button that opens the
           hidden file input above. Hence the scoped disable for the drop zone. */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        data-testid="input-wrapper"
        ref={wrapperRef}
        className={`${hasApproval ? 'rounded-b-2xl rounded-t-none' : 'rounded-2xl'} relative transition-colors overflow-hidden ${manualHeight !== null ? 'flex flex-col min-h-0' : ''} ${(memoryMode === 'incognito' || memoryMode === 'temporary') ? 'border-2' : 'border'} bg-transparent ${memoryMode === 'temporary' ? 'border-aim' : memoryMode === 'incognito' ? 'border-warn' : 'border-transparent'}`}

        data-tree-drop-active={treeDrop.state === 'accept' ? 'true' : undefined}
        data-tree-drop-refused={treeDrop.state === 'refuse' ? 'true' : undefined}
        onDragOver={treeDrop.onDragOver}
        onDragLeave={treeDrop.onDragLeave}
        onDrop={treeDrop.onDrop}
      >
        {treeDrop.state === 'accept' && (
          <div aria-hidden="true" data-testid="composer-tree-drop-indicator" className="pointer-events-none absolute inset-0 z-10 rounded-[inherit] border-2 border-dashed border-accent bg-accent/5" />
        )}
        {/* Where a release would land: portalled so a transformed ancestor
            cannot offset its viewport coordinates. */}
        {treeDrop.state === 'accept' && treeDrop.caret && createPortal(
          <div
            aria-hidden="true"
            data-testid="composer-tree-drop-caret"
            className="pointer-events-none fixed z-50 w-0.5 rounded-full bg-accent"
            style={{ left: treeDrop.caret.left - 1, top: treeDrop.caret.top, height: treeDrop.caret.height }}
          />,
          document.body,
        )}
        {/* A folder whose path cannot be written as a folder reference: say why
            instead of leaving only the no-drop cursor. */}
        {treeDrop.state === 'refuse' && (
          <div role="status" data-testid="composer-tree-drop-refused" className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-[inherit] border-2 border-dashed border-warn bg-bg-elevated px-4 text-center text-[13px] text-text">
            {i18nT('components.chatInput.tree_drop_folder_refused')}
          </div>
        )}
        <SessionRefStrip refs={pendingSessions} onRemove={onRemoveSessionRef} rootRef={sessionStripRef} />
        <FilePreviewStrip files={pendingFiles} dirs={pendingDirs} resizedInfo={resizedInfo} onRemove={removeFileEndingUndoBurst} onRemoveDir={removeDirEndingUndoBurst} rootRef={fileStripRef} />

        <VoiceCaptureStatus
          voiceHoldMode={voiceHoldMode} touchPtt={touchPtt} showDictation={showDictation} value={value} voicePartial={voicePartial}
          voiceDeviceLabel={voiceDeviceLabel} voiceDeviceId={voiceDeviceId} onSelectVoiceDevice={onSelectVoiceDevice} voiceDeviceSwitchIsLive={voiceDeviceSwitchIsLive}
          voiceStreaming={voiceStreaming} voiceDownload={voiceDownload} voiceRecording={voiceRecording} voiceLevel={voiceLevel} voiceError={voiceError}
          onClearVoiceError={onClearVoiceError} voiceDrainCancellable={voiceDrainCancellable} cancelVoiceDrain={cancelVoiceDrain} onVoiceToggle={onVoiceToggle}
          micHeldElsewhere={micHeldElsewhere} micHeldElsewhereLabel={micHeldElsewhereLabel} micHeldElsewhereAction={micHeldElsewhereAction} voiceHeldLanded={voiceHeldLanded}
        />

        {optimizing && <span className="optimize-overlay absolute inset-0 flex items-center justify-center backdrop-blur-md pointer-events-none z-10 rounded-2xl"><span className="optimize-overlay-pill inline-flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium text-text"><Sparkles size={15} className="text-accent animate-pulse shrink-0" /> {i18nT('components.chatInput.optimizing_prompt')}</span></span>}
        {/* The textarea fallback is seamless for typing (draft intact), but the
            failure itself must be user-visible, not only a console line: the
            person who opted into the editor should know they are no longer in
            it. `askAgent` stays off — the hand-off unmounts the composer and
            the draft is exactly what is not yet saved. */}
        {lexicalLoadFailed && !lexicalFailedNoticeDismissed && (
          <ErrorNotice
            variant="inline"
            message={i18nT('components.chatInput.editor_unavailable_standard_input')}
            onDismiss={() => setLexicalFailedNoticeDismissed(true)}
            testId="composer-fallback-notice"
            className="mb-1 px-1"
          />
        )}
        <div className={`relative ${showDictation || voiceHoldMode ? 'sr-only' : ''} ${manualHeight !== null ? 'flex-1 min-h-0 flex flex-col' : ''}`}>
        {lexicalComposer && !lexicalLoadFailed ? (
          <ComposerLoadBoundary onError={() => setLexicalLoadFailed(true)}>
            <Suspense fallback={
              <div
                role="status"
                aria-label={inputAriaLabel ?? i18nT('components.chatInput.message_input')}
                aria-busy="true"
                className={`relative flex w-full min-h-[44px] items-center px-4 text-muted ${INPUT_TYPO}`}
              >
                <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
              </div>
            }>
              <LexicalComposerInput
                value={value}
                blocks={pasteBlocks}
                onChange={handleLexicalChange}
                onBlocksChange={onPasteBlocksChange}
                showFullPastes={showFullPastes}
                onSend={fireComposer}
                onUploadFiles={onUploadFiles}
                controlRef={lexicalControlRef}
                onReady={markLexicalReady}
                onSelectionChange={publishLexicalSelection}
                sentMessages={sentMessages}
                historyScope={slotId}
                onEditLastRequest={onEditLastRequest}
                ariaLabel={inputAriaLabel ?? i18nT('components.chatInput.message_input')}
                placeholder={activePlaceholder}
                disabled={disabled}
                readOnly={optimizing}
                sendOnEnter={sendOnEnter}
                spellCheck={spellCheck}
                className={manualHeight !== null ? 'flex-1 min-h-0' : ''}
              />
            </Suspense>
          </ComposerLoadBoundary>
        ) : (<>
        <PasteHighlightLayer ref={mirrorRef} value={value} blocks={pasteBlocks} />
        {/* `block` on the textarea is load-bearing for the mirror above. A
            textarea is inline-block by default, so it sits on a line box and
            leaves a descender gap (~7px) under itself. The wrapper grows by that
            gap, the `inset-0` mirror grows with it, and once the draft scrolls
            the taller mirror clamps to a smaller scrollTop than the textarea:
            the paste chip's background drifts off the token text.
            playwright/composer-paste-highlight.spec.ts pins this. */}
        <textarea
          ref={setTextareaRef}
          aria-label={inputAriaLabel ?? i18nT('components.chatInput.message_input')}
          data-composer-input=""
          spellCheck={spellCheck}
          aria-describedby={pastePreviewPanelId ?? undefined}
          data-composer-typo
          // Chromium paints no `text-overflow` on a `::placeholder`, so the cut tail
          // fades out instead, the way the app's other cut edges do.
          className={/* focus-cue-ok: maintainer decision -- the glass dock holding this textarea does not change on focus (no ring, no colour, no shadow step; index.css `.glass-shadow`), and a ring on the textarea itself is not wanted either; the caret is the composer's focus indicator. */ `relative block w-full bg-transparent border-none ${INPUT_TYPO} text-text outline-hidden min-h-[44px] max-h-[50vh] placeholder:text-muted resize-none ${placeholderIsHint ? 'placeholder:whitespace-nowrap placeholder:overflow-hidden placeholder:[mask-image:linear-gradient(to_right,black_calc(100%-1.5rem),transparent)] placeholder:[-webkit-mask-image:linear-gradient(to_right,black_calc(100%-1.5rem),transparent)]' : ''} ${manualHeight !== null ? 'flex-1' : ''} ${disabled ? 'opacity-40 pointer-events-none' : ''} ${optimizing ? 'opacity-30' : ''}`}
          style={manualHeight !== null ? { height: '100%' } : undefined}
          placeholder={activePlaceholder}
          readOnly={optimizing}
          rows={1}
          value={value}
          onDragOver={e => { e.preventDefault(); treeDrop.onDragOver(e); e.stopPropagation() }}
          onDragLeave={e => { treeDrop.onDragLeave(e); e.stopPropagation() }}
          onDrop={e => { e.preventDefault(); treeDrop.onDrop(e); e.stopPropagation() }}
          onChange={handleTextareaChange}
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
        </>)}
        </div>

        {voiceHoldMode && (
          <HoldToTalkBar manualHeight={manualHeight} setHoldTarget={setHoldTarget} touchPtt={touchPtt} disabled={disabled} micBlocked={micBlocked} optimizing={optimizing} voiceSettling={voiceSettling} holdBarLabel={holdBarLabel} />
        )}

        <PromptLengthNotice value={value} blocks={pasteBlocks} contextWindowTokens={contextWindowTokens} confirmPending={overLimitPending} />

        {/* Bottom icon row */}
        <div className="flex items-center justify-between px-2.5 pb-2 pt-0.5">
          <div className="flex items-center gap-0.5 min-w-0">
            <AttachMenu plus={plus} onUploadFiles={onUploadFiles} uploading={uploading} onCancelUpload={onCancelUpload} directFilePicker={directFilePicker} collapsible={collapsible} fileInputId={fileInputId} openPicker={openPicker} isMac={isMac} isMobile={isMobile} onScreenshot={onScreenshot} collapseMenuRow={collapseMenuRow} typedCommandMenus={typedCommandMenus} onFileSelect={onFileSelect} />
            {directFilePicker && collapsible && (
              /* The repo's own overflow mechanism, not a second spelling of it.
                 `max-two-buttons-per-row` names the two files to copy for exactly
                 this shape, and `DetailOverflowMenu.tsx` already answers the same
                 rule the same way -- a labelled MoreHorizontal trigger holding
                 "everything past the second control", whose own comment says
                 "rather than inventing a second overflow shape". The hand-rolled
                 portal that stood here re-implemented top-side anchoring, viewport
                 collision and outside-click that this wrapper does natively, and
                 review was right that the symmetry argument for it (matching the
                 "+" drop-up) was a preference rather than a constraint.

                 The TRIGGER is deliberately NOT disabled while an upload is in
                 flight, though the pencil it replaces was. The pencil hosted one
                 action, so disabling it disabled exactly that action; this hosts
                 the collapse too, and taking the collapse away mid-upload would
                 reintroduce the unreachability this control exists to fix. The
                 guard belongs on the item that needs it, just below. */
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    data-testid="composer-more-trigger"
                    className="w-8 h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover data-[state=open]:text-text data-[state=open]:bg-bg-hover"
                    aria-label={i18nT('components.chatInput.more_actions')}
                    title={i18nT('components.chatInput.more_actions')}
                  >
                    <MoreHorizontal size={17} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent side="top" align="start" className="w-[260px] p-2">
                  {onUploadFiles && (
                    /* `disabled={uploading}` restores a guard the pencil carried and
                       this row lost when Sketch moved in here. Sketch attaches
                       through the same `onUploadFiles` handler, and the in-flight
                       flag is a single shared boolean rather than a counter -- so a
                       sketch attached while another upload is still running lets
                       whichever request finishes first clear the in-flight state for
                       both. Self-correcting and lossless, but the pencil guarded
                       against it and a moved control must not quietly drop a guard.
                       Review caught the omission. */
                    <DropdownMenuItem
                      disabled={uploading}
                      /* Deferred one macrotask, which is this repo's established
                         remedy for opening a dialog from a menu item (see
                         `DrivePage.tsx`'s `openShare`/`openMove`): Radix dispatches
                         item select with `flushSync`, so a dialog opened inline
                         mounts in a commit where the menu is STILL trapping focus.
                         The dialog focuses itself, the menu's trap yanks focus back,
                         and the menu then unmounts -- stranding focus on `body`. In
                         happy-dom the same fight shows up as an unbounded
                         blur/focus recursion, which is how the test suite surfaced
                         it here. Past the close commit there is only one trap. */
                      onSelect={() => { setTimeout(() => setSketchOpen(true), 0) }}
                      title={i18nT('components.chatInput.sketch')}
                      className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg cursor-pointer text-left"
                    >
                      <PenLine size={14} className="w-4 shrink-0 text-muted lucide-inline" />
                      <div className="min-w-0">
                        <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.sketch')}</div>
                        <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.sketch_desc')}</div>
                      </div>
                    </DropdownMenuItem>
                  )}
                  {collapseMenuRow && (
                    <DropdownMenuItem asChild>
                      {collapseMenuRow}
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {/* The wrapper exists for the edge cues: absolutely-positioned
                children of the scroller itself would travel with the scrolled
                content, so the fades anchor to this non-scrolling parent. It
                also owns the flex sizing so the scroller keeps filling the
                row. */}
            <div className="relative min-w-0 flex-1">
              <div ref={attachControlRow} data-testid="composer-control-row" className="flex items-center gap-0.5 overflow-x-auto">

              {onAutomationClick && (
                <Suspense fallback={null}>
                  <SessionAutomationPopover
                    slotKey={slotId || ''}
                    automation={automation || null}
                    open={automationOpen || false}
                    onOpenChange={v => onAutomationClick(v)}
                    onChange={onAutomationChange || (() => {})}
                    creationReady={automationCreationReady}
                    snapshotFailed={automationSnapshotFailed}
                    sessionMode={sessionMode}
                    // Same condition as the Resume placeholder (`resumeOffered`):
                    // whenever the composer says "press Resume", the loop chip
                    // must not pulse as if a cycle were executing.
                    interrupted={resumeOffered}
                  />
                </Suspense>
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
              <MicButton voiceHoldMode={voiceHoldMode} voiceRecording={voiceRecording} micIsModeSwitch={micIsModeSwitch} transcribeInFlight={transcribeInFlight} micHeldElsewhere={micHeldElsewhere} micBlocked={micBlocked} toggleVoiceMode={toggleVoiceMode} onVoiceToggle={onVoiceToggle} onVoicePrewarm={onVoicePrewarm} disabled={disabled} optimizing={optimizing} micLabel={micLabel} />
            )}
            {/* The busy branch is reachable with EITHER a stop affordance or a
                steer path: a host without onStop (the side panel — stopping the
                main turn from there would be misdirected) still needs the
                split steer/queue button while a turn runs. */}
            {compacting && !isRunning && !composerHasDraft && (!stopState || stopState === 'idle') ? (
              // An automatic compaction holds the session. It is NOT a turn
              // (`isRunning` is false), so without this branch the composer
              // read idle and the only affordance was Send. Yields to a LIVE
              // turn: a turn sharing the session with a compaction keeps its
              // armed Stop and steer controls, and the backend declines the
              // first press with a card while arming the second as the force
              // escape. Also yields to an in-flight stop and to a TYPED DRAFT:
              // the idle Send queues the message behind the compaction, so a
              // user with something to say is never left without a send.
              <CompactingIndicator />
            ) : (isRunning || stopState === 'soft_pending' || stopState === 'killing') && (onStop || (canSteer && onSteer)) ? (
              <BusySendControls stopState={stopState} killingEscaped={killingEscaped} stopWithTap={stopWithTap} isQueued={isQueued} composerHasDraft={composerHasDraft} canSteer={canSteer} onSteer={onSteer} steerOnly={steerOnly} fireComposer={fireComposer} disabled={disabled} connected={connected} effectiveBusyMode={effectiveBusyMode} setBusySendMode={setBusySendMode} sendOnEnter={sendOnEnter} jevAutoAvailable={jevAutoAvailable} onStop={onStop} stopDeclinedArmed={stopDeclinedArmed} />
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

      {!showGhost && composerCollapsed && (
        <CollapsedComposerBar collapsedBarRef={collapsedBarRef} expandComposer={expandComposer} collapsedDraftLine={collapsedDraftLine} />
      )}
      </Glass>

      {/* Context shelf — plain full-width row below input, standing on the
          `glass-shelf` fade (index.css): the dock floats over the transcript,
          so without it the agent / project / model chips read against whatever
          scrolls under them. The fade is positioned against ChatPage's
          `relative z-10 dock-inert` wrapper (the shelf is deliberately not
          positioned), so it spans the dock root — which stops short of the
          scrollbar gutter — and sits behind the pane's shadow;
          `--glass-shelf-h` tells it where the shelf starts.
          Stands down with the composer for the same reason it stands down for the
          ghost bar: agent, project, branch and model are context for WRITING, and
          the assembly is not being written in. Leaving it up was measured to cost
          most of the collapse — the assembly gave back 57px with the shelf still
          mounted against 89px without it, at a 1500x950 viewport — so keeping it
          would have shipped a "reclaim the space" control that reclaimed little. */}
      {!showGhost &&
        !composerCollapsed &&
        (onProjectClick ||
          (onModelClick && modelName) ||
          // An app-contributed chip is reason enough to draw the shelf. Without
          // this the chip is silently invisible whenever no other pill happens
          // to be present — the control is declared, mounted and unreachable.
          !!sessionControls?.length) && (
        <div ref={shelfRef} data-testid="composer-context-shelf" className="glass-shelf pt-1 flex items-center gap-2 min-w-0" style={{ ['--glass-shelf-h' as string]: `${shelfHeight}px` }}>
          {/* App-contributed session controls live in their OWN group, not
              beside the agent/project chips. `max-two-buttons-per-row`
              (AUTOSDE.yaml, blocking) caps a horizontal group at 2 action
              controls and forbids an already-exempt 3+ group from growing —
              and the chip group next door already carries 5 on main. Its own
              separated region is the rule's stated exemption ("the cap is
              per visual group, not per component"), and keeps the per-app
              status tint that one collapsed kebab would hide. Bounded at 2
              by MAX_INLINE_SESSION_CONTROLS so this group sits AT the cap. */}
          {!!sessionControls?.length && (
            <SessionControlChips sessionControls={sessionControls} shelfCompact={shelfCompact} onSessionControlClick={onSessionControlClick} />
          )}
          <div className="flex items-center gap-2 min-w-0 flex-1">
          {onAgentClick && agentName && (
            <AgentChip agentName={agentName} agentLabel={agentLabel} agentIsInheritedDefault={agentIsInheritedDefault} agentSource={agentSource} isRunning={isRunning} shelfCompact={shelfCompact} onAgentClick={onAgentClick} />
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
            onClick={e => onProjectClick(e.currentTarget.getBoundingClientRect(), e.currentTarget)}
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
            <ContextUsageControl contextPct={contextPct} contextUsedTokens={contextUsedTokens} contextWindowTokens={contextWindowTokens} showContextPct={showContextPct} showContextTokens={showContextTokens} shelfCompact={shelfCompact} modelName={modelName} ctxPopoverOpen={ctxPopoverOpen} setCtxPopoverOpen={setCtxPopoverOpen} ctxWrapRef={ctxWrapRef} autoCompactThreshold={autoCompactThreshold} />
          )}
          {onModelClick && modelName && (
            <ModelChip modelName={modelName} modelIsJevRouted={modelIsJevRouted} modelIsInheritedDefault={modelIsInheritedDefault} modelIsAutoChosen={modelIsAutoChosen} reasoningEffort={reasoningEffort} effortIsDefault={effortIsDefault} hasEffort={hasEffort} isRunning={isRunning} shelfCompact={shelfCompact} shelfTiny={shelfTiny} composerControl={composerControl} modelChipPressedFromComposerRef={modelChipPressedFromComposerRef} onModelClick={onModelClick} />
          )}
          </div>
        </div>
      )}
    </div>
  )
}

export default memo(ChatInput)
