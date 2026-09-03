import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { shallowEqual } from 'react-redux'
import { useAppSelector, useAppDispatch } from '../../store'
import { clearFocusToolCallId, mcpAppKey } from '../../store/chatSlice'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useLanguage } from '../../i18n/LanguageProvider'
import { DERIVE_LABEL_THRESHOLD_CHARS, deriveShellSummary, pickToolLabel } from '../../utils/toolLabel'
import { LoaderCircle, CircleSlash, CircleAlert, CircleDot, Lock, PanelRight } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import type { ChatMessage } from '../../types'
import { ToolDetails } from './ToolDetails'
import { extractDenyDetail } from '../../utils/denyReason'
import { registerToolPill } from '../../store/toolPillRegistry'
import { ROW_PILL_BUTTON_CLASS, ROW_PILL_WRAPPER_CLASS } from './rowPill'
import { extractToolFilePath } from '../../utils/toolFilePath'
import { countDiffStats } from '../../utils/diffLineCounts'
import { isWaitToolTitle } from '../../utils/waitToolTitle'
import { isSafePath } from '../../utils/safePath'
import { fileReadUrl } from '../../utils/fileReadUrl'
import McpAppFrame from '../../components/McpAppFrame'
import DiffBlock, { extractFilePath as extractDiffHeaderPath } from '../../components/DiffBlock'
import { presentToolDiff } from './toolDiff'
import { FileDiff } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { fmtDateFields, fmtDuration as fmtDurationParts, fmtUnit } from '../../i18n/format'
import { api } from '../../api/client'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { isRejectedDecision } from '../../utils/approvalDecision'
import { selectToolRowIndex, lookupLogEntry, denySiblingContent } from './toolRowIndex'
import type { ToolActivity } from '../../types'

// Stable empties for slots with no per-slot state yet. A fresh `[]` per
// selector run would change identity every dispatch and defeat the
// per-(messages, toolLog) memoization of selectToolRowIndex.
const EMPTY_MESSAGES: ChatMessage[] = []
const EMPTY_TOOL_LOG: ToolActivity[] = []

// Tool-call ids that have already played their one-shot `.ft-block-reveal`
// entrance fade. A CSS animation re-fires on every DOM *mount*, and a pill
// remounts in several normal situations that are NOT a genuine first
// appearance — singles→turn promotion (ChatPage flushTurn), the flat→segmented
// restructure at turn completion (TurnBlock), expand/collapse via
// AnimatePresence, and virtualizer window recycling on scroll. Without this
// guard every one of those remounts replays the fade, so all previously-shown
// tool results visibly flash whenever a new tool runs / the turn advances.
// Keyed by the stable tool_call_id and held at module scope so it outlives any
// unmount, this lets a pill fade in exactly once — the first time it appears.
// (Same philosophy as the `.ft-word` streaming reveal: already-revealed nodes
// don't re-fire.)
const revealedToolIds = new Set<string>()

// Diff cards the reader OPENED, by tool_call_id — survives virtualizer
// unmounts for the page lifetime so an opened card does not snap shut on
// scroll. Cards start folded, so the remembered state is the expansion (the
// same inversion `FoldableDiffBlock.expandedDiffFences` makes for prose
// fences); session-scoped by intent, a reload starts folded again.
const openedDiffCards = new Set<string>()

/** Exported for tests: forget every remembered card expansion. */
export function resetOpenedDiffCards(): void {
  openedDiffCards.clear()
}

// ── Row slide (height easing) ──
// The transcript is pinned to the bottom, and the virtualizer's pin is
// deliberately INSTANT (a smooth pin chases the moving bottom while output
// streams, so it never converges). Every height change therefore lands in a
// single frame: a tool row mounting at its full height, or its status line
// unmounting, teleports everything above it by that many pixels.
//
// Easing the ROW's height instead of the scroll leaves the pin instant and
// spreads the same movement over ~13 frames, which reads as the content sliding.
// It rides the path streaming growth already uses — the ResizeObserver reports
// each frame's height and re-pins — so no new scroll machinery is involved.
//
// Kept short and cheap: this fires on every tool call, and each animated frame
// costs one scrollTop write.
const SLIDE_DURATION = 0.22
const SLIDE_EASE = [0.4, 0, 0.2, 1] as const

// ── Collapsed label clamp ──
// A tool title is whatever the transport hands us, and for a shell call that is
// the WHOLE command — an inline heredoc or a chained one-liner is routinely
// several kB. Rendered with `break-words` that wrapped to forty-odd lines, so a
// single folded tool row could be taller than the answer it belongs to, and the
// reader had to scroll a wall of quoting to reach the next message.
//
// A collapsed row is a STATUS line, exactly like ThinkingBlock's folded
// "Thought process": it says which tool is running and that it is running, and
// nothing more. So the collapsed label is pinned to one line with an ellipsis
// and the full text lives one click away in ToolDetails, which already renders
// the verbatim input. Expanding restores wrapping — the user asked to see it.
//
// `truncate` (not `line-clamp-1`) because the label is a plain flex child:
// line-clamp would force `display: -webkit-box` on it and drop the flex
// alignment the icon's centering depends on.
const LABEL_COLLAPSED_CLASS = 'truncate'
const LABEL_EXPANDED_CLASS = 'break-words'

/**
 * A tool row's secondary status line (shell elapsed time, `wait` countdown).
 *
 * Appears and disappears by easing its own height rather than mounting at full
 * height, so the transcript above slides instead of stepping. The clip is only
 * needed while the height is mid-animation, but it is harmless at rest: the box
 * settles at its content height, so nothing is ever cut off.
 *
 * `children` are constructed by the caller even while hidden (ordinary JSX
 * evaluation) — they must therefore stay cheap and side-effect free, which the
 * two status lines are.
 *
 * `snap` disables the ease while the transcript is streaming at high
 * frequency (see the `transcriptHot` prop): each animated frame costs a
 * scrollTop re-pin, and during a hot stream those stack across rows. The ease
 * stays for quiet-transcript transitions.
 */
function StatusRow({ show, snap = false, children }: { show: boolean; snap?: boolean; children: ReactNode }) {
  const reduce = useReducedMotion()
  return (
    <AnimatePresence initial={false}>
      {show && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={reduce || snap
            ? { duration: 0 }
            : { height: { duration: SLIDE_DURATION, ease: SLIDE_EASE }, opacity: { duration: 0.15 } }}
          style={{ overflow: 'hidden' }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

/** Inline tool call pill. Click toggles an expanded panel below the pill that
 *  shows purpose / input / output. */
export default memo(function ToolCallLine({ message, running: _running, slot, onFileOpen, disclosure, disclosureKey, onDisclosureChange, appInPanel, onOpenApp, transcriptHot = false }: { message: ChatMessage; running: boolean; slot?: string; onFileOpen?: (path: string) => void; disclosure?: boolean; disclosureKey?: string; onDisclosureChange?: (key: string, expanded: boolean) => void; appInPanel?: boolean; onOpenApp?: (toolCallId: string) => void;
  /** True while the transcript is streaming at high frequency (host-derived
   *  from useStreamIdle). Gates the entrance/status height eases: a hot-stream
   *  mount lands at full height in one frame, a quiet-transcript mount keeps
   *  the ease. Defaults to false so hosts without a heat signal keep the
   *  animations unconditionally. */
  transcriptHot?: boolean }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const dispatch = useAppDispatch()
  const label = message.content.replace(/^🔧\s*/, '')
  const toolCallId = message.meta?.tool_call_id as string | undefined
  const simplified = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved

  // MCP App (SEP-1865) render payload attached to this tool call, if any.
  // Rendered as an inline sandboxed iframe below the tool-call row. Selected
  // by (slot, tool_call_id) — this row's own slot — so another session's app
  // (and its callback capability) can never mount here.
  const mcpApp = useAppSelector(s => {
    const sk = slot ?? s.chat.activeSlot
    return toolCallId && sk ? s.chat.mcpApps?.[mcpAppKey(sk, toolCallId)] : undefined
  })

  // Pull the matching toolLog entry. Returns purpose/input/output for the inline
  // expansion as well as completion status for the icon. All transcript scans go
  // through the shared per-slot index (see toolRowIndex.ts): built once per
  // (messages, toolLog) identity change, O(1) per row per dispatch.
  const { effectiveId, isDone: logIsDone, isRejected, isAutoDenied, autoDenyReason, purpose, input, output, auto, ts, executionStartedAt, hasEntry, isShell, toolKind, toolName, fromLog } = useAppSelector(s => {
    // Slot-aware: for a non-active slot (split-view pane) read that slot's
    // per-slot tool log / messages / running state; `slot` undefined or equal to
    // the active slot → active-slot globals.
    const bg = slot && slot !== s.chat.activeSlot ? slot : null
    const log = bg ? (s.chat.slotActivity[bg]?.toolLog ?? EMPTY_TOOL_LOG) : s.chat.toolLog
    const slotRunning = bg ? ((s.chat.slotRun[bg]?.state ?? 'idle') !== 'idle') : s.chat.slotRunning
    const msgs = bg ? (s.chat.slotMessages[bg] ?? EMPTY_MESSAGES) : s.chat.messages
    const index = selectToolRowIndex(msgs, log)

    // Was this tool's permission resolved as rejected? Only the NEWEST
    // permission message for the id carries the decision.
    const wasRejectedByPerm = () => {
      if (!toolCallId) return false
      const perm = index.lastPermById.get(toolCallId)
      return perm ? isRejectedDecision(perm.meta?.resolved) : false
    }

    // Auto-denied detection. When a security-policy deny rule or hook blocks a
    // tool, the gateway appends a SECOND tool message — "🚫 <title> …" —
    // sharing this pill's tool_call_id. That message never renders (TurnBlock
    // hides every tool message not starting with 🔧), so the visible 🔧 pill
    // must find its hidden sibling to know the call was blocked. Its CONTENT is
    // used too: the row carries "— Blocked by security policy: <reason>", and the
    // Output panel leads with its own LOCALIZED sentence and follows with the
    // rule, so the user learns which rule fired without a translated sentence
    // being replaced by untranslated English (extractDenyDetail yields "" for a
    // row without that marker, and the panel then shows the localized line
    // alone). The interactive user-reject path also appends a 🚫 message, but
    // that flow ALSO resolves a permission message as rejected —
    // wasRejectedByPerm() takes precedence below, so a user rejection still
    // shows red, not amber.
    const denySibling = denySiblingContent(index, toolCallId, message)
    const autoDenied = !!denySibling
    const autoDenyReason = extractDenyDetail(denySibling)

    const e = lookupLogEntry(index, toolCallId, label)
    if (e) {
      const rejected = !!e.rejected || wasRejectedByPerm()
      const isDone = e.output != null || rejected || autoDenied || !slotRunning
      return {
        effectiveId: e.tool_call_id || null,
        isDone, isRejected: rejected,
        isAutoDenied: !rejected && autoDenied,
        autoDenyReason,
        purpose: e.purpose || '',
        input: e.input || '',
        output: e.output || '',
        auto: !!e.auto,
        ts: e.ts || 0,
        executionStartedAt: e.execution_started_at || 0,
        hasEntry: true,
        // Older ACP update frames may omit is_shell; execute is the stable
        // tool-kind value used by the transport and keeps those frames live.
        isShell: e.is_shell === true || e.kind === 'execute' || e.text.startsWith('Running:'),
        // Raw ACP tool kind — gates the inline diff-card promotion below
        // (only kind === 'edit' rows promote).
        toolKind: e.kind || '',
        // Raw transport tool name, kept separate from the display `label`:
        // the label is simplified/localized for humans, so gating behaviour on
        // it would break under `useSimplifiedToolNames` or a translated UI.
        toolName: e.text || '',
        fromLog: true,
      }
    }
    // No toolLog entry — historical message. Check permission state for rejection.
    const rejected = wasRejectedByPerm()
    // Backend persists `input` (when the call was issued) and `output` (when
    // the result arrived) directly on the tool message's meta — see
    // _tool_meta() and the EVENT_TOOL_RESULT handler in chat_runner.py.
    // Pre-persistence messages won't have these fields and fall through to
    // the empty-state hint inside ToolDetails.
    const metaInput = (message.meta?.input as string | undefined) || ''
    const metaOutput = (message.meta?.output as string | undefined) || ''
    return {
      effectiveId: toolCallId || null,
      isDone: true, isRejected: rejected,
      isAutoDenied: !rejected && autoDenied,
      autoDenyReason,
      purpose: (message.meta?.purpose as string) || '',
      input: metaInput, output: metaOutput, auto: false,
      // ChatMessage.ts is a string (ISO timestamp) when restored from history;
      // parse it for the meta-row time renderer. Falls to 0 if unparseable —
      // fmtTime hides the row when ts is 0.
      ts: typeof message.ts === 'number' ? message.ts : (message.ts ? Date.parse(String(message.ts)) || 0 : 0),
      executionStartedAt: 0,
      // Treat the message as having an entry when persisted I/O is available,
      // so the empty-state copy only shows for truly bare historical messages.
      hasEntry: !!(metaInput || metaOutput),
      isShell: false,
      // Persisted ACP tool kind (see _tool_meta in chat_runner.py). Rows
      // written before the field existed read '' and never promote a card.
      toolKind: (message.meta?.kind as string | undefined) || '',
      // Historical rows have no log entry; the message content is the only
      // carrier. Harmless either way — a replayed wait is never in flight.
      toolName: message.content.replace(/^🔧\s*/, ''),
      // No live tool-log entry backs this row. `isDone` above is a REPLAY
      // default, not an observation, so anything that needs real liveness must
      // consult server state instead of trusting it (see the wait countdown).
      fromLog: false,
    }
  }, shallowEqual)

  // The tool log is authoritative here: its output/rejection/turn-running
  // state survives the ACP initial-call → update handoff, while the parent
  // `running` prop is not present on every historical/live rendering path.
  const isDone = logIsDone
  const turnRunning = !isDone

  // Check if this specific tool has a pending (unresolved) permission matching its tool_call_id.
  // Only match when tool_call_id is present on both sides — prevents batched approvals from
  // incorrectly lighting up all pills as pending approval.
  const hasPendingPerm = useAppSelector(s => {
    if (isDone || !toolCallId) return false
    const bg = slot && slot !== s.chat.activeSlot ? slot : null
    const msgs = bg ? (s.chat.slotMessages[bg] ?? EMPTY_MESSAGES) : s.chat.messages
    const log = bg ? (s.chat.slotActivity[bg]?.toolLog ?? EMPTY_TOOL_LOG) : s.chat.toolLog
    return selectToolRowIndex(msgs, log).pendingPermIds.has(toolCallId)
  })

  // Shell commands do not expose a reliable total, so their live indicator is
  // deliberately indeterminate. The existing tool output remains the source
  // of truth; this status only makes an in-flight command visible while its
  // details panel is collapsed after approval.
  //
  // STICKY within the turn: the row APPEARS when the tool starts but does
  // not collapse when it finishes -- it freezes into the elapsed total and
  // is reclaimed with the whole turn's collapse. Collapsing per-completion
  // pulsed ~26px above a bottom-pinned reader at every tool boundary of a
  // working turn (the tool-rhythm 'bounces in place while I just watch'
  // report: text streaming was stable, tool execution bounced), because
  // the closing height ease moves the pinned viewport down and back up
  // once per tool. Within-turn transcript height is now monotonic here.
  const shellActivityShownRef = useRef(false)
  const liveShellActivity = isShell && turnRunning && !hasPendingPerm
  if (liveShellActivity) shellActivityShownRef.current = true
  const showShellActivity = liveShellActivity || (isShell && shellActivityShownRef.current)

  // ── `wait` countdown ──
  // Matched to this pill by tool NAME, not by id: the wait_id is minted inside
  // the MCP subprocess and never appears on the ACP tool_call frame, so there is
  // nothing to join on. The title has to go through isWaitToolTitle rather than
  // `=== 'wait'` because the transport decides its shape (`wait`,
  // `kirocrew-core___wait`, `wait (mcp)`) — see that helper.
  //
  // `slot.wait_state` is the ONLY liveness signal consulted, and deliberately so.
  // The backend mints it on the sleep's first keepalive ping and clears it on the
  // final one (and again at turn end as a backstop), so its presence means a wait
  // is genuinely sleeping right now. The row's own `isDone` cannot stand in for
  // that: after a mid-wait reload the runtime tool log is empty, the historical
  // branch defaults `isDone` to true, and gating on it would blank the countdown
  // in exactly the case the server-side deadline exists to serve. `isDone` is
  // still honoured while a real log entry backs the row, so a wait that finishes
  // in a live session stops ticking on the tool_result rather than waiting for
  // the slots push.
  //
  // The deadline likewise comes from the slots payload, not the tool input: it is
  // minted on the backend clock (the log entry's `ts` is browser-side and is
  // reset by ACP's second tool_call frame) and re-seeds from GET /api/chat/slots.
  //
  // The `running` and newest-tool-row guards below are containment for a session
  // identity that resolves per RUNTIME rather than per ACP session; the cure is
  // tracked in https://github.com/kirodotdev/KiroCrew/issues/2347.
  const isWaitTool = isWaitToolTitle(toolName)
  const waitSlotKey = useAppSelector(s => slot ?? s.chat.activeSlot)
  const waitState = useAppSelector(s => {
    if (!isWaitTool || hasPendingPerm) return null
    if (fromLog && isDone) return null
    const found = waitSlotKey ? s.dashboard.slots.find(x => x.key === waitSlotKey) : undefined
    const ws = found?.wait_state
    if (!ws) return null
    // The turn must actually be running. Cheap on its own, but load-bearing for
    // a replayed row: the historical branch cannot observe liveness, so without
    // this an idle slot's old wait pill would count down against a deadline
    // belonging to some other sleep the backend is tracking.
    if (!found?.running) return null
    const bg = slot && slot !== s.chat.activeSlot ? slot : null
    const msgs = bg ? (s.chat.slotMessages[bg] ?? EMPTY_MESSAGES) : s.chat.messages
    const log = bg ? (s.chat.slotActivity[bg]?.toolLog ?? EMPTY_TOOL_LOG) : s.chat.toolLog
    // Exactly one pill owns the countdown, and it must be the transcript's
    // newest TOOL call of any kind -- not merely its newest wait. Scanning only
    // for waits would let a completed wait from earlier in the turn light up
    // while the agent is busy in some later tool, which is the shape a sleep
    // belonging to a different ACP session on this same slot would take.
    const last = selectToolRowIndex(msgs, log).lastToolMsg
    if (!last || last.meta?.tool_call_id !== toolCallId) return null
    return isWaitToolTitle(last.content.replace(/^🔧\s*/, '')) ? ws : null
  }, shallowEqual)
  const showWaitCountdown = !!waitState && waitState.deadline_ts > 0
  const [activityNow, setActivityNow] = useState(() => Date.now())
  // Seed from execution_started_at (persisted in Redux, survives remount) when
  // available — it records when execution actually began after approval. Falls
  // back to ts (tool issuance time) then Date.now() for brand-new calls.
  const activityStartRef = useRef(executionStartedAt || ts || Date.now())
  const wasPendingRef = useRef(hasPendingPerm)
  // Tracks whether approval resolved during this mount — once set, the ts
  // re-anchor effect is suppressed so the post-approval Date.now() anchor
  // is not overwritten by the pre-approval ts.
  const approvalResolvedRef = useRef(false)
  const [endingWait, setEndingWait] = useState(false)

  // Re-arm the button for a NEW sleep. Left latched otherwise: after a
  // successful request the row lives on for up to one poll interval, and a
  // re-enabled button there would invite a second click the backend answers 409.
  const endedWaitIdRef = useRef<string | null>(null)
  useEffect(() => {
    const id = waitState?.wait_id ?? null
    if (id !== endedWaitIdRef.current) {
      endedWaitIdRef.current = id
      setEndingWait(false)
    }
  }, [waitState?.wait_id])

  const endWaitNow = useCallback(() => {
    if (!waitSlotKey || !waitState || endingWait) return
    setEndingWait(true)
    void api.endWait(waitSlotKey, waitState.wait_id).catch(() => {
      // Roll back so a transport failure is retryable. A 409 also lands here,
      // and re-enabling is still right: the countdown it referred to is gone.
      setEndingWait(false)
    })
  }, [waitSlotKey, waitState, endingWait])

  useEffect(() => {
    if (hasPendingPerm) {
      wasPendingRef.current = true
      return
    }
    if (wasPendingRef.current) {
      const now = Date.now()
      activityStartRef.current = now
      wasPendingRef.current = false
      approvalResolvedRef.current = true
      setActivityNow(now)
      // The durable anchor is stamped server-side: the approval_resolved
      // frame sets execution_started_at on the tool entry in Redux (see
      // sseActivityEvent), which the re-anchor effect below reads on remount.
    }
  }, [hasPendingPerm])

  // When the tool log timestamp arrives (or on remount with a persisted ts),
  // re-anchor the elapsed clock so it reflects real wall time since the tool
  // started — not time since the component mounted. Skip if approval just
  // resolved in this mount — the post-approval Date.now() is the correct anchor
  // for approved commands (the pre-approval ts would inflate the timer by the
  // entire approval wait). Also skip if executionStartedAt is set — it persists
  // in Redux and is the authoritative anchor that survives remount.
  useEffect(() => {
    if (executionStartedAt) {
      activityStartRef.current = executionStartedAt
    } else if (ts && !hasPendingPerm && !approvalResolvedRef.current) {
      activityStartRef.current = ts
    }
  }, [ts, hasPendingPerm, executionStartedAt])

  useEffect(() => {
    if (!liveShellActivity && !showWaitCountdown) return
    const timer = window.setInterval(() => setActivityNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [liveShellActivity, showWaitCountdown])

  const elapsedSeconds = Math.max(0, Math.floor((activityNow - activityStartRef.current) / 1000))
  const elapsedLabel = fmtDurationParts(
    [[Math.floor(elapsedSeconds / 60), 'minute'], [elapsedSeconds % 60, 'second']],
    { dropZero: true },
  )

  // Remaining time on the sleeping wait. Ceil so the label reads "1s" for the
  // final fractional second instead of flashing "0s" while the tool is still
  // asleep, and clamp at zero: the sleep can outlast its own deadline by up to
  // one poll interval, and a negative countdown is worse than a parked "0s".
  const remainingSeconds = waitState
    ? Math.max(0, Math.ceil((waitState.deadline_ts * 1000 - activityNow) / 1000))
    : 0
  // Minutes are dropped below 60s but the seconds place is NEVER dropped above
  // it, so the label steps 2m 1s → 2m 0s → 59s instead of collapsing to a bare
  // "2m" for one tick. Same reasoning as fmtTurnElapsed's "2m 0s" comment.
  const remainingLabel = remainingSeconds < 60
    ? fmtUnit(remainingSeconds, 'second', { maximumFractionDigits: 0 })
    : fmtDurationParts([
      [Math.floor(remainingSeconds / 60), 'minute'],
      [remainingSeconds % 60, 'second'],
    ])

  // Inline expansion state.
  //
  // Default `expanded` mirrors `hasPendingPerm` so a tool that lands awaiting
  // approval (or one that's still pending after a page reload) opens with its
  // details visible — the inline panel is the only place the user can read
  // what the agent is about to run.
  //
  // `pendingAutoExpand` tracks whether the current expanded state was *driven*
  // by the pending-approval transition. We clear it on any user interaction
  // (manual toggle / focus signal) so the panel stays open if the user took
  // explicit control, and only auto-collapse when the approval resolves
  // *and* we were the ones who opened it.
  const [localExpanded, setLocalExpanded] = useState(() => hasPendingPerm)
  // Disclosure is HOST-OWNED when `disclosure` is supplied. The transcript is
  // virtualised, so this pill is unmounted whenever its row leaves the mounted
  // window, and state kept only here dies with it. `undefined` means the host
  // holds no choice for this pill, so the local value applies.
  const expanded = disclosure ?? localExpanded
  // Both the notifier and the key are passed in already-stable, so memo() on
  // this component still short-circuits.
  const onDisclosureChangeRef = useRef(onDisclosureChange)
  onDisclosureChangeRef.current = onDisclosureChange
  const disclosureKeyRef = useRef(disclosureKey)
  disclosureKeyRef.current = disclosureKey
  // Write both channels: the local value keeps an uncontrolled host (split-view
  // ChatPane, app-sdk) working, and the notification is what survives a remount.
  const applyExpanded = useCallback((next: boolean) => {
    setLocalExpanded(next)
    const notify = onDisclosureChangeRef.current
    const key = disclosureKeyRef.current
    if (notify && key) notify(key, next)
  }, [])
  const [pendingAutoExpand, setPendingAutoExpand] = useState(() => hasPendingPerm)
  const prevPendingRef = useRef(hasPendingPerm)
  useEffect(() => {
    const wasPending = prevPendingRef.current
    prevPendingRef.current = hasPendingPerm
    if (hasPendingPerm && !wasPending) {
      // Approval just became pending → auto-expand
      applyExpanded(true)
      setPendingAutoExpand(true)
    } else if (!hasPendingPerm && wasPending && pendingAutoExpand) {
      // Approval just resolved (approved/rejected/cancelled) and the user
      // didn't take over → auto-collapse. Defer to the next animation frame
      // so any concurrent state changes (inner Input/Output section auto-
      // promote when output arrives, output content rendering, etc.) commit
      // and settle layout first. Without this, AnimatePresence captures a
      // mid-flux height for the exit animation and the panel snaps shut
      // instead of animating cleanly.
      const raf = requestAnimationFrame(() => {
        applyExpanded(false)
        setPendingAutoExpand(false)
      })
      return () => cancelAnimationFrame(raf)
    }
  }, [hasPendingPerm, pendingAutoExpand, applyExpanded])

  const containerRef = useRef<HTMLDivElement>(null)

  // External focus signal (e.g. from ChatInput's "Jump to tool" link). When the
  // redux focusToolCallId matches this pill, auto-expand and scroll into view,
  // then clear the focus so subsequent re-renders don't keep firing. Treat the
  // jump as user intent — clear pendingAutoExpand so the panel stays open
  // through approval resolution.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (focusToolCallId && effectiveId && focusToolCallId === effectiveId) {
      applyExpanded(true)
      setPendingAutoExpand(false)
      requestAnimationFrame(() => containerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
      dispatch(clearFocusToolCallId())
    }
  }, [focusToolCallId, effectiveId, dispatch, applyExpanded])

  // File-op tool pills (read/edit/write) get a side-panel open affordance.
  // Extract the fs path from the tool's JSON args; the chip is gated on a
  // safe path AND an onFileOpen handler AND a successful HEAD probe (the file
  // still exists on disk).
  const filePath = useMemo(() => extractToolFilePath(input), [input])
  // Inline diff presentation: an edit tool's input IS a unified diff
  // (backend-derived from the ACP diff content block, see acp/_dispatch.py).
  // Small diffs promote to a DiffBlock card below the pill, folded to its chip
  // until the reader opens it; over-cap diffs degrade to a summary chip
  // (filename, −N +M) that expands the details panel — never to nothing,
  // because under the relaxed prompt
  // the model no longer restates tool edits as ```diff blocks. Null for every
  // non-edit tool and for rows predating meta.kind — those keep the
  // collapsed-details rendering.
  //
  // A REJECTED or auto-denied call never promotes: the edit was not applied,
  // and a first-class diff card visually dominates the pill's small red/amber
  // status icon — a reader scanning history would believe the file changed.
  // The full diff stays readable in the expanded details panel.
  //
  // The card renders REGARDLESS of the collapse-all-steps preference: a file
  // change is a result, not a working step — the same class as the prose
  // ```diff the final summary used to carry, which that preference never
  // folded either. Density relief is per-card (the fold chip below) plus the
  // size caps in presentToolDiff.
  const denied = isRejected || isAutoDenied
  const diffView = useMemo(
    () => (denied ? null : presentToolDiff(toolKind, input)),
    [denied, toolKind, input],
  )
  // Per-card density control, FOLDED by default: a turn that edits several
  // files stacks a full patch per file, so the answer the reader came for
  // scrolls off. The chip still states the three facts that decide whether to
  // look (file, +N, −M) and one click opens the patch. Expansions are
  // remembered at module scope by tool_call_id (same lifetime pattern as
  // revealedToolIds above) so a virtualizer unmount does not silently re-fold
  // a card the reader opened.
  const [cardFolded, setCardFolded] = useState(
    () => !(toolCallId && openedDiffCards.has(toolCallId)),
  )
  const diffTogglePendingFocus = useRef(false)
  const toggleCardFolded = useCallback(() => {
    setCardFolded(prev => {
      const next = !prev
      if (toolCallId) {
        if (next) openedDiffCards.delete(toolCallId)
        else openedDiffCards.add(toolCallId)
      }
      return next
    })
    // The two halves of the toggle unmount each other, so the activated
    // control disappears and focus would fall to <body>. Hand focus to the
    // counterpart once it mounts — both carry data-diff-toggle.
    diffTogglePendingFocus.current = true
  }, [toolCallId])
  useEffect(() => {
    if (!diffTogglePendingFocus.current) return
    diffTogglePendingFocus.current = false
    const el = containerRef.current?.querySelector<HTMLElement>('[data-diff-toggle]')
    el?.focus()
  }, [cardFolded])
  const cardStats = useMemo(
    () => (diffView?.mode === 'card' ? countDiffStats(diffView.code) : null),
    [diffView],
  )
  const showCard = diffView?.mode === 'card' && !cardFolded
  // The chip is the one-line handle for COMPACT states only: the folded
  // card's re-open handle, and the summary / pathname rows. An OPEN card
  // shows no chip — DiffBlock's own header row already carries the file
  // icon, basename and ±counts, and its fold control lives there (onFold),
  // so the facts never render twice.
  const chipView: { path: string | null; added: number; removed: number; truncated: boolean; opensCard: boolean } | null =
    diffView?.mode === 'card'
      ? (cardFolded
        ? { path: extractDiffHeaderPath(diffView.code)?.path ?? filePath, added: cardStats?.added ?? 0, removed: cardStats?.removed ?? 0, truncated: false, opensCard: true }
        : null)
      : diffView?.mode === 'summary'
        ? { ...diffView, opensCard: false }
        : null
  const probeEnabled = !!filePath && isSafePath(filePath) && !!onFileOpen
  // HEAD-probe via React Query (project guideline: no manual useState/useEffect
  // fetch for server state). Gives request dedup across pills touching the same
  // file and stale-while-revalidate caching so re-renders don't re-probe —
  // replacing the manual AbortController + onFileOpenRef + setFileExists dance.
  const { data: fileExists = false } = useQuery({
    queryKey: ['tool-pill-file-exists', filePath],
    queryFn: async ({ signal }) => {
      const r = await fetch(fileReadUrl(filePath!), { method: 'HEAD', signal })
      return r.ok
    },
    enabled: probeEnabled,
    staleTime: 30_000,
  })
  const showFileOpen = probeEnabled && fileExists

  const Icon = isDone
    ? (isRejected ? CircleSlash : isAutoDenied ? CircleAlert : CircleDot)
    : hasPendingPerm ? Lock
    : LoaderCircle
  const iconClass = isDone
    // Auto-denied: amber (warn). User-rejected: red (danger). Completed: green (ok).
    ? (isRejected ? 'text-danger' : isAutoDenied ? 'text-warn' : 'text-ok')
    : hasPendingPerm ? 'text-warn'
    : 'text-accent animate-spin'
  // Match the panel's left rail to the pill's status — keeps the visual chain
  // (icon → bar → content) coherent across auto-denied (amber), user-rejected
  // (red), done (green), pending-approval (yellow), and running (accent) states.
  // Inline style with color-mix() rather than Tailwind opacity classes — the
  // project's Tailwind config doesn't compile `border-{color}/N` opacity
  // variants for theme colors.
  const barColor = isDone
    ? (isRejected ? 'var(--danger)' : isAutoDenied ? 'var(--warn)' : 'var(--ok)')
    : hasPendingPerm ? 'var(--warn)' : 'var(--accent)'
  const barStyle = `color-mix(in srgb, ${barColor} 70%, transparent)`
  // A blocked call's Output leads with the LOCALIZED sentence and follows with
  // the rule that fired. Leading with the rule would hand every non-English
  // reader untranslated English (and a bare regex is not an explanation in any
  // language), while showing only the localized line is what this change set out
  // to fix: it names no rule, so the reader cannot tell WHICH policy fired.
  // Detail is absent for a hook-blocked row, which carries no marker; the
  // localized line then stands alone, exactly as it did before.
  const denyOutput = [i18nT('pages.chat.toolCallLine.blocked_by_security_policy'), autoDenyReason]
    .filter(Boolean)
    .join('\n')

  // Purpose is the agent's prose label (simplified mode). Guard it against the
  // active UI language so a purpose written in another language (e.g. a Chinese
  // label persisted before the user switched to English) falls back to the
  // language-neutral raw tool label instead of showing foreign-script text.
  const toolLabel = pickToolLabel({
    simplified,
    purpose: purpose || (message.meta?.purpose as string | undefined),
    rawLabel: label,
    uiLang,
  })

  // Design C: surface the file basename as a chip that hugs the open-in-pane
  // icon, so the affordance names the file it opens — crucial in simplified /
  // purpose mode where the label is prose and no path is otherwise shown. The
  // full path stays in the button tooltip and the expanded details.
  const basename = useMemo(() => (filePath ? (filePath.split('/').pop() || filePath) : null), [filePath])
  // When the chip is shown, strip the now-redundant raw path out of the visible
  // label. Raw mode `Read /a/b/c.ts` → `Read`; purpose-mode prose contains no
  // path substring → unchanged. Falls back to the full label if stripping
  // would leave it empty (label was nothing but the path).
  const displayLabel = useMemo(() => {
    if (!showFileOpen || !filePath) return toolLabel
    const stripped = toolLabel.split(filePath).join('').replace(/\s+/g, ' ').trim()
    return stripped || toolLabel
  }, [showFileOpen, filePath, toolLabel])
  // A purpose-less shell call's label is the raw command. The collapsed row's
  // `truncate` (LABEL_COLLAPSED_CLASS) already bounds how much of it is
  // visible, but a clipped wall of quoting says nothing — in simplified mode a
  // flood-length shell label is substituted with a derived command digest
  // (binaries + redirect target), so the visible line is meaningful. Short
  // labels pass through untouched, and raw mode always shows the exact command.
  const pillLabelText = useMemo(() => {
    if (!simplified) return displayLabel
    if (displayLabel.length <= DERIVE_LABEL_THRESHOLD_CHARS && !displayLabel.includes('\n')) {
      return displayLabel
    }
    return deriveShellSummary(displayLabel, { bareCommand: isShell }) ?? displayLabel
  }, [displayLabel, simplified, isShell])
  // Hover reveals the verbatim command whenever the pill shows a substitute.
  const pillLabelTitle = pillLabelText === displayLabel ? undefined : displayLabel
  // Both running and pending-approval pills shimmer — the highlight color
  // tracks the status so pending shimmers warn-yellow (matching the approval
  // bar) and running shimmers accent.
  const isShimmering = !isDone
  const shimmerHighlight = hasPendingPerm ? 'var(--warn)' : 'var(--accent)'
  const shimmerBase = 'var(--muted)'

  // Click-to-toggle handler — kept stable so memo() short-circuits work.
  // User click is explicit intent — clear pendingAutoExpand so the panel
  // doesn't auto-collapse out from under them when the approval resolves.
  // Pending-approval pills are locked open: clicking them is a no-op so the
  // user can't hide the input they're being asked to approve.
  const onToggle = useCallback(() => {
    if (hasPendingPerm) return
    applyExpanded(!expanded)
    setPendingAutoExpand(false)
  }, [hasPendingPerm, expanded, applyExpanded])

  const fmtTime = (t: number) => t ? fmtDateFields(t, { hour: '2-digit', minute: '2-digit' }) : ''

  // Pending pills are always expanded — `expanded` state still tracks the
  // user's intent for after the approval resolves, but the rendered panel
  // ignores it while pending.
  const effectivelyExpanded = expanded || hasPendingPerm

  // One line while folded, full wrap once opened. See LABEL_COLLAPSED_CLASS.
  const labelWrapClass = effectivelyExpanded ? LABEL_EXPANDED_CLASS : LABEL_COLLAPSED_CLASS

  // Stable per-instance fallback id for framer-motion's `LayoutGroup`. When a
  // pre-persistence historical message has neither `effectiveId` nor
  // `toolCallId`, multiple pills would otherwise share `tool-detail-` and the
  // segmented-control highlight would fly between unrelated panels.
  const fallbackId = useId()

  // While this pill is awaiting approval, register its DOM node with the
  // tool pill visibility registry. The approval bar in ChatInput subscribes
  // and grows a "ghost pill" mirror when this node scrolls out of view, so
  // the user never loses sight of what the tool is about to do. We use
  // useLayoutEffect so registration commits before paint — eliminates the
  // brief flicker that would otherwise show a ghost on the same frame the
  // pill mounts already-visible.
  const pillButtonRef = useRef<HTMLButtonElement>(null)
  useLayoutEffect(() => {
    if (!hasPendingPerm || !toolCallId) return
    const el = pillButtonRef.current
    if (!el) return
    return registerToolPill(toolCallId, el)
  }, [hasPendingPerm, toolCallId])

  // Play the `.ft-block-reveal` entrance fade only on a pill's genuine first
  // appearance, never on a remount (turn promotion, flat→segmented restructure,
  // expand/collapse, virtualizer recycling). `revealId` prefers the message's
  // own tool_call_id (available immediately) and falls back to the resolved
  // toolLog id. The decision is read once in a useState initializer (pure — no
  // side effect in render, so it's StrictMode-safe) and the id is marked
  // revealed in an effect after commit; a later remount finds it already in the
  // set and renders without the animation class, so it appears instantly. An
  // id-less historical pill (no stable identity) falls back to animating.
  const revealId = toolCallId ?? effectiveId ?? null
  const [animateEntrance] = useState(() => !revealId || !revealedToolIds.has(revealId))
  useEffect(() => { if (revealId) revealedToolIds.add(revealId) }, [revealId])

  // Drop the class once the fade has played. It is a ONE-SHOT animation, and
  // `ft-fade` animates opacity — so leaving the class on keeps a
  // compositing/stacking context alive long after the 0.6s is over. That traps
  // a `position: fixed` DESCENDANT (an MCP app's full-screen sheet) inside this
  // row's stacking context instead of the viewport's, leaving it painted
  // beneath shell navigation. Removing the class when it finishes is both tidier
  // and what keeps that escape hatch working.
  //
  // Guarded on the event target so a nested element's animation cannot end the
  // parent's, and only armed while the class is present. Under
  // `prefers-reduced-motion` the animation is `none` and never fires — harmless,
  // because with no opacity animation there is no stacking context to escape.
  const [revealPlayed, setRevealPlayed] = useState(false)
  const revealing = animateEntrance && !revealPlayed

  // Height half of the entrance (see SLIDE_DURATION): grow from 0 so the rows
  // above slide up instead of stepping. Same one-shot decision as the fade —
  // a remount must not replay it — but tracked separately because the two have
  // different lengths, and the height must be released as soon as IT finishes:
  // a lingering inline px height would freeze the row at its entrance size and
  // clip everything that grows later (details panel, MCP app iframe).
  //
  // `revealId` is REQUIRED, unlike for the fade. An id-less pre-persistence
  // historical row cannot be recorded in `revealedToolIds`, so its
  // `animateEntrance` is permanently true and the virtualizer would replay the
  // grow every time the row re-enters the mounted window — shifting layout under
  // a reader scrolling back through history. Replaying an opacity fade there
  // moves nothing, so the fade keeps its id-less fallback; replaying a height
  // does, so the slide takes stable identity or nothing.
  const reduceMotion = useReducedMotion()
  const [slidePlayed, setSlidePlayed] = useState(false)
  // Heat is sampled ONCE at mount: a row that mounts during a hot stream snaps
  // to full height (each animated frame costs a scrollTop re-pin, and bursts
  // of tool calls stack those), while a quiet-transcript mount keeps the ease.
  // Captured in a state initializer so a heat flip mid-animation can neither
  // cut a playing slide short nor start one retroactively — the one-shot
  // revealedToolIds/slidePlayed machinery still prevents replays either way.
  const [snapOnMount] = useState(() => transcriptHot)
  const sliding = !!revealId && animateEntrance && !slidePlayed && !reduceMotion && !snapOnMount
  // Release from the effect rather than from the completion callback alone, so
  // the inline values are cleared however `sliding` ends — the animation
  // finishing, or the OS reduced-motion preference flipping mid-flight, which
  // drops `animate` while framer's `height: 0px` is still on the node and would
  // otherwise leave the row collapsed forever.
  useEffect(() => {
    if (sliding) return
    const el = containerRef.current
    if (!el) return
    el.style.height = ''
    el.style.overflow = ''
  }, [sliding])
  const endSlide = useCallback(() => setSlidePlayed(true), [])

  return (
    <motion.div
      ref={containerRef}
      className={revealing ? 'ft-block-reveal' : undefined}
      initial={sliding ? { height: 0 } : false}
      animate={sliding ? { height: 'auto' } : undefined}
      transition={{ height: { duration: SLIDE_DURATION, ease: SLIDE_EASE } }}
      onAnimationComplete={sliding ? endSlide : undefined}
      style={sliding ? { overflow: 'hidden' } : undefined}
      onAnimationEnd={revealing
        ? (e) => { if (e.target === e.currentTarget) setRevealPlayed(true) }
        : undefined}
    >
      {/* The pill (icon + label) and the file chip WRAP rather than sharing one
          line unconditionally. The chip is `shrink-0` with its own 240px label
          cap while the pill's label is `min-w-0` shrinkable, so on a no-wrap row
          the chip takes its width first and the label lives on whatever is left
          — in a 358px column that left the pill 78px and stacked "Editing
          <long_name>.ion" into a ten-line ribbon beside a chip carrying the same
          truncated name.
          The wrap is deliberately NOT gated on a viewport breakpoint. The
          starvation is a function of the COLUMN's width, and the column is not
          the viewport: `ChatPane` sets `--mc-content-width: 100%`, so a
          quarter-width pane in the session grid is a ~350px column at a 1440px
          viewport. Flex wrapping already keys on the space actually available,
          which is the same axis as the defect — a `md:` gate would have re-pinned
          exactly those panes to one row and let the ribbon back in.
          Wrapping also costs less than an unconditional column: a flex item
          wraps on its BASE size, so the common short row ("Reading the turn
          grouping" + TurnBlock.tsx) keeps its chip beside the label and only a
          pair that genuinely cannot share the width pays a second line. */}
      <div className={`inline-flex flex-wrap items-start gap-x-0 gap-y-1 group/toolpill ${ROW_PILL_WRAPPER_CLASS}`}>
      {/* No `font-mono`: the pill's label is prose with the odd argument spliced
          in ("Searching for 'YOLO' in src"), not code, and Tailwind's
          `font-mono` pins `var(--mono)` — which the Font Family setting never
          writes. The file-path chip below keeps mono, where it is earned. */}
      <button
        ref={pillButtonRef}
        className={`inline-flex ${ROW_PILL_BUTTON_CLASS} focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none ${hasPendingPerm ? 'cursor-default' : 'cursor-pointer hover:brightness-110'}`}
        aria-expanded={effectivelyExpanded}
        title={pillLabelTitle}
        aria-label={hasPendingPerm
          ? i18nT('pages.chat.toolCallLine.aria_awaiting_approval', { label })
          : effectivelyExpanded
            ? i18nT('pages.chat.toolCallLine.aria_hide_details', { label })
            : i18nT('pages.chat.toolCallLine.aria_show_details', { label })}
        onClick={onToggle}
      >
        {/* Deterministic vertical centering: the label spans pin leading-5
            (20px), so the 12px icon centers on the first line at exactly
            (20 − 12) / 2 = 4px. items-start keeps the icon on the first line
            when the label wraps, which only an EXPANDED row now does. */}
        <Icon size={12} className={`shrink-0 ${iconClass}`} style={{ marginTop: '4px' }} />
        {isShimmering ? (
          <motion.span
            data-testid="tool-pill-label"
            className={`${labelWrapClass} min-w-0 leading-5 bg-clip-text`}
            style={{
              backgroundImage: `linear-gradient(90deg, ${shimmerBase} 0%, ${shimmerBase} 40%, ${shimmerHighlight} 50%, ${shimmerBase} 60%, ${shimmerBase} 100%)`,
              backgroundSize: '300% 100%',
              WebkitTextFillColor: 'transparent',
              color: 'transparent',
            }}
            animate={{ backgroundPosition: ['100% 0%', '-50% 0%'] }}
            transition={{ duration: 2.4, repeat: Infinity, ease: 'linear' }}
          >{pillLabelText}</motion.span>
        ) : (
          <span data-testid="tool-pill-label" className={`${labelWrapClass} min-w-0 leading-5 text-muted hover:text-text transition-colors`}>{pillLabelText}</span>
        )}
      </button>

      {/* Side-panel open: a basename CHIP hugging the open-in-pane icon, as one
          clickable unit and a SIBLING of the pill (never nested) — clicking it
          opens the file in the right-hand MarkdownPanel and must NOT toggle the
          pill's expand. stopPropagation guards against the click bubbling to any
          ancestor handler. Unlike the pill label, the chip is always visible: it
          carries the filename (the whole point in purpose mode) and the full
          path lives in the tooltip + expanded details. Neutral at rest, accent
          on hover; the icon inherits the button's currentColor. The `ms-2`
          cancels the wrapper's -ml-2, so when the chip wraps onto its own line
          its left edge lands on the message column's text edge instead of 8px
          into the gutter. It is unconditional because no CSS condition — a media
          query or a container query — can report whether a flex line WRAPPED;
          keying it on a width threshold would only be a proxy that is wrong on
          both sides of its guess. The wrapper's row gap is therefore 0 and this
          margin is the whole separation: 8px on an unwrapped row, where it was
          the wrapper's 4px before. */}
      {showFileOpen && filePath && (
        <button
          className="pi-morph shrink-0 inline-flex items-center gap-1 ms-2 px-1.5 py-0.5 rounded font-mono text-[12px] leading-5 bg-bg-hover text-muted hover:text-accent hover:bg-accent/10 cursor-pointer transition-colors focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none"
          style={{ marginTop: '1px' }}
          onClick={(e) => { e.stopPropagation(); onFileOpen!(filePath) }}
          title={i18nT('pages.chat.toolCallLine.open_in_side_panel', { path: filePath })}
          aria-label={i18nT('pages.chat.toolCallLine.open_in_side_panel', { path: filePath })}
        >
          <span className="max-w-[240px] truncate">{basename}</span>
          <PanelRightSolid size={12} className="shrink-0" />
        </button>
      )}
      </div>

      {/* Diff chip: the one-line handle for every diff presentation. For a
          promoted card it folds/unfolds the card below; for a summary /
          pathname row it expands the details panel. Full path in the native
          tooltip — the visible basename alone cannot tell two same-named
          files apart. Truncated transports prefix counts with ≥ (lower
          bounds) next to a visible localized note. The two testids name which
          of those two roles the chip is playing; both are distinct from
          FoldableDiffBlock's `prose-diff-chip`, which carries the same
          data-diff-toggle and can sit in the very same transcript. */}
      {chipView && (
        <button
          type="button"
          className="mt-1 ml-3 inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-[12px] leading-5 text-muted hover:text-text hover:border-border-strong cursor-pointer transition-colors focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none"
          title={chipView.path ?? undefined}
          aria-expanded={chipView.opensCard ? showCard : effectivelyExpanded}
          data-diff-toggle={chipView.opensCard ? true : undefined}
          data-testid={chipView.opensCard ? 'tool-diff-chip' : 'tool-diff-summary-chip'}
          onClick={e => {
            e.stopPropagation()
            if (chipView.opensCard) toggleCardFolded()
            else onToggle()
          }}
        >
          <FileDiff size={12} className="shrink-0" aria-hidden />
          {chipView.path && <span className="font-mono max-w-[240px] truncate">{chipView.path.split('/').pop()}</span>}
          <span className="tabular-nums">
            {chipView.removed > 0 && <span className="text-danger">{chipView.truncated ? '≥' : ''}-{chipView.removed}</span>}
            {chipView.removed > 0 && chipView.added > 0 && ' '}
            {chipView.added > 0 && <span className="text-ok">{chipView.truncated ? '≥' : ''}+{chipView.added}</span>}
          </span>
          {chipView.truncated && (
            <span className="text-warn">· {i18nT('pages.chat.toolCallLine.diff_truncated')}</span>
          )}
        </button>
      )}
      {/* Diff card: the full inline diff, foldable via the chip above. A
          sibling of the pill (not inside the expanded panel) — the primary
          display of the change; the details panel keeps the raw copy. The
          wrapper is a pointer-only event fence (role="presentation"): clicks
          inside the card must never toggle a surrounding TurnBlock /
          collapsed-group wrapper. */}
      {showCard && diffView?.mode === 'card' && (
        <div className="mt-1.5" role="presentation" onClick={e => e.stopPropagation()}>
          <DiffBlock code={diffView.code} complete onFileOpen={onFileOpen} onFold={toggleCardFolded} />
        </div>
      )}

      <StatusRow show={showShellActivity} snap={transcriptHot}>
        {/* ml-5 = the pill's icon (12px) + the pill BUTTON's gap-2 (8px), so
            this secondary line starts exactly where the pill's label text
            does. (The outer wrapper's gap-1 is between pill and file chip —
            not the icon-to-label gap.) */}
        <div className="ml-5 mt-1 text-[12px] leading-5 text-muted" data-testid="shell-activity">
          <span className="sr-only" aria-live="polite">{i18nT('pages.chat.activityViewer.running')}</span>
          <span aria-hidden="true" className="tabular-nums font-mono">
            {liveShellActivity ? `${i18nT('pages.chat.activityViewer.running')} · ${elapsedLabel}` : elapsedLabel}
          </span>
        </div>
      </StatusRow>
      <StatusRow show={showWaitCountdown} snap={transcriptHot}>
        {/* Same ml-5 anchor as the shell-activity line above: the countdown is
            a continuation of the pill's label text, not of its icon column. */}
        <div className="ml-5 mt-1 flex items-center gap-2 text-[12px] leading-5 text-muted" data-testid="wait-countdown">
          {/* The status word is announced once; the digits are aria-hidden so a
              screen reader is not re-read every second. Same split as the shell
              activity row above. */}
          <span className="sr-only" aria-live="polite">{i18nT('pages.chat.toolCallLine.wait_status')}</span>
          {/* tabular-nums only, no font-mono: the fixed-width digits are what
              stops the label jittering as it ticks, but a mono FACE next to the
              pill's body-font label reads as a different kind of text. */}
          <span aria-hidden="true" className="tabular-nums">
            {i18nT('pages.chat.toolCallLine.wait_status')} · {i18nT('pages.chat.toolCallLine.wait_remaining', { time: remainingLabel })}
          </span>
          {/* stopPropagation is defensive, not load-bearing for the pill: this
              row is a SIBLING of the pill button, not inside its click target.
              It guards against an ancestor click handler (TurnBlock and the
              collapsed-group wrapper both bind one) treating the press as a
              request to expand or collapse the surrounding block. */}
          <button
            type="button"
            disabled={endingWait}
            data-testid="wait-end-now"
            className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[12px] leading-5 cursor-pointer font-body hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={e => { e.stopPropagation(); endWaitNow() }}
          >
            {endingWait
              ? i18nT('pages.chat.toolCallLine.wait_ending')
              : i18nT('pages.chat.toolCallLine.wait_end_now')}
          </button>
        </div>
      </StatusRow>

      <AnimatePresence initial={false}>
        {effectivelyExpanded && (
          <motion.div
            key="tool-details"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.35, ease: [0.4, 0.0, 0.2, 1] /* Material standard */ }}
            style={{ overflow: 'hidden' }}
          >
            <ToolDetails purpose={purpose} pillLabel={toolLabel} toolName={label} input={input} output={isAutoDenied ? denyOutput : output} auto={auto} pending={hasPendingPerm} ts={ts} hasEntry={hasEntry} fmtTime={fmtTime} barColor={barStyle} layoutId={`tool-detail-${effectiveId || toolCallId || fallbackId}`} flush />
          </motion.div>
        )}
      </AnimatePresence>

      {/* MCP App (SEP-1865): inline sandboxed render attached to this tool
          call. Appears below the details panel; presence is driven by the
          `mcp_app_render` WS event stored in chat.mcpApps. */}
      {/* With dashboard.mcp_app_panel on, the app is hosted by its own side-panel
          tab instead of here. The bubble keeps a compact affordance: a
          historical transcript has no live panel, and an empty gap where a
          diagram used to be reads as a broken render. Clicking it re-focuses —
          or re-creates — the tab, which is the route back after the user closes
          it, since auto-open does not re-open a tab they dismissed. */}
      {mcpApp && (appInPanel
        ? (
          <button
            type="button"
            onClick={() => { if (toolCallId) onOpenApp?.(toolCallId) }}
            className="mt-1.5 flex items-center gap-2 px-1.5 py-0.5 -ml-1.5 rounded text-[12px] leading-5 text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none cursor-pointer transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
          >
            <PanelRight size={13} aria-hidden />
            <span>{i18nT('pages.chat.toolCallLine.opened_in_the_side_panel')}</span>
          </button>
        )
        : <McpAppFrame payload={mcpApp} />)}
    </motion.div>
  )
})
