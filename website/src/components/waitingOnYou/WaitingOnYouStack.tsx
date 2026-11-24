/** The pinned asks of one slot, shown in the composer band below the question
 *  card: the newest in full, with "N more" above it that expands in place.
 *
 *  The asks come only from the slot row's `waiting_on_you`, which the gateway
 *  pushes after every store change; the caller passes that row's asks in, so
 *  this component holds no copy of its own beyond what a click left
 *  unresolved: a note being typed, or why an answer did not go.
 *
 *  An ask that goes while its note is unsent keeps its element and its key: the
 *  card turns into its notice where it stood, instead of leaving while a
 *  different card arrives. */
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { Btn } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { fmtNumber, joinSentences } from '../../i18n/format'
import { useAppSelector } from '../../store'
import { selectSlotMessages } from '../../store/chatSlice'
import type { WaitingOnYouAsk } from '../../types'
import WaitingOnYouCard, { type WaitingClosedView } from './WaitingOnYouCard'
import { deleteWaitingDraft, setWaitingDraft, useWaitingDrafts, waitingDraftText, type WaitingDraft } from './drafts'
import { useWaitingOnYouActions } from './useWaitingOnYouActions'

const motionProps = {
  layout: true,
  initial: { opacity: 0, y: 4 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -4 },
  transition: { duration: 0.14 },
} as const

/** One card in the stack: a live ask, or a gone one whose note was never sent. */
interface StackEntry {
  ask: WaitingOnYouAsk
  draft?: WaitingDraft
}

/** The row's order for asks still in it; an ask gone from the row goes back
 *  where its age puts it, so a card that closes stays where it stood. */
function inStackOrder(kept: readonly StackEntry[], gone: readonly StackEntry[]): StackEntry[] {
  const out = [...kept]
  for (const entry of [...gone].sort((a, b) => a.ask.ts - b.ask.ts)) {
    const at = out.findIndex(e => e.ask.ts > entry.ask.ts)
    out.splice(at === -1 ? out.length : at, 0, entry)
  }
  return out
}

export interface WaitingOnYouStackProps {
  slotKey: string | null
  /** The slot row's pinned asks, in stack order (`pinnedAsks`). */
  asks: readonly WaitingOnYouAsk[]
  agent?: string
  /** `list` shows every ask in full, for a panel that lists them (the Questions tab). */
  layout?: 'stack' | 'list'
}

export default function WaitingOnYouStack({ slotKey, asks: pinned, agent, layout = 'stack' }: WaitingOnYouStackProps) {
  const { t } = useTranslation()
  const { states, answer, dismiss, clear } = useWaitingOnYouActions(slotKey)
  const drafts = useWaitingDrafts(slotKey)
  const slotMessages = useAppSelector(state => slotKey ? selectSlotMessages(state, slotKey) : state.chat.messages)
  const queuedAnswers = useMemo(() => slotMessages.flatMap(message => {
    if (message.role !== 'queued') return []
    const claim = message.meta?.waitingOnYouAnswer as { key?: unknown; ask_id?: unknown } | undefined
    return typeof claim?.key === 'string' && typeof claim.ask_id === 'string' ? [claim] : []
  }), [slotMessages])
  const answerIsQueued = (ask: WaitingOnYouAsk) => queuedAnswers.some(claim => claim.key === ask.key && claim.ask_id === ask.ask_id)
  const [expanded, setExpanded] = useState(false)
  const live = pinned.filter(a => answerIsQueued(a) || !states[a.ask_id]?.settled)
  const showAll = layout === 'list' || expanded
  const shownLive = new Set((showAll ? live : live.slice(-1)).map(a => a.ask_id))
  const hidden = live.length - shownLive.size
  const pinnedIds = new Set(pinned.map(a => a.ask_id))
  const kept = pinned.filter(a => shownLive.has(a.ask_id)).map((a): StackEntry => ({ ask: a }))
  const gone: StackEntry[] = drafts
    .filter(d => !pinnedIds.has(d.ask.ask_id))
    .map(d => ({ ask: d.ask, draft: d }))
  const entries = inStackOrder(kept, gone)
  // A refusal whose ask is gone has no card left to carry it, so the stack says it.
  const shownIds = new Set(entries.map(e => e.ask.ask_id))
  const orphaned = Object.entries(states).filter(([askId, s]) => s.errorKey && !pinnedIds.has(askId) && !shownIds.has(askId))
  if (!slotKey || (entries.length === 0 && orphaned.length === 0)) return null

  const closedView = ({ ask, draft }: StackEntry): WaitingClosedView | undefined => {
    if (!draft) return undefined
    return {
      testId: 'waiting-on-you-closed',
      message: joinSentences([t('components.waitingOnYou.closed'), t('components.waitingOnYou.closed_draft')]),
      note: draft.text,
      actions: (
        <Btn type="button" onClick={() => deleteWaitingDraft(draft.slot, ask.ask_id)} data-testid="waiting-on-you-discard-draft">
          {t('components.waitingOnYou.discard')}
        </Btn>
      ),
    }
  }

  return (
    <section className={`flex flex-col gap-1.5 ${layout === 'stack' ? 'max-h-[min(50svh,32rem)] overflow-y-auto overscroll-contain' : ''}`} aria-label={t('components.waitingOnYou.stack_label')} data-testid="waiting-on-you-stack" data-slot={slotKey}>
      {/* No hand-off: the composer this stack sits above may hold an unsent message. */}
      {orphaned.map(([askId, s]) => (
        <ErrorNotice key={askId} testId="waiting-on-you-orphan-error" title={s.title}
          message={[t(s.errorKey as string), s.errorDetail].filter(Boolean).join(' ')}
          onDismiss={() => clear(askId)} />
      ))}
      <AnimatePresence initial={false}>
        {layout === 'stack' && live.length > 1 && (
          <motion.div key="more" layout className="flex">
            <button type="button" aria-expanded={expanded} onClick={() => setExpanded(v => !v)} data-testid="waiting-on-you-more"
              className="inline-flex items-center gap-1 p-0 bg-transparent border-none cursor-pointer text-[12px] text-muted hover:text-text">
              {expanded ? <ChevronDown size={12} aria-hidden /> : <ChevronUp size={12} aria-hidden />}
              {expanded ? t('components.waitingOnYou.show_fewer') : t('components.waitingOnYou.more_count', { count: hidden, n: fmtNumber(hidden) })}
            </button>
          </motion.div>
        )}
        {entries.map(entry => (
          <motion.div key={entry.ask.ask_id} {...motionProps}>
            <WaitingOnYouCard
              ask={entry.ask}
              agent={agent}
              state={states[entry.ask.ask_id]}
              answerQueued={answerIsQueued(entry.ask)}
              note={waitingDraftText(slotKey, entry.ask.ask_id)}
              onNoteChange={text => setWaitingDraft(slotKey, entry.ask, text)}
              onAnswer={choice => answer(entry.ask, choice)}
              onDismiss={() => dismiss(entry.ask)}
              closed={closedView(entry)}
            />
          </motion.div>
        ))}
      </AnimatePresence>
    </section>
  )
}
