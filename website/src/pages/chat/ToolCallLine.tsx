import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { shallowEqual } from 'react-redux'
import { useAppSelector, useAppDispatch } from '../../store'
import { clearFocusToolCallId, mcpAppKey } from '../../store/chatSlice'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useLanguage } from '../../i18n/LanguageProvider'
import { pickToolLabel } from '../../utils/toolLabel'
import { LoaderCircle, CircleSlash, CircleAlert, CircleDot, Lock, PanelRight } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { motion, AnimatePresence } from 'framer-motion'
import type { ChatMessage } from '../../types'
import { ToolDetails } from './ToolDetails'
import { registerToolPill } from '../../store/toolPillRegistry'
import { extractToolFilePath } from '../../utils/toolFilePath'
import { isSafePath } from '../../utils/safePath'
import { fileReadUrl } from '../../utils/fileReadUrl'
import McpAppFrame from '../../components/McpAppFrame'
import { i18nT } from '../../i18n/t'
import { fmtDateFields, fmtDuration as fmtDurationParts } from '../../i18n/format'

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

/** Inline tool call pill. Click toggles an expanded panel below the pill that
 *  shows purpose / input / output. */
export default memo(function ToolCallLine({ message, running: _running, slot, onFileOpen, disclosure, disclosureKey, onDisclosureChange, appInPanel, onOpenApp }: { message: ChatMessage; running: boolean; slot?: string; onFileOpen?: (path: string) => void; disclosure?: boolean; disclosureKey?: string; onDisclosureChange?: (key: string, expanded: boolean) => void; appInPanel?: boolean; onOpenApp?: (toolCallId: string) => void }) {
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
  // expansion as well as completion status for the icon.
  const { effectiveId, isDone: logIsDone, isRejected, isAutoDenied, purpose, input, output, auto, ts, hasEntry, isShell } = useAppSelector(s => {
    // Slot-aware: for a non-active slot (split-view pane) read that slot's
    // per-slot tool log / messages / running state; `slot` undefined or equal to
    // the active slot → active-slot globals.
    const bg = slot && slot !== s.chat.activeSlot ? slot : null
    const log = bg ? (s.chat.slotActivity[bg]?.toolLog ?? []) : s.chat.toolLog
    const slotRunning = bg ? ((s.chat.slotRun[bg]?.state ?? 'idle') !== 'idle') : s.chat.slotRunning
    const msgs = bg ? (s.chat.slotMessages[bg] ?? []) : s.chat.messages

    // Helper: check if this tool's permission was resolved as rejected
    const wasRejectedByPerm = () => {
      if (!toolCallId) return false
      for (let j = msgs.length - 1; j >= 0; j--) {
        const m = msgs[j]
        if (m.role !== 'permission' || !m.meta?.tool_call_id) continue
        if (m.meta.tool_call_id === toolCallId) {
          return m.meta?.resolved === 'rejected'
        }
      }
      return false
    }

    // Auto-denied detection. When a security-policy deny rule or hook blocks a
    // tool, the gateway appends a SECOND tool message — "🚫 <title> …" —
    // sharing this pill's tool_call_id. That message never renders (TurnBlock
    // hides every tool message not starting with 🔧), so the visible 🔧 pill
    // must find its hidden sibling to know the call was blocked. Only the
    // sibling's PRESENCE is used: its content is a redacted event title (often
    // just "shell"), not a reliable human-readable reason, so the Output panel
    // shows a standard "blocked by security policy" message instead. The
    // interactive user-reject path also appends a 🚫 message, but that flow
    // ALSO resolves a permission message as rejected — wasRejectedByPerm()
    // takes precedence below, so a user rejection still shows red, not amber.
    const hasAutoDenySibling = (): boolean => {
      if (!toolCallId) return false
      for (let j = msgs.length - 1; j >= 0; j--) {
        const m = msgs[j]
        if (m.role !== 'tool' || m.meta?.tool_call_id !== toolCallId) continue
        if (m.content.startsWith('🚫')) return true
        // The pill's own 🔧 message reached without a 🚫 sibling above it —
        // any earlier match would predate this call; stop scanning.
        if (m === message) break
      }
      return false
    }
    const autoDenied = hasAutoDenySibling()

    for (let i = log.length - 1; i >= 0; i--) {
      const e = log[i]
      if (e.type !== 'tool') continue
      if ((toolCallId && e.tool_call_id === toolCallId) || (!toolCallId && e.tool_call_id && label.includes(e.text))) {
        const rejected = !!e.rejected || wasRejectedByPerm()
        const isDone = e.output != null || rejected || autoDenied || !slotRunning
        return {
          effectiveId: e.tool_call_id || null,
          isDone, isRejected: rejected,
          isAutoDenied: !rejected && autoDenied,
          purpose: e.purpose || '',
          input: e.input || '',
          output: e.output || '',
          auto: !!e.auto,
          ts: e.ts || 0,
          hasEntry: true,
          // Older ACP update frames may omit is_shell; execute is the stable
          // tool-kind value used by the transport and keeps those frames live.
          isShell: e.is_shell === true || e.kind === 'execute' || e.text.startsWith('Running:'),
        }
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
      purpose: (message.meta?.purpose as string) || '',
      input: metaInput, output: metaOutput, auto: false,
      // ChatMessage.ts is a string (ISO timestamp) when restored from history;
      // parse it for the meta-row time renderer. Falls to 0 if unparseable —
      // fmtTime hides the row when ts is 0.
      ts: typeof message.ts === 'number' ? message.ts : (message.ts ? Date.parse(String(message.ts)) || 0 : 0),
      // Treat the message as having an entry when persisted I/O is available,
      // so the empty-state copy only shows for truly bare historical messages.
      hasEntry: !!(metaInput || metaOutput),
      isShell: false,
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
    const msgs = bg ? (s.chat.slotMessages[bg] ?? []) : s.chat.messages
    for (let j = msgs.length - 1; j >= 0; j--) {
      const m = msgs[j]
      if (m.role !== 'permission' || m.meta?.resolved || !m.meta?.tool_call_id) continue
      if (m.meta.tool_call_id === toolCallId) return true
    }
    return false
  })

  // Shell commands do not expose a reliable total, so their live indicator is
  // deliberately indeterminate. The existing tool output remains the source
  // of truth; this status only makes an in-flight command visible while its
  // details panel is collapsed after approval.
  const showShellActivity = isShell && turnRunning && !hasPendingPerm
  const [activityNow, setActivityNow] = useState(() => Date.now())
  const activityStartRef = useRef(Date.now())
  const wasPendingRef = useRef(hasPendingPerm)

  useEffect(() => {
    if (hasPendingPerm) {
      wasPendingRef.current = true
      return
    }
    if (wasPendingRef.current) {
      activityStartRef.current = Date.now()
      wasPendingRef.current = false
      setActivityNow(Date.now())
    }
  }, [hasPendingPerm])

  useEffect(() => {
    if (!showShellActivity) return
    const timer = window.setInterval(() => setActivityNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [showShellActivity])

  const elapsedSeconds = Math.max(0, Math.floor((activityNow - activityStartRef.current) / 1000))
  const elapsedLabel = fmtDurationParts(
    [[Math.floor(elapsedSeconds / 60), 'minute'], [elapsedSeconds % 60, 'second']],
    { dropZero: true },
  )

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

  return (
    <div
      ref={containerRef}
      className={revealing ? 'ft-block-reveal' : undefined}
      onAnimationEnd={revealing
        ? (e) => { if (e.target === e.currentTarget) setRevealPlayed(true) }
        : undefined}
    >
      <div className="inline-flex items-start gap-1 group/toolpill max-w-full min-w-0">
      {/* No `font-mono`: the pill's label is prose with the odd argument spliced
          in ("Searching for 'YOLO' in src"), not code, and Tailwind's
          `font-mono` pins `var(--mono)` — which the Font Family setting never
          writes. The file-path chip below keeps mono, where it is earned. */}
      <button
        ref={pillButtonRef}
        className={`inline-flex items-start gap-1 min-w-0 max-w-full text-[13px] px-2 py-0.5 rounded-md transition-all text-left focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none ${hasPendingPerm ? 'cursor-default' : 'cursor-pointer hover:brightness-110'}`}
        aria-expanded={effectivelyExpanded}
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
            when the label wraps. */}
        <Icon size={12} className={`shrink-0 ${iconClass}`} style={{ marginTop: '4px' }} />
        {isShimmering ? (
          <motion.span
            className="break-words min-w-0 leading-5 bg-clip-text"
            style={{
              backgroundImage: `linear-gradient(90deg, ${shimmerBase} 0%, ${shimmerBase} 40%, ${shimmerHighlight} 50%, ${shimmerBase} 60%, ${shimmerBase} 100%)`,
              backgroundSize: '300% 100%',
              WebkitTextFillColor: 'transparent',
              color: 'transparent',
            }}
            animate={{ backgroundPosition: ['100% 0%', '-50% 0%'] }}
            transition={{ duration: 2.4, repeat: Infinity, ease: 'linear' }}
          >{displayLabel}</motion.span>
        ) : (
          <span className="break-words min-w-0 leading-5 text-muted hover:text-text transition-colors">{displayLabel}</span>
        )}
      </button>

      {/* Side-panel open: a basename CHIP hugging the open-in-pane icon, as one
          clickable unit and a SIBLING of the pill (never nested) — clicking it
          opens the file in the right-hand MarkdownPanel and must NOT toggle the
          pill's expand. stopPropagation guards against the click bubbling to any
          ancestor handler. Unlike the pill label, the chip is always visible: it
          carries the filename (the whole point in purpose mode) and the full
          path lives in the tooltip + expanded details. Neutral at rest, accent
          on hover; the icon inherits the button's currentColor. */}
      {showFileOpen && filePath && (
        <button
          className="pi-morph shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-mono text-[12px] leading-tight bg-bg-hover text-muted hover:text-accent hover:bg-accent/10 cursor-pointer transition-colors focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none"
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

      {showShellActivity && (
        <div className="ml-3 mt-1 text-[12px] text-muted">
          <span className="sr-only" aria-live="polite">{i18nT('pages.chat.activityViewer.running')}</span>
          <span aria-hidden="true" className="tabular-nums font-mono">
            {i18nT('pages.chat.activityViewer.running')} · {elapsedLabel}
          </span>
        </div>
      )}

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
            <ToolDetails purpose={purpose} pillLabel={toolLabel} toolName={label} input={input} output={isAutoDenied ? i18nT('pages.chat.toolCallLine.blocked_by_security_policy') : output} auto={auto} pending={hasPendingPerm} ts={ts} hasEntry={hasEntry} fmtTime={fmtTime} barColor={barStyle} layoutId={`tool-detail-${effectiveId || toolCallId || fallbackId}`} />
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
            className="mt-1.5 flex items-center gap-1.5 px-1.5 py-0.5 -ml-1.5 rounded text-[12px] text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none cursor-pointer transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
          >
            <PanelRight size={13} aria-hidden />
            <span>{i18nT('pages.chat.toolCallLine.opened_in_the_side_panel')}</span>
          </button>
        )
        : <McpAppFrame payload={mcpApp} />)}
    </div>
  )
})
