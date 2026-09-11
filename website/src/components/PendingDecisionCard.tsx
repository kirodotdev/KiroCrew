import { useEffect, useState } from 'react'
import { CornerDownLeft, Reply, Send, X } from 'lucide-react'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'
import { api, ApiError } from '../api/client'
import { timeAgo } from '../utils/timeAgo'
import type { PendingDecision } from '../types'

interface PendingDecisionCardProps {
  /** Slot this decision belongs to — the dismiss round-trip names it. */
  slotKey: string
  /** The slot payload's `pending_decision`. The MOUNT is gated on it being
   *  present (and on no question card owning the band), so this component
   *  never reads the store itself: both surfaces (single view, grid panes)
   *  already hold their slot record, and a prop keeps the two from selecting
   *  it differently. */
  decision: PendingDecision
  /** Put the picked options' text into the message box (the secondary action). */
  onPick: (text: string) => void
  /** Send the picked options as a message immediately (the primary action).
   *  Required: both surfaces (ChatPage, ChatPane) have a direct sender, and
   *  their offline handling lives at the call site, which falls back to the
   *  message box itself before invoking this. */
  onSendDirect: (text: string) => void
}

/**
 * The "Waiting on you" card for a buried [OPTIONS:] decision.
 *
 * The composer chips derive from the NEWEST assistant turn, so a monitor-loop
 * cycle's option-less reply takes an unanswered ask off screen everywhere —
 * the user returns to a loop session and has to scroll for "what's waiting on
 * me?". This card re-surfaces exactly that: the question excerpt, its age, and
 * the choices, pinned above the composer until a real user message answers it
 * (the server derivation retires on any `user` row) or the ✕ dismisses it.
 *
 * Interaction mirrors the sibling QuestionCard, not the FollowUpBar chips:
 * options are aria-pressed TOGGLES (multi-pick joins with ", ", the same
 * suffix shape the composer chips append), and exactly two action buttons
 * dispatch — Send, and Add to composer. That keeps the action row within the
 * `max-two-buttons-per-row` budget: N options are selections, not N sibling
 * actions.
 *
 * A pending question card owns the above-composer band outright (the mount
 * gates on it), matching the sidebar's precedence where `needs_input` outranks
 * `pending_decision`.
 */
export default function PendingDecisionCard({ slotKey, decision, onPick, onSendDirect }: PendingDecisionCardProps) {
  /* Optimistic hide, keyed by the decision's identity rather than a boolean:
     this component stays mounted while the slot payload refreshes, and a NEWER
     options turn (different ts) must render even though an older one was just
     dismissed. */
  const [dismissedTs, setDismissedTs] = useState<string | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [failed, setFailed] = useState(false)
  const ts = decision.ts || ''
  /* This component stays mounted while a NEWER options turn supersedes the one
     on screen (see the dismissedTs note above), so per-decision interaction
     state must retire with the decision it belongs to: a selection made on the
     old question must not pre-answer the new one, and a spent send lock must
     not hold the new question's actions shut. */
  useEffect(() => {
    setPicked(new Set())
    setSent(false)
    setFailed(false)
  }, [ts])
  if (dismissedTs && dismissedTs === ts) return null
  const options = (decision.options ?? []).filter((o) => typeof o === 'string' && o.trim())
  if (options.length === 0) return null

  const askedMs = ts ? Date.parse(ts) : NaN
  /* A negative age means the row's naive timestamp parsed ahead of the local
     clock — show nothing rather than "in 3 hours". Cosmetic either way. */
  const askedAgo = Number.isFinite(askedMs) && Date.now() >= askedMs
    ? i18nT('components.pendingDecisionCard.asked_ago', { time: timeAgo(askedMs / 1000) })
    : null

  const toggle = (option: string) => {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(option)) next.delete(option)
      else next.add(option)
      return next
    })
  }
  /* Joined in the OPTIONS order, not click order — the same ordered suffix the
     composer chips append, so a multi-pick answer reads the same either way. */
  const pickedText = () => options.filter((o) => picked.has(o)).join(', ')

  const dismiss = () => {
    if (busy) return
    /* No server identity to name (an older payload, or a fixture): local-only
       hide, same degradation the question card's dismiss uses. */
    if (!ts) { setDismissedTs(''); return }
    setBusy(true)
    setFailed(false)
    api
      .dismissPendingDecision(slotKey, ts)
      .then(() => setDismissedTs(ts))
      .catch((err) => {
        // 404 = the slot is gone server-side; the card is stale, take it away.
        if (err instanceof ApiError && err.status === 404) { setDismissedTs(ts); return }
        // Anything else is retryable — keep the card AND say why it is still
        // here, or the retry never happens.
        setFailed(true)
      })
      .finally(() => setBusy(false))
  }

  return (
    <div
      data-testid="pending-decision-card"
      className="rounded-lg p-3 animate-rise"
      style={{ background: 'var(--warn-subtle)', borderLeft: '3px solid var(--warn)' }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0 text-[11px] font-bold tracking-wide" style={{ color: 'var(--warn)' }}>
          <Reply size={12} className="shrink-0" aria-hidden />
          <span className="truncate uppercase">{i18nT('components.pendingDecisionCard.waiting_on_you')}</span>
          {askedAgo && <span className="font-normal normal-case shrink-0 text-muted">· {askedAgo}</span>}
        </div>
        <button
          type="button"
          data-testid="pending-decision-dismiss"
          className="shrink-0 p-0.5 rounded text-muted hover:text-text disabled:opacity-50"
          aria-label={i18nT('components.pendingDecisionCard.dismiss')}
          title={i18nT('components.pendingDecisionCard.dismiss_hint')}
          disabled={busy}
          onClick={dismiss}
        >
          <X size={13} aria-hidden />
        </button>
      </div>
      {decision.excerpt && (
        <div className="text-xs italic mt-1.5" style={{ color: 'var(--card-fg, var(--text))' }}>
          {decision.excerpt}
        </div>
      )}
      <div className="flex flex-col gap-1.5 mt-2">
        {options.map((option) => {
          const isSelected = picked.has(option)
          return (
            <button
              key={option}
              type="button"
              data-testid="pending-decision-option"
              onClick={() => toggle(option)}
              /* WCAG 4.1.2, same reasoning as QuestionCard: the selected state
                 is programmatic via aria-pressed, and a toggle button matches
                 the click-again-to-deselect behaviour exactly. */
              aria-pressed={isSelected}
              className={`text-left px-3 py-2 rounded-lg text-[13px] cursor-pointer transition-all border ${
                isSelected
                  ? 'border-accent text-text bg-accent-subtle/60'
                  : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg'
              }`}
            >
              <span className="font-medium">{option}</span>
            </button>
          )
        })}
      </div>
      <div className="flex items-center justify-end gap-2 mt-2.5">
        <button
          type="button"
          data-testid="pending-decision-fill"
          onClick={() => { const text = pickedText(); if (text) onPick(text) }}
          disabled={picked.size === 0 || sent}
          title={i18nT('components.pendingDecisionCard.add_to_message_box_hint')}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-transparent text-muted hover:text-text border border-border"
        >
          <CornerDownLeft size={14} aria-hidden /> {i18nT('components.pendingDecisionCard.add_to_message_box')}
        </button>
        <button
          type="button"
          data-testid="pending-decision-send"
          /* The lock is set synchronously in the same click that dispatches, so
             a double-click's second event sees `sent` already true — one user
             turn per decision, until the slot payload retires the card (or a
             newer decision arrives and the ts effect re-arms the actions). */
          onClick={() => {
            if (sent) return
            const text = pickedText()
            if (!text) return
            setSent(true)
            onSendDirect(text)
          }}
          disabled={picked.size === 0 || sent}
          /* Disabled state must READ disabled: an opacity-dimmed accent fill
             still renders as a saturated live button, and a cold reader treats
             clicking it as a choice rather than impossible. Muted outline
             while disabled, accent fill only when armed — the same split the
             secondary button gets for free from its grey palette. */
          className={`inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-[13px] font-medium transition-all ${
            picked.size === 0 || sent
              ? 'bg-transparent text-muted border border-border opacity-60 cursor-not-allowed'
              : 'bg-accent text-accent-fg hover:bg-accent-hover border-none cursor-pointer'
          }`}
        >
          <Send size={14} aria-hidden />{' '}
          {picked.size > 1
            ? i18nT('components.pendingDecisionCard.submit_n_answers', { count: String(picked.size) })
            : i18nT('components.pendingDecisionCard.submit')}
        </button>
      </div>
      {failed && (
        <ErrorNotice
          className="mt-2"
          testId="pending-decision-dismiss-error"
          message={i18nT('components.pendingDecisionCard.dismiss_failed')}
          onDismiss={() => setFailed(false)}
          /* askAgent: a failed dismiss holds no unsaved draft — the hand-off
             cannot destroy anything, and the failing endpoint's context is
             exactly what the agent can investigate. */
          askAgent
        />
      )}
    </div>
  )
}
