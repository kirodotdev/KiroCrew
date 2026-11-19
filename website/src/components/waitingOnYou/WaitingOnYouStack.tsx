/** The pinned asks of one slot, shown in the composer band below the question
 *  card: the newest in full, with "N more" above it that expands in place.
 *
 *  The asks come only from the slot row's `waiting_on_you`, which the gateway
 *  pushes after every store change; the caller passes that row's asks in, so
 *  this component holds no copy of its own beyond what a click left
 *  unresolved: a note being typed, or why an answer did not go.
 *
 *  An ask that goes while it is on screen keeps its element and its key: the
 *  card turns into its notice where it stood, instead of leaving while a
 *  different card arrives. */
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { Btn } from '../ui'
import { fmtNumber, joinSentences } from '../../i18n/format'
import type { WaitingOnYouAsk } from '../../types'
import WaitingOnYouCard, { type WaitingClosedView } from './WaitingOnYouCard'
import { deleteWaitingDraft, setWaitingDraft, useWaitingDrafts, waitingDraftText, type WaitingDraft } from './drafts'
import { useWaitingOnYouActions, type WaitingAskState } from './useWaitingOnYouActions'

const STALE_KEYS = {
  answered: 'components.waitingOnYou.answered_elsewhere',
  superseded: 'components.waitingOnYou.superseded',
  cleared: 'components.waitingOnYou.cleared',
} as const

const motionProps = {
  layout: true,
  initial: { opacity: 0, y: 4 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -4 },
  transition: { duration: 0.14 },
} as const

type StaleState = WaitingAskState & { ask: WaitingOnYouAsk; stale: NonNullable<WaitingAskState['stale']> }

/** One card in the stack: a live ask, or a gone one with the notice it shows. */
interface StackEntry {
  ask: WaitingOnYouAsk
  stale?: StaleState
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

export default function WaitingOnYouStack({ slotKey, asks: pinned, agent, onReturnToComposer, layout = 'stack' }: {
  slotKey: string | null
  /** The slot row's pinned asks, in stack order (`pinnedAsks`). */
  asks: readonly WaitingOnYouAsk[]
  agent?: string
  /** Put text the person chose or typed back into this slot's composer, unsent. */
  onReturnToComposer?: (text: string) => void
  /** `list` shows every ask in full, for a panel that lists them (the Questions tab). */
  layout?: 'stack' | 'list'
}) {
  const { t } = useTranslation()
  const { states, answer, dismiss, forget } = useWaitingOnYouActions(slotKey, onReturnToComposer)
  const drafts = useWaitingDrafts(slotKey)
  const [expanded, setExpanded] = useState(false)
  const staleOf = (askId: string): StaleState | undefined => {
    const st = states[askId]
    return st?.stale && st.ask ? st as StaleState : undefined
  }
  const live = pinned.filter(a => !states[a.ask_id]?.settled)
  const showAll = layout === 'list' || expanded
  const shownLive = new Set((showAll ? live : live.slice(-1)).map(a => a.ask_id))
  const hidden = live.length - shownLive.size
  const pinnedIds = new Set(pinned.map(a => a.ask_id))
  const kept = pinned.flatMap((a): StackEntry[] => {
    const stale = staleOf(a.ask_id)
    if (stale) return [{ ask: stale.ask, stale }]
    return shownLive.has(a.ask_id) ? [{ ask: a }] : []
  })
  const gone: StackEntry[] = [
    ...Object.keys(states).filter(id => !pinnedIds.has(id)).flatMap(id => {
      const stale = staleOf(id)
      return stale ? [{ ask: stale.ask, stale }] : []
    }),
    ...drafts.filter(d => !pinnedIds.has(d.ask.ask_id) && !staleOf(d.ask.ask_id)).map(d => ({ ask: d.ask, draft: d })),
  ]
  const entries = inStackOrder(kept, gone)
  if (!slotKey || entries.length === 0) return null

  const closedView = ({ ask, stale, draft }: StackEntry): WaitingClosedView | undefined => {
    if (stale) return {
      testId: 'waiting-on-you-stale',
      message: joinSentences([t(STALE_KEYS[stale.stale]), stale.returned ? t('components.waitingOnYou.returned_to_composer') : '']),
      onClose: () => forget(ask.ask_id),
    }
    if (draft) return {
      testId: 'waiting-on-you-closed',
      message: joinSentences([t('components.waitingOnYou.closed'), t('components.waitingOnYou.closed_draft')]),
      actions: <>
        {onReturnToComposer && (
          <Btn type="button" onClick={() => { onReturnToComposer(draft.text); deleteWaitingDraft(draft.slot, ask.ask_id) }} data-testid="waiting-on-you-copy-draft">
            {t('components.waitingOnYou.copy_to_composer')}
          </Btn>
        )}
        <Btn type="button" onClick={() => deleteWaitingDraft(draft.slot, ask.ask_id)} data-testid="waiting-on-you-discard-draft">
          {t('components.waitingOnYou.discard')}
        </Btn>
      </>,
    }
    return undefined
  }

  return (
    <section className="flex flex-col gap-1.5" aria-label={t('components.waitingOnYou.stack_label')} data-testid="waiting-on-you-stack" data-slot={slotKey}>
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
