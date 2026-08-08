import { useState, useRef, useEffect, memo } from 'react'
import { AnimatePresence, motion, useMotionValue, useSpring } from 'framer-motion'
import { Hourglass, ChevronUp, X, Zap, Pencil, Check, Bot, Loader2 } from 'lucide-react'
import type { ChatMessage } from '../types'

import { i18nT } from '../i18n/t'
import { parseRecoveryMessage } from '../pages/chat/RecoveryCard'
import { hasSubagentCompletionPrefix } from '../pages/chat/subagentCompletion'
/** System-injected sub-agent completion deliveries waiting for the busy slot.
 *  These are NOT user messages: they must not be editable/cancellable (either
 *  would silently lose a finished agent's result) and rendering each as a
 *  queue card is noise at scale — they collapse into one progress line
 *  (SubagentDeliveryProgress) instead of the interactive QueueStack. */
export function isSystemDelivery(m: ChatMessage): boolean {
  return hasSubagentCompletionPrefix(m.content || '')
}

/** A queued entry that must NOT render as an interactive (edit/cancel) user
 *  card. Two families qualify, both machine orchestration rather than user
 *  speech:
 *    - sub-agent completion deliveries (isSystemDelivery), and
 *    - synthetic turn-recovery continuations (tool refusal / stalled turn /
 *      stalled tool / interrupted / empty response), which the gateway
 *      re-queues automatically and which surface as a compact RecoveryCard in
 *      the transcript once dequeued.
 *  Editing or cancelling either would corrupt an automatic effect, so they are
 *  filtered out of the QueueStack (sub-agent deliveries are still counted for
 *  the progress line via isSystemDelivery). */
export function isNonInteractiveQueued(m: ChatMessage): boolean {
  return isSystemDelivery(m) || parseRecoveryMessage(m.content || '') !== null
}

/** Split a slot's message list into the three things a pane surface needs:
 *  the transcript (everything not queued), the INTERACTIVE queue cards, and a
 *  count of held sub-agent deliveries for the collapsed progress line.
 *
 *  One pass, and one place. Callers own the composer's `input` state, so they
 *  re-render on every keystroke; deriving these in a render body handed the
 *  transcript array a fresh identity per character, which defeated the memo()
 *  on ChatMessageList and re-ran its O(N) turn grouping while the user typed.
 *  Callers must wrap this in a `useMemo` keyed on the input array. */
export function splitPaneMessages(allMessages: ChatMessage[]): {
  messages: ChatMessage[]
  queuedMessages: ChatMessage[]
  systemDeliveryCount: number
} {
  const messages: ChatMessage[] = []
  const queuedMessages: ChatMessage[] = []
  let systemDeliveryCount = 0
  for (const m of allMessages) {
    if (m.role !== 'queued') { messages.push(m); continue }
    // Both queue predicates are independent, not mutually exclusive: a
    // sub-agent delivery is excluded from the interactive stack AND counted
    // for the progress line.
    if (!isNonInteractiveQueued(m)) queuedMessages.push(m)
    if (isSystemDelivery(m)) systemDeliveryCount++
  }
  return { messages, queuedMessages, systemDeliveryCount }
}

/** One quiet, non-interactive line summarizing held sub-agent deliveries —
 *  "the results are in; they'll be processed when the current turn finishes". */
export function SubagentDeliveryProgress({ count }: { count: number }) {
  if (count <= 0) return null
  return (
    <div
      className="mx-auto w-full px-5"
      style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
      data-testid="subagent-delivery-progress"
    >
      <div className="mb-1 flex items-center gap-2 rounded-md bg-accent/5 border border-accent/15 px-3 py-1.5 text-[12px] font-mono text-muted">
        <Bot size={13} className="text-accent/70 shrink-0" />
        <Loader2 size={12} className="animate-spin text-accent/70 shrink-0" />
        <span>
          {i18nT('components.queueStack.sub_agent_result', { count: count })} {i18nT('components.queueStack.ready_processing_after_the_current_turn')}
        </span>
      </div>
    </div>
  )
}

const MAX_PEEK = 2
const CARD_H = 40
const PEEK = 6
const EXPANDED_GAP = 4
const SCALE_STEP = 0.04
const HIDDEN_EXTRA_SCALE = 0.02
const OVERLAP = 11 // overlap to fuse with input area below

const DEPTH_BRIGHTNESS = [1, 0.88, 0.76]
const SPRING = { type: 'spring' as const, stiffness: 400, damping: 30 }

/** Inline editor (input + save) swapped in for the message text while editing.
 *  Owns the live value so its own controls commit the typed text, never stale content. */
function EditInput({ initial, onCommit, onCancel }: {
  initial: string
  onCommit: (value: string) => void
  onCancel: () => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  const [value, setValue] = useState(initial)
  // Guard so blur and an explicit save/Enter don't both fire onCommit.
  const committedRef = useRef(false)
  useEffect(() => { ref.current?.focus(); ref.current?.select() }, [])
  // Commit only a real change: skip empty and unchanged values so a stray
  // focus→blur (or clear→blur) doesn't fire a no-op PATCH + WS broadcast.
  const commit = () => {
    if (committedRef.current) return
    committedRef.current = true
    const trimmed = value.trim()
    if (trimmed && trimmed !== initial.trim()) onCommit(value)
    else onCancel()
  }
  const cancel = () => { if (committedRef.current) return; committedRef.current = true; onCancel() }
  return (
    <>
      <input
        ref={ref}
        value={value}
        onChange={e => setValue(e.target.value)}
        // Stop the card's expand/collapse + drag handlers from swallowing pointer + key events.
        onPointerDown={e => e.stopPropagation()}
        onClick={e => e.stopPropagation()}
        onKeyDown={e => {
          e.stopPropagation()
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commit() }
          else if (e.key === 'Escape') { e.preventDefault(); cancel() }
        }}
        onBlur={commit}
        className="flex-1 min-w-0 bg-white/20 text-warn-fg placeholder:text-warn-fg/50 rounded px-1.5 py-0.5 text-[13px] outline-none border border-white/30 focus:border-white/60"
        aria-label={i18nT('components.queueStack.edit_queued_message')}
      />
      <button className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors text-white"
        title={i18nT('components.queueStack.save')} aria-label={i18nT('components.queueStack.save_edit')}
        // mousedown commits before the input's blur can fire with the same value.
        onMouseDown={e => { e.preventDefault(); e.stopPropagation() }}
        onClick={e => { e.stopPropagation(); commit() }}>
        <Check size={13} />
      </button>
    </>
  )
}

function QueueStackInner({ messages, onCancel, onInterrupt, onEdit, fuseBelow = true, pendingIds }: {
  messages: ChatMessage[]
  onCancel?: (queueId: string) => void
  onInterrupt?: (queueId: string) => void
  onEdit?: (queueId: string, content: string) => void
  /** Queue ids whose cancel/edit is in flight. Their controls are disabled so a
   *  second click cannot fire a duplicate request — on a surface where the card
   *  is only retired once the server confirms, that second request races the
   *  first and comes back 404, reporting a failure for an action that worked. */
  pendingIds?: ReadonlySet<string>
  /** When true (default) the front collapsed card fuses into the surface directly
   *  below it (the input box) via a negative bottom margin + a flat, borderless bottom
   *  edge. Set false when a non-fusable element sits between the queue and the input box
   *  (follow-up option chips or the knowledge chip): the card then keeps its negative
   *  margin off and renders as a complete rounded card cleanly above that element instead
   *  of overlapping it. */
  fuseBelow?: boolean
}) {
  const [_expanded, setExpanded] = useState(false)
  const expanded = _expanded && messages.length > 1
  const [editingId, setEditingId] = useState<string | null>(null)

  // Reset expanded when queue drains to trivial size
  useEffect(() => {
    if (messages.length <= 1) setExpanded(false)
  }, [messages.length])

  // Drop a stale edit target if its card leaves the queue (e.g. dequeued / cancelled).
  useEffect(() => {
    if (editingId && !messages.some(m => (m.meta?.queueId as string) === editingId)) setEditingId(null)
  }, [messages, editingId])

  const commitEdit = (queueId: string, content: string) => {
    setEditingId(null)
    if (onEdit) onEdit(queueId, content)
  }
  const cancelEdit = () => setEditingId(null)

  const peekCount = Math.min(MAX_PEEK, Math.max(0, messages.length - 1))
  const collapsedHeight = messages.length > 0 ? CARD_H + peekCount * PEEK : 0
  const expandedHeight = messages.length > 0 ? messages.length * CARD_H + (messages.length - 1) * EXPANDED_GAP : 0

  const targetHeight = expanded ? expandedHeight : collapsedHeight
  const targetMargin = messages.length > 0 && !expanded && fuseBelow ? -OVERLAP : 0

  // Imperatively control margin: spring on expand/collapse, snap on enter/exit
  const marginMV = useMotionValue(targetMargin)
  const marginSpring = useSpring(marginMV, SPRING)
  const prevExpanded = useRef(expanded)

  useEffect(() => {
    const expandChanged = prevExpanded.current !== expanded
    prevExpanded.current = expanded

    if (expandChanged) {
      // Expand/collapse: animate via spring
      marginMV.set(targetMargin)
    } else if (messages.length > 0) {
      // Enter (count increased) or count decreased but not to 0: snap immediately
      // When count hits 0, let onExitComplete handle the margin reset
      marginSpring.jump(targetMargin)
    }
  }, [expanded, targetMargin, messages.length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Handle last-card exit: snap margin to 0 when AnimatePresence finishes
  const prevCountForExit = useRef(messages.length)
  const hasExitingRef = useRef(false)
  useEffect(() => {
    if (messages.length < prevCountForExit.current) hasExitingRef.current = true
    prevCountForExit.current = messages.length
  }, [messages.length])

  const onExitComplete = () => {
    hasExitingRef.current = false
    if (messages.length === 0) marginSpring.jump(0)
  }

  return (
    <div className="px-5 mx-auto w-full relative" style={{ maxWidth: 'var(--mc-content-width, 900px)', zIndex: 0 }}>
      <motion.div
        className="relative cursor-pointer"
        animate={{ height: targetHeight }}
        transition={SPRING}
        style={{ marginBottom: marginSpring }}
        onClick={() => messages.length > 1 && setExpanded(e => !e)}
        onKeyDown={(e: React.KeyboardEvent) => {
          if ((e.key === 'Enter' || e.key === ' ') && messages.length > 1) {
            e.preventDefault()
            setExpanded(prev => !prev)
          }
        }}
        role={messages.length > 1 ? 'button' : undefined}
        tabIndex={messages.length > 1 ? 0 : undefined}
        aria-expanded={messages.length > 1 ? expanded : undefined}
      >
        <AnimatePresence initial={false} onExitComplete={onExitComplete}>
          {messages.map((m, i) => {
            let y: number
            let scale: number
            let opacity: number
            let zIndex: number
            let brightness: number

            if (expanded) {
              const pos = messages.length - 1 - i
              y = pos * (CARD_H + EXPANDED_GAP)
              scale = 1
              opacity = 1
              zIndex = pos + 1
              brightness = 1
            } else if (i <= MAX_PEEK) {
              const depth = i
              y = (collapsedHeight - CARD_H) - depth * PEEK
              scale = 1 - (depth + 1) * SCALE_STEP
              opacity = 1
              zIndex = (MAX_PEEK + 1) - depth
              brightness = DEPTH_BRIGHTNESS[depth] ?? DEPTH_BRIGHTNESS[MAX_PEEK]
            } else {
              y = (collapsedHeight - CARD_H) - MAX_PEEK * PEEK
              scale = 1 - (MAX_PEEK + 1) * SCALE_STEP - HIDDEN_EXTRA_SCALE
              opacity = 0
              zIndex = 0
              brightness = DEPTH_BRIGHTNESS[MAX_PEEK]
            }

            const isFrontCollapsed = !expanded && i === 0
            // Flat, borderless bottom (to seam into the input box) only when we're
            // actually fusing into the surface below. When fuseBelow is off, keep the
            // card fully rounded/bordered so it doesn't look cut off above the chips.
            const fused = isFrontCollapsed && fuseBelow
            const queueId = m.meta?.queueId as string | undefined
            const isEditing = !!queueId && editingId === queueId
            const isPending = !!queueId && !!pendingIds?.has(queueId)
            // Per-card actions show on the front single card or when expanded.
            const showActions = (expanded || messages.length === 1) && !!queueId

            return (
              <motion.div
                key={m.meta?.queueId as string ?? m.ts ?? `q-${i}-${m.content}`}
                initial={false}
                animate={{
                  opacity, y, scale,
                  filter: `brightness(${brightness})`,
                  borderTopLeftRadius: 12,
                  borderTopRightRadius: 12,
                  borderBottomLeftRadius: fused ? 0 : 12,
                  borderBottomRightRadius: fused ? 0 : 12,
                  borderBottomWidth: fused ? 0 : 1,
                }}
                exit={{ y: y + 40, zIndex: 50, borderBottomWidth: 1, borderBottomLeftRadius: 12, borderBottomRightRadius: 12, transition: SPRING }}
                transition={SPRING}
                // Theme colors are raw var(--x) without <alpha-value>, so Tailwind
                // alpha modifiers (bg-warn/15) silently generate no CSS. Use explicit
                // color-mix instead — and mix the bg toward the opaque surface color
                // (not transparent): cards overlap in the collapsed peek stack, so a
                // translucent bg would let the cards behind bleed through. The
                // kiro-dark .queue-card override in index.css still takes precedence.
                className="queue-card absolute top-0 left-0 right-0 bg-[color-mix(in_srgb,var(--warn)_15%,var(--bg-elevated))] border border-[color-mix(in_srgb,var(--warn)_40%,transparent)] px-3 py-2 text-[13px] text-warn"
                style={{ transformOrigin: 'bottom center', height: CARD_H, zIndex }}
              >
                <span className="flex items-center gap-1.5 h-full">
                  <span className="shrink-0 text-[10px] font-mono opacity-50 w-4 text-center">{i + 1}</span>
                  {isFrontCollapsed && (
                    <span className="shrink-0 inline-flex animate-[hourglass-flip_3s_ease-in-out_infinite]">
                      <Hourglass size={13} />
                    </span>
                  )}
                  {isEditing && onEdit ? (
                    <EditInput initial={m.content} onCommit={v => commitEdit(queueId!, v)} onCancel={cancelEdit} />
                  ) : (
                    <>
                      <span className="truncate flex-1">{m.content}</span>
                      {onEdit && showActions && (
                        <button
                          className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={i18nT('components.queueStack.edit_queued_message')}
                          aria-label={i18nT('components.queueStack.edit_queued_message')}
                          disabled={isPending}
                          onClick={(e) => { e.stopPropagation(); setEditingId(queueId!) }}
                        >
                          <Pencil size={13} />
                        </button>
                      )}
                      {onInterrupt && showActions && (
                        <button
                          className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors text-white disabled:opacity-40 disabled:cursor-not-allowed"
                          title={i18nT('components.queueStack.interrupt_current_turn_and_send_this_now')}
                          aria-label={i18nT('components.queueStack.send_now')}
                          disabled={isPending}
                          onClick={(e) => { e.stopPropagation(); onInterrupt(queueId!) }}
                        >
                          <Zap size={13} fill="currentColor" />
                        </button>
                      )}
                      {onCancel && showActions && (
                        <button
                          className="shrink-0 p-0.5 rounded hover:bg-white/20 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={i18nT('components.queueStack.cancel_and_move_back_to_input')}
                          aria-label={i18nT('components.queueStack.cancel_queued_message')}
                          disabled={isPending}
                          onClick={(e) => { e.stopPropagation(); onCancel(queueId!) }}
                        >
                          <X size={13} />
                        </button>
                      )}
                      {isFrontCollapsed && messages.length > 1 && (
                        <span className="shrink-0 flex items-center gap-1 text-[11px] opacity-70">
                          {messages.length} {i18nT('components.queueStack.queued')}
                          <ChevronUp size={12} />
                        </span>
                      )}
                      {expanded && i === 0 && (
                        <ChevronUp size={13} className="shrink-0 opacity-50 rotate-180" />
                      )}
                    </>
                  )}
                </span>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </motion.div>
    </div>
  )
}

export default memo(QueueStackInner, (prev, next) =>
  prev.messages.length === next.messages.length &&
  prev.fuseBelow === next.fuseBelow &&
  prev.pendingIds === next.pendingIds &&
  prev.messages.every((m, i) => m === next.messages[i]) &&
  prev.onCancel === next.onCancel &&
  prev.onInterrupt === next.onInterrupt &&
  prev.onEdit === next.onEdit
)
