import { useState, useRef, useEffect, memo } from 'react'
import { AnimatePresence, motion, useMotionValue, useSpring } from 'framer-motion'
import { Hourglass, ChevronUp, X, Zap, Pencil, Check, Bot, Loader2, ArrowUp, ArrowDown, AppWindow } from 'lucide-react'
import type { ChatMessage } from '../types'
import { useImeGuard } from '../hooks/useImeGuard'
import { Glass } from './Glass'

import { i18nT } from '../i18n/t'
import { parseRecoveryMessage } from '../pages/chat/RecoveryCard'
import { stripAppEnvelope } from '../pages/chat/groupDisplayItems'
import { hasSubagentCompletionPrefix } from '../pages/chat/subagentCompletion'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
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

/** An MCP-App ui/message entry waiting in the queue. It stays VISIBLE in the
 *  stack (the user's own click, awaiting delivery) but read-only: editing its
 *  card would drain the user's replacement words as app-authored (inject row,
 *  actor `app`, channel mirror suppressed) — the backend refuses the edit
 *  (queue_edit_by_id), so the card must not offer it. The `meta.kind` tag
 *  rides on both the live twin row and the slot-detail queue hydration. */
export function isAppMessageQueued(m: ChatMessage): boolean {
  return m.meta?.kind === 'mcp_app_message'
}

/** Display text for a queued entry: an app-message entry drops its machine
 *  envelope so the queue card wears the same skin the transcript row will —
 *  two skins for one message read as two different messages. */
export function queuedDisplayText(m: ChatMessage): string {
  if (!isAppMessageQueued(m)) return m.content
  return stripAppEnvelope(m.content)
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
      // `relative z-[2]`: one explicit layer in the composer dock's status
      // stack, below the composer's own `z-10` like every other bar there, so
      // the stack's paint order is stated rather than left to DOM order.
      // ChatPage.statusStackLayering.test.tsx pins the ordering.
      className="relative z-[2] mx-auto w-full px-4"
      style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
      data-testid="subagent-delivery-progress"
    >
      {/* The dock's glass (components/Glass.tsx) on the accent tint step, like
          the sub-agent bar this line stands in for once the wave has landed. */}
      <Glass variant="chip" radius={8} className="mb-1 flex items-center gap-2 glass-accent px-3 py-1.5 text-[12px] font-mono text-muted">
        <Bot size={13} className="text-accent/70 shrink-0" />
        <Loader2 size={12} className="animate-spin text-accent/70 shrink-0" />
        <span>
          {i18nT('components.queueStack.sub_agent_result', { count: count })} {i18nT('components.queueStack.ready_processing_after_the_current_turn')}
        </span>
      </Glass>
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

const SPRING = { type: 'spring' as const, stiffness: 400, damping: 30 }

/** Inline editor (textarea + save) swapped in for the message text while editing.
 *  Owns the live value so its own controls commit the typed text, never stale content.
 *
 *  A textarea, not an `<input>`: a queued message can span several lines --
 *  the attachment serializer writes one `[attached_file N] path` marker per
 *  line -- and a single-line input drops every newline from its value, so an
 *  ordinary edit would glue the markers together and the queue edit's
 *  whitespace-bounded marker match would prune every attachment but the last.
 *  Enter commits (the composer's own contract); Shift+Enter inserts a line. */
function EditInput({ initial, onCommit, onCancel }: {
  initial: string
  onCommit: (value: string) => void
  onCancel: () => void
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const ime = useImeGuard()
  const [value, setValue] = useState(initial)
  // Guard so blur and an explicit save/Enter don't both fire onCommit.
  const committedRef = useRef(false)
  // Select the FIRST line only, never the whole value: the marker lines sit
  // below the single visible row, and a select-all would let an ordinary
  // retype replace them unseen -- the queue edit then prunes every
  // attachment from the send with nothing on screen to say so.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    const nl = initial.indexOf('\n')
    el.setSelectionRange(0, nl === -1 ? initial.length : nl)
  }, [initial])
  // Lines below the visible one, surfaced as a count so the hidden part of
  // the value is never a surprise. When every hidden line is an attachment
  // marker (the serializer's `[attached_file N] path` / `[attached_dir N]
  // path` lines) the cue names them as attachments -- "+2 attachments" says
  // what is there, where "+2 lines" only says how much.
  const hidden = value.split('\n').slice(1)
  const hiddenLines = hidden.length
  const hiddenAreAttachments = hiddenLines > 0 && hidden.every(l => /^\[attached_(?:file|dir) \d+\] /.test(l))
  const hiddenCue = hiddenAreAttachments
    ? i18nT('components.queueStack.hidden_attachments', { count: hiddenLines })
    : i18nT('components.queueStack.hidden_lines', { count: hiddenLines })
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
      <textarea
        ref={ref}
        value={value}
        // One visible row: the card is a fixed-height stack slot (CARD_H) and
        // shows the content itself truncated to one line, so the editor shows
        // the same line the card does. The value keeps every newline; the
        // textarea scrolls to the caret as the user moves through the lines.
        rows={1}
        onChange={e => setValue(e.target.value)}
        // Stop the card's expand/collapse + drag handlers from swallowing pointer + key events.
        onPointerDown={e => e.stopPropagation()}
        onClick={e => e.stopPropagation()}
        onKeyDown={e => {
          e.stopPropagation()
          if (e.key === 'Enter' && !e.shiftKey) {
            // The commit's own emptiness check stays in commit(). claimEnter
            // consumes the keypress, so a committing Enter never inserts a line.
            if (ime.claimEnter(e)) commit()
          } else if (e.key === 'Escape') { e.preventDefault(); ime.reset(); cancel() }
        }}
        {...ime.bindComposition({ onBlur: commit })}
        className="flex-1 min-w-0 resize-none overflow-hidden bg-[var(--bg)] text-[var(--text)] placeholder:text-[var(--muted)] rounded px-1.5 py-0.5 text-[13px] leading-5 outline-hidden border border-[var(--border)] focus-visible:border-[var(--accent)]"
        aria-label={i18nT('components.queueStack.edit_queued_message')}
      />
      {hiddenLines > 0 && (
        <span className="shrink-0 text-[11px] text-[var(--muted)] tabular-nums" data-testid="queue-edit-hidden-lines"
          title={hiddenCue}>
          {hiddenCue}
        </span>
      )}
      <button className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors text-[var(--text)]"
        title={i18nT('components.queueStack.save')} aria-label={i18nT('components.queueStack.save_edit')}
        // mousedown commits before the input's blur can fire with the same value.
        onMouseDown={e => { e.preventDefault(); e.stopPropagation() }}
        onClick={e => { e.stopPropagation(); commit() }}>
        <Check size={13} />
      </button>
    </>
  )
}

function QueueStackInner({ messages, onCancel, onInterrupt, onEdit, onReorder, fuseBelow = true, pendingIds }: {
  messages: ChatMessage[]
  onCancel?: (queueId: string) => void
  onInterrupt?: (queueId: string) => void
  onEdit?: (queueId: string, content: string) => void
  /** Move a queued message one step toward the front (`next`) or the back
   *  (`later`) of the run order. Index 0 runs first. */
  onReorder?: (queueId: string, direction: 'next' | 'later') => void
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
    // `zIndex: 2`: an explicit layer in the composer dock's status stack, far
    // below the composer's own `z-10`, so the collapsed card's -OVERLAP fuse
    // keeps sliding UNDER the input box rather than over it.
    // ChatPage.statusStackLayering.test.tsx pins the ordering.
    <div className="px-4 mx-auto w-full relative" style={{ maxWidth: 'var(--mc-content-width, 900px)', zIndex: 2 }}>
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

            if (expanded) {
              const pos = messages.length - 1 - i
              y = pos * (CARD_H + EXPANDED_GAP)
              scale = 1
              opacity = 1
              zIndex = pos + 1
            } else if (i <= MAX_PEEK) {
              const depth = i
              y = (collapsedHeight - CARD_H) - depth * PEEK
              scale = 1 - (depth + 1) * SCALE_STEP
              opacity = 1
              zIndex = (MAX_PEEK + 1) - depth
            } else {
              y = (collapsedHeight - CARD_H) - MAX_PEEK * PEEK
              scale = 1 - (MAX_PEEK + 1) * SCALE_STEP - HIDDEN_EXTRA_SCALE
              opacity = 0
              zIndex = 0
            }

            const isFrontCollapsed = !expanded && i === 0
            const queueId = m.meta?.queueId as string | undefined
            const isEditing = !!queueId && editingId === queueId
            const isPending = !!queueId && !!pendingIds?.has(queueId)
            // Per-card actions show on the front single card or when expanded.
            const showActions = (expanded || messages.length === 1) && !!queueId
            // App-message entries are read-only-but-visible: the backend
            // refuses queue_edit_by_id for system-injection kinds (the user's
            // replacement words would drain app-authored), so the card must
            // not offer the pencil. Cancel/interrupt/reorder stay: they change
            // WHEN or WHETHER the entry runs, never who authored its text.
            const isAppEntry = isAppMessageQueued(m)
            const displayText = queuedDisplayText(m)

            return (
              // The motion box only places the card (peek offset, scale, layer);
              // the card itself is the composer dock's glass on the warn tint
              // step. Two things the old solid card animated are gone with it:
              // the per-depth `brightness()` filter (a filter on the box would
              // make it the backdrop root, and the glass inside would have
              // nothing left to blur) and the square-bottomed "fused" corners
              // (the primitive has one radius; the front card's bottom -OVERLAP
              // now sits UNDER the composer's own glass, which is the seam).
              <motion.div
                key={m.meta?.queueId as string ?? m.ts ?? `q-${i}-${m.content}`}
                initial={false}
                animate={{ opacity, y, scale }}
                exit={{ y: y + 40, zIndex: 50, transition: SPRING }}
                transition={SPRING}
                className="absolute top-0 left-0 right-0"
                style={{ transformOrigin: 'bottom center', height: CARD_H, zIndex }}
              >
              {/* `glass-warn`, not a theme-specific color: the warn tint step is
                  what every pending-decision pane in the dock wears, and the
                  solid fallbacks in index.css mix the same hue into
                  `--bg-elevated` where the glass cannot paint. Cards behind
                  peek out above this one as more glass, as a stack of panes
                  would. */}
              {/* `data-testid="queue-card"` is the hook the capture harnesses
                  (capture-queued-cancel-restore, capture-members-steer-only)
                  wait on; it replaces the old `queue-card` class, which no
                  longer has a style to carry. */}
              <Glass
                variant="chip"
                radius={12}
                data-testid="queue-card"
                className="h-full glass-warn px-3 py-2 text-[13px] text-warn"
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
                      {/* Same attribution the transcript row wears: a queued
                          app message must not read as the user's own words
                          while it waits (one message, one identity). The
                          visible "Queued" word states the pending state in
                          words — the hourglass alone is icon-only, and color
                          coincidence is not how a reader should have to link
                          the strip to the transcript row it becomes. */}
                      {isAppEntry && (
                        <span className="min-w-0 shrink text-muted text-[11px] inline-flex items-center gap-1 cursor-help" title={i18nT('components.mcpApp.from_app_tooltip')}>
                          <span className="shrink-0 uppercase tracking-wide opacity-70">{i18nT('components.queueStack.queued')}</span>
                          <AppWindow size={11} className="shrink-0" />
                          <span className="truncate">{i18nT('components.mcpApp.from_app', { app: String((m.meta?.appLabel as string) || 'app').split('/')[0] })}</span>
                        </span>
                      )}
                      <span className="truncate flex-1">{displayText}</span>
                      {/* Reorder arrows only make sense with 2+ cards, and only
                          in the expanded stack where the run order is visible.
                          Index 0 runs first and renders at the BOTTOM of the
                          expanded stack, so "run sooner" moves the card DOWN
                          visually: ArrowDown = sooner, ArrowUp = later. */}
                      {onReorder && expanded && messages.length > 1 && (
                        <>
                          <button
                            className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-30 disabled:hover:bg-transparent"
                            title={i18nT('components.queueStack.run_sooner')}
                            aria-label={i18nT('components.queueStack.run_sooner')}
                            disabled={i === 0}
                            onClick={(e) => { e.stopPropagation(); onReorder(queueId!, 'next') }}
                          >
                            <ArrowDown size={13} />
                          </button>
                          <button
                            className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-30 disabled:hover:bg-transparent"
                            title={i18nT('components.queueStack.run_later')}
                            aria-label={i18nT('components.queueStack.run_later')}
                            disabled={i === messages.length - 1}
                            onClick={(e) => { e.stopPropagation(); onReorder(queueId!, 'later') }}
                          >
                            <ArrowUp size={13} />
                          </button>
                        </>
                      )}
                      {onEdit && showActions && !isAppEntry && (
                        <button
                          className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
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
                          className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors text-[var(--text)] disabled:opacity-40 disabled:cursor-not-allowed"
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
                          className="shrink-0 p-0.5 rounded hover:bg-[var(--bg-hover)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={i18nT(isAppEntry ? 'components.queueStack.cancel_queued_message' : 'components.queueStack.cancel_and_move_back_to_input')}
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
              </Glass>
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
  prev.onEdit === next.onEdit &&
  prev.onReorder === next.onReorder
)
