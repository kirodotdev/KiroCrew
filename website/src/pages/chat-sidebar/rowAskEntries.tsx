/** The sidebar row's status entries for an ask the agent put to the person:
 *  an open question card, and a pinned waiting-on-you ask. Both rank with the
 *  owed decisions in `SessionRow`'s status chain, above every "working" signal. */
import { MessageCircleQuestionMark } from 'lucide-react'
import { ActionForYouRowLine } from '../../components/waitingOnYou/LazyActionForYouStatus'
import { actionForYou } from '../../lib/waitingOnYou'
import type { ChatSlot } from '../../types'

/** An unanswered question card. A blocking card keeps `running` true, so
 *  without this the row would show "Thinking…" while nothing can advance.
 *  Info-coloured and static-glyphed to stay distinct from the warn-coloured
 *  approval rows above it.
 *
 *  A card is a websocket broadcast with no transcript row, so `last_message`
 *  is whatever the agent last said BEFORE the ask, not the question. Trailing
 *  it after "Needs your answer ·" would read as the question itself, so the
 *  label stands alone. */
export function needsInputRowEntry(needsInput: boolean, label: string, lineCls: string, iconPx: number) {
  return {
    key: 'needs_input',
    when: needsInput,
    build: () => (
      <div className={lineCls} title={label}>
        <MessageCircleQuestionMark size={iconPx} className="shrink-0" style={{ color: 'var(--info)' }} aria-hidden />
        <span className="truncate font-medium" style={{ color: 'var(--info)' }}>{label}</span>
      </div>
    ),
  }
}

/** A pinned ask, shown below a live question card and above a loop's
 *  `[OPTIONS:]` wait: the agent declared it, so it is the more specific ask. */
export function actionForYouRowEntry(s: Pick<ChatSlot, 'waiting_on_you' | 'pending_approval' | 'needs_input'>, lineCls: string, iconPx: number) {
  return {
    key: 'waiting_on_you',
    when: !!actionForYou(s),
    build: () => <ActionForYouRowLine slot={s} lineCls={lineCls} iconPx={iconPx} />,
  }
}
