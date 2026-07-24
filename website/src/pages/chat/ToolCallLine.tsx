import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { shallowEqual } from 'react-redux'
import { useAppSelector, useAppDispatch } from '../../store'
import { clearFocusToolCallId, mcpAppKey } from '../../store/chatSlice'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { LoaderCircle, CircleSlash, CircleDot, Lock, PanelRight } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import type { ChatMessage } from '../../types'
import { ToolDetails } from './ToolDetails'
import { registerToolPill } from '../../store/toolPillRegistry'
import { extractToolFilePath } from '../../utils/toolFilePath'
import { isSafePath } from '../../utils/safePath'
import { fileReadUrl } from '../../utils/fileReadUrl'
import McpAppFrame from '../../components/McpAppFrame'

/** Inline tool call pill. Click toggles an expanded panel below the pill that
 *  shows purpose / input / output (the same details that previously lived in
 *  the Activity sidebar's deprecated "Tools" tab). */
export default memo(function ToolCallLine({ message, running: _running, slot, onFileOpen }: { message: ChatMessage; running: boolean; slot?: string; onFileOpen?: (path: string) => void }) {
  const dispatch = useAppDispatch()
  const label = message.content.replace(/^🔧\s*/, '')
  const toolCallId = message.meta?.tool_call_id as string | undefined
  const simplified = useSimplifiedToolNames()

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
  const { effectiveId, isDone, isRejected, purpose, input, output, auto, ts, hasEntry } = useAppSelector(s => {
    // Slot-aware: for a non-active slot (split-view pane) read that slot's
    // per-slot tool log / messages / running state; `slot` undefined or equal to
    // the active slot → active-slot globals (behavior identical to before).
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

    for (let i = log.length - 1; i >= 0; i--) {
      const e = log[i]
      if (e.type !== 'tool') continue
      if ((toolCallId && e.tool_call_id === toolCallId) || (!toolCallId && e.tool_call_id && label.includes(e.text))) {
        const rejected = !!e.rejected || wasRejectedByPerm()
        const isDone = e.output != null || rejected || !slotRunning
        return {
          effectiveId: e.tool_call_id || null,
          isDone, isRejected: rejected,
          purpose: e.purpose || '',
          input: e.input || '',
          output: e.output || '',
          auto: !!e.auto,
          ts: e.ts || 0,
          hasEntry: true,
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
      purpose: (message.meta?.purpose as string) || '',
      input: metaInput, output: metaOutput, auto: false,
      // ChatMessage.ts is a string (ISO timestamp) when restored from history;
      // parse it for the meta-row time renderer. Falls to 0 if unparseable —
      // fmtTime hides the row when ts is 0.
      ts: typeof message.ts === 'number' ? message.ts : (message.ts ? Date.parse(String(message.ts)) || 0 : 0),
      // Treat the message as having an entry when persisted I/O is available,
      // so the empty-state copy only shows for truly bare historical messages.
      hasEntry: !!(metaInput || metaOutput),
    }
  }, shallowEqual)

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
  const [expanded, setExpanded] = useState(() => hasPendingPerm)
  const [pendingAutoExpand, setPendingAutoExpand] = useState(() => hasPendingPerm)
  const prevPendingRef = useRef(hasPendingPerm)
  useEffect(() => {
    const wasPending = prevPendingRef.current
    prevPendingRef.current = hasPendingPerm
    if (hasPendingPerm && !wasPending) {
      // Approval just became pending → auto-expand
      setExpanded(true)
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
        setExpanded(false)
        setPendingAutoExpand(false)
      })
      return () => cancelAnimationFrame(raf)
    }
  }, [hasPendingPerm, pendingAutoExpand])

  const containerRef = useRef<HTMLDivElement>(null)

  // External focus signal (e.g. from ChatInput's "Jump to tool" link). When the
  // redux focusToolCallId matches this pill, auto-expand and scroll into view,
  // then clear the focus so subsequent re-renders don't keep firing. Treat the
  // jump as user intent — clear pendingAutoExpand so the panel stays open
  // through approval resolution.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (focusToolCallId && effectiveId && focusToolCallId === effectiveId) {
      setExpanded(true)
      setPendingAutoExpand(false)
      requestAnimationFrame(() => containerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
      dispatch(clearFocusToolCallId())
    }
  }, [focusToolCallId, effectiveId, dispatch])

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
    ? (isRejected ? CircleSlash : CircleDot)
    : hasPendingPerm ? Lock
    : LoaderCircle
  const iconClass = isDone
    ? (isRejected ? 'text-danger' : 'text-ok')
    : hasPendingPerm ? 'text-warn'
    : 'text-accent animate-spin'
  // Match the panel's left rail to the pill's status — keeps the visual chain
  // (icon → bar → content) coherent across rejected (red), done (green),
  // pending-approval (yellow), and running (accent) states. Inline style with
  // color-mix() rather than Tailwind opacity classes — the project's Tailwind
  // config doesn't compile `border-{color}/N` opacity variants for theme colors.
  const barColor = isDone
    ? (isRejected ? 'var(--danger)' : 'var(--ok)')
    : hasPendingPerm ? 'var(--warn)' : 'var(--accent)'
  const barStyle = `color-mix(in srgb, ${barColor} 70%, transparent)`
  const toolLabel = (simplified && (purpose || message.meta?.purpose)) ? (purpose || message.meta?.purpose as string) : label

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
    setExpanded(e => !e)
    setPendingAutoExpand(false)
  }, [hasPendingPerm])

  const fmtTime = (t: number) => t ? new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''

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

  return (
    <div ref={containerRef} className="ft-block-reveal">
      <div className="inline-flex items-start gap-1 group/toolpill">
      <button
        ref={pillButtonRef}
        className={`inline-flex items-start gap-1 text-[13px] font-mono px-2 py-0.5 rounded-md transition-all text-left focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none ${hasPendingPerm ? 'cursor-default' : 'cursor-pointer hover:brightness-110'}`}
        aria-expanded={effectivelyExpanded}
        aria-label={hasPendingPerm ? `Awaiting approval for tool: ${label}` : `${effectivelyExpanded ? 'Hide' : 'Show'} details for tool: ${label}`}
        onClick={onToggle}
      >
        <Icon size={12} className={`shrink-0 ${iconClass}`} style={{ marginTop: '3px' }} />
        {isShimmering ? (
          <motion.span
            className="break-words min-w-0 bg-clip-text"
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
          <span className="break-words min-w-0 text-muted hover:text-text transition-colors">{displayLabel}</span>
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
          className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-mono text-[12px] leading-tight bg-bg-hover text-muted hover:text-accent hover:bg-accent/10 cursor-pointer transition-colors focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none"
          style={{ marginTop: '1px' }}
          onClick={(e) => { e.stopPropagation(); onFileOpen!(filePath) }}
          title={`Open ${filePath} in side panel`}
          aria-label={`Open ${filePath} in side panel`}
        >
          <span className="max-w-[240px] truncate">{basename}</span>
          <PanelRight size={12} className="shrink-0" />
        </button>
      )}
      </div>

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
            <ToolDetails purpose={purpose} pillLabel={toolLabel} toolName={label} input={input} output={output} auto={auto} pending={hasPendingPerm} ts={ts} hasEntry={hasEntry} fmtTime={fmtTime} barColor={barStyle} layoutId={`tool-detail-${effectiveId || toolCallId || fallbackId}`} />
          </motion.div>
        )}
      </AnimatePresence>

      {/* MCP App (SEP-1865): inline sandboxed render attached to this tool
          call. Appears below the details panel; presence is driven by the
          `mcp_app_render` WS event stored in chat.mcpApps. */}
      {mcpApp && <McpAppFrame payload={mcpApp} />}
    </div>
  )
})
